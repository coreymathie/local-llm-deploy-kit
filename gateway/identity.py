# Corey Mathie, 2026
"""
Caller identity: who is calling and what they may do.

Two kinds of credentials are accepted on the same `Authorization: Bearer ...` header:

- API keys (one per application, as before). An admin key has the `admin` role, any other key the
  `user` role. Keys may also carry groups (used by document ACLs).
- OIDC access tokens (JWTs) from a configured issuer, when GATEWAY_OIDC_ENABLED=true. The signature is
  checked against the issuer's JWKS (fetched over HTTPS, cached, refreshed on key rotation), the
  algorithm must be on the allow-list (RS256, ES256; never "none" or HMAC), and `iss`, `aud`, `exp`
  and `sub` are required, with a configurable clock skew for `exp`/`nbf`. The groups claim is mapped
  to gateway roles with GATEWAY_OIDC_GROUP_ROLES.

Roles:
  admin              everything: key management, documents, ACLs, audit, model pulls, all collections
  user               chat, embeddings, models; collections whose ACL admits the caller, and collections
                     with no ACL when GATEWAY_COLLECTION_DEFAULT_ACCESS=open
  reader:<c>         may list and ask collection <c> (reader:* for every collection); nothing else

A valid token that maps to no role is rejected with 403. SCIM provisioning is not implemented: groups
come only from the token, so a group change takes effect when the user's next token is issued.

This module imports PyJWT lazily, so the role and ACL logic also runs in the browser demo (Pyodide),
where only the JWT signature check is unavailable.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .config import settings

log = logging.getLogger("gateway.identity")

ROLE_ADMIN = "admin"
ROLE_USER = "user"
READER_PREFIX = "reader:"
ALLOWED_ALGORITHMS = ("RS256", "ES256")
OIDC_METRICS_LABEL = "oidc"  # Prometheus label for token callers: user names never become label values
MAX_TOKEN_BYTES = 16_384
UNKNOWN_KID_REFRESH_SECONDS = 30.0  # at most one JWKS refetch per this interval for unknown key ids
_KTY_FOR_ALG = {"RS256": ("RSA", None), "ES256": ("EC", "P-256")}
_ROLE_RE = re.compile(r"^(admin|user|reader:(\*|[a-z0-9][a-z0-9_-]{0,63}))$")


class IdentityError(Exception):
    """Authentication failed (401) or the identity provider can't be reached (503)."""

    status_code = 401


class IdentityUnavailable(IdentityError):
    status_code = 503


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. `label` is what audit entries show; `key` is never logged."""

    kind: str  # "api_key" | "oidc"
    subject: str  # stable id: the key value for keys (in memory only), "iss|sub" for tokens
    label: str  # "hr-bot" for keys, "user:<username>" for tokens
    roles: frozenset[str]
    groups: frozenset[str] = frozenset()
    key: str | None = None
    metrics_label: str = ""
    claims: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    @property
    def is_user(self) -> bool:
        return ROLE_USER in self.roles or self.is_admin

    def reader_of(self) -> set[str]:
        return {r[len(READER_PREFIX) :] for r in self.roles if r.startswith(READER_PREFIX)}

    def acl_names(self) -> set[str]:
        """The names this caller matches in a document or collection ACL."""
        names = {f"group:{g}" for g in self.groups}
        if self.kind == "api_key":
            names.add(f"key:{self.label}")
        else:
            names.add(self.label)  # user:<username>
        return names

    def public(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "roles": sorted(self.roles),
            "groups": sorted(self.groups),
        }


def principal_for_key(rec) -> Principal:
    """Principal for an API-key record (gateway.models.ApiKey)."""
    return Principal(
        kind="api_key",
        subject=rec.key,
        label=rec.label,
        roles=frozenset({ROLE_ADMIN if rec.is_admin else ROLE_USER}),
        groups=frozenset(getattr(rec, "groups", None) or ()),
        key=rec.key,
        metrics_label=rec.label,
    )


def can_read_collection(principal: Principal, collection: str, acl: list[str] | None = None) -> tuple[bool, str]:
    """Collection-level read decision and its basis (recorded in the audit log).

    Order: admin role; reader:<c> / reader:* role; the collection's ACL if it has one (the caller must
    match an entry); otherwise GATEWAY_COLLECTION_DEFAULT_ACCESS for callers with the user role.
    """
    if principal.is_admin:
        return True, "role:admin"
    readable = principal.reader_of()
    if "*" in readable:
        return True, "role:reader:*"
    if collection in readable:
        return True, f"role:reader:{collection}"
    if acl:
        matched = sorted(principal.acl_names() & set(acl))
        if matched:
            return True, f"collection_acl:{matched[0]}"
        return False, "collection_acl:no_match"
    if principal.is_user and settings.GATEWAY_COLLECTION_DEFAULT_ACCESS == "open":
        return True, "default:open"
    return False, f"default:{settings.GATEWAY_COLLECTION_DEFAULT_ACCESS}"


ACL_ENTRY_RE = re.compile(r"^(group|key|user):[^\s,]{1,128}$")


def check_acl(entries: list[str]) -> list[str]:
    """Validate and normalize an access list: "group:<idp or key group>", "key:<key label>", "user:<username>"."""
    if len(entries) > 64:
        raise ValueError("an access list holds at most 64 entries")
    for entry in entries:
        if not ACL_ENTRY_RE.match(entry):
            raise ValueError(f"invalid access-list entry {entry!r}: use group:<name>, key:<label> or user:<name>")
    return sorted(set(entries))


def can_read_document(principal: Principal, acl: list[str]) -> tuple[bool, str]:
    """Document-level decision, applied after the collection-level one. An empty ACL inherits the collection's.

    Reader roles and collection ACLs do not override a document ACL; only the admin role does.
    """
    if principal.is_admin:
        return True, "role:admin"
    if not acl:
        return True, "inherited"
    matched = sorted(principal.acl_names() & set(acl))
    return (True, f"document_acl:{matched[0]}") if matched else (False, "document_acl:no_match")


# ---------- Settings ----------


def configured_algorithms() -> list[str]:
    algs = [a.strip() for a in settings.GATEWAY_OIDC_ALGORITHMS.split(",") if a.strip()]
    bad = [a for a in algs if a not in ALLOWED_ALGORITHMS]
    if bad or not algs:
        raise ValueError(f"GATEWAY_OIDC_ALGORITHMS may only list {', '.join(ALLOWED_ALGORITHMS)}; got {algs}")
    return algs


def check_role(role: str) -> str:
    if not _ROLE_RE.match(role):
        raise ValueError(f"unknown role {role!r}: use admin, user, reader:<collection> or reader:*")
    return role


def _check_url(url: str, what: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "https" or (parsed.scheme == "http" and settings.GATEWAY_OIDC_ALLOW_HTTP):
        if parsed.netloc:
            return
    raise ValueError(f"{what} must be an https:// URL (set GATEWAY_OIDC_ALLOW_HTTP=true for http): {url!r}")


def validate_settings() -> None:
    """Fail fast at startup on an unsafe or incomplete OIDC configuration."""
    if not settings.GATEWAY_OIDC_ENABLED:
        return
    if not settings.GATEWAY_OIDC_ISSUER or not settings.GATEWAY_OIDC_AUDIENCE:
        raise ValueError("GATEWAY_OIDC_ISSUER and GATEWAY_OIDC_AUDIENCE are required when OIDC is enabled")
    configured_algorithms()
    _check_url(settings.GATEWAY_OIDC_ISSUER, "GATEWAY_OIDC_ISSUER")
    if settings.GATEWAY_OIDC_JWKS_URL:
        _check_url(settings.GATEWAY_OIDC_JWKS_URL, "GATEWAY_OIDC_JWKS_URL")
    for group, roles in settings.GATEWAY_OIDC_GROUP_ROLES.items():
        for role in roles:
            try:
                check_role(role)
            except ValueError as e:
                raise ValueError(f"GATEWAY_OIDC_GROUP_ROLES[{group!r}]: {e}") from e
    if settings.GATEWAY_OIDC_CLOCK_SKEW_SECONDS < 0 or settings.GATEWAY_OIDC_CLOCK_SKEW_SECONDS > 600:
        raise ValueError("GATEWAY_OIDC_CLOCK_SKEW_SECONDS must be between 0 and 600")


# ---------- Claims to roles ----------


def claim(claims: dict, path: str):
    """Read a claim by dotted path, e.g. "realm_access.roles"."""
    value = claims
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def groups_from_claims(claims: dict) -> frozenset[str]:
    raw = claim(claims, settings.GATEWAY_OIDC_GROUPS_CLAIM)
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(g).strip() for g in raw if str(g).strip())


def roles_for_groups(groups: frozenset[str] | set[str]) -> frozenset[str]:
    roles: set[str] = set()
    for group in groups:
        roles.update(settings.GATEWAY_OIDC_GROUP_ROLES.get(group, ()))
    return frozenset(r for r in roles if _ROLE_RE.match(r))


def principal_for_claims(claims: dict) -> Principal:
    username = claim(claims, settings.GATEWAY_OIDC_USERNAME_CLAIM) or claims.get("sub")
    groups = groups_from_claims(claims)
    return Principal(
        kind="oidc",
        subject=f"{claims.get('iss')}|{claims.get('sub')}",
        label=f"user:{username}",
        roles=roles_for_groups(groups),
        groups=groups,
        metrics_label=OIDC_METRICS_LABEL,
        claims=claims,
    )


# ---------- JWKS ----------

HttpGetJson = Callable[[str], Awaitable[dict]]
HTTP_TRANSPORT = None  # tests plug in an httpx transport (fake identity provider)


async def http_get_json(url: str) -> dict:
    async with httpx.AsyncClient(transport=HTTP_TRANSPORT, timeout=httpx.Timeout(5.0)) as client:
        r = await client.get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()


class JWKSCache:
    """Signing keys by `kid`, refreshed every `ttl` seconds and when an unknown `kid` appears.

    - Unknown kid: one refetch, at most once per UNKNOWN_KID_REFRESH_SECONDS (a stream of made-up kids
      can't turn the gateway into a load generator against the identity provider).
    - Provider unreachable: the last good key set keeps working for one more `ttl`, then 503.
    """

    def __init__(
        self,
        jwks_url: str = "",
        issuer: str = "",
        ttl: float = 3600.0,
        fetch: HttpGetJson | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.jwks_url = jwks_url
        self.issuer = issuer
        self.ttl = ttl
        self.fetch = fetch or http_get_json
        self.clock = clock
        self.keys: dict[str, dict] = {}
        self.fetched_at: float | None = None
        self.last_attempt: float | None = None
        self.fetch_count = 0
        self._configured: tuple | None = None
        self._locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def _lock(self) -> asyncio.Lock:
        return self._locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())

    async def _resolve_url(self) -> str:
        if self.jwks_url:
            return self.jwks_url
        doc = await self.fetch(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if doc.get("issuer") != self.issuer:
            raise IdentityUnavailable("discovery document's issuer does not match GATEWAY_OIDC_ISSUER")
        url = doc.get("jwks_uri") or ""
        try:
            _check_url(url, "jwks_uri")
        except ValueError as e:
            raise IdentityUnavailable(str(e)) from e
        self.jwks_url = url
        return url

    async def refresh(self) -> None:
        self.last_attempt = self.clock()
        url = await self._resolve_url()
        doc = await self.fetch(url)
        keys = {}
        for jwk in doc.get("keys", []):
            if not isinstance(jwk, dict) or jwk.get("use", "sig") != "sig":
                continue
            keys[jwk.get("kid", "")] = jwk
        self.fetch_count += 1
        self.keys = keys
        self.fetched_at = self.clock()

    async def _refresh_safely(self) -> bool:
        try:
            await self.refresh()
            return True
        except IdentityUnavailable:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError) as e:  # keep the old keys
            log.warning("JWKS refresh failed: %s", e.__class__.__name__)
            return False

    async def key_for(self, kid: str | None, alg: str) -> dict:
        async with self._lock():
            now = self.clock()
            if self.fetched_at is None:
                if not await self._refresh_safely():
                    raise IdentityUnavailable("identity provider keys are unavailable")
            elif now - self.fetched_at > self.ttl:
                ok = await self._refresh_safely()
                if not ok and now - self.fetched_at > 2 * self.ttl:
                    raise IdentityUnavailable("identity provider keys are stale and can't be refreshed")
            jwk = self._pick(kid, alg)
            if jwk is None and (
                self.last_attempt is None or self.clock() - self.last_attempt >= UNKNOWN_KID_REFRESH_SECONDS
            ):
                await self._refresh_safely()  # key rotation: the provider may have published a new key
                jwk = self._pick(kid, alg)
        if jwk is None:
            raise IdentityError("token signed with an unknown key")
        return jwk

    def _pick(self, kid: str | None, alg: str) -> dict | None:
        kty, crv = _KTY_FOR_ALG[alg]

        def fits(jwk: dict) -> bool:
            return (
                jwk.get("kty") == kty
                and (crv is None or jwk.get("crv") == crv)
                and jwk.get("alg", alg) == alg  # a key published for another algorithm is not reused
            )

        if kid:
            jwk = self.keys.get(kid)
            return jwk if jwk and fits(jwk) else None
        candidates = [j for j in self.keys.values() if fits(j)]
        return candidates[0] if len(candidates) == 1 else None


_cache: JWKSCache | None = None


def jwks_cache() -> JWKSCache:
    global _cache
    want = (settings.GATEWAY_OIDC_JWKS_URL, settings.GATEWAY_OIDC_ISSUER, settings.GATEWAY_OIDC_JWKS_CACHE_SECONDS)
    if _cache is None or _cache._configured != want:  # settings changed (tests, reload)
        _cache = JWKSCache(
            jwks_url=settings.GATEWAY_OIDC_JWKS_URL,
            issuer=settings.GATEWAY_OIDC_ISSUER,
            ttl=float(settings.GATEWAY_OIDC_JWKS_CACHE_SECONDS),
        )
        _cache._configured = want
    return _cache


def reset_cache() -> None:
    global _cache
    _cache = None


# ---------- Tokens ----------


def looks_like_jwt(token: str) -> bool:
    """Three base64url segments whose first decodes to a JSON header with an "alg". API keys never do."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[0]:
        return False
    try:
        header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
    except ValueError:
        return False
    return isinstance(header, dict) and "alg" in header


async def verify_token(token: str, cache: JWKSCache | None = None) -> dict:
    """Verify an OIDC access token and return its claims. Raises IdentityError."""
    import jwt  # PyJWT, with the cryptography backend

    if len(token) > MAX_TOKEN_BYTES:
        raise IdentityError("token too large")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise IdentityError("malformed token") from e
    alg = header.get("alg")
    if alg not in configured_algorithms():
        raise IdentityError(f"token algorithm {alg!r} is not allowed")
    jwk = await (cache or jwks_cache()).key_for(header.get("kid"), alg)
    try:
        key = jwt.PyJWK(jwk, algorithm=alg)
        return jwt.decode(
            token,
            key.key,
            algorithms=[alg],
            audience=settings.GATEWAY_OIDC_AUDIENCE,
            issuer=settings.GATEWAY_OIDC_ISSUER,
            leeway=settings.GATEWAY_OIDC_CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise IdentityError("token expired") from e
    except jwt.ImmatureSignatureError as e:
        raise IdentityError("token not valid yet") from e
    except jwt.InvalidAudienceError as e:
        raise IdentityError("token audience mismatch") from e
    except jwt.InvalidIssuerError as e:
        raise IdentityError("token issuer mismatch") from e
    except jwt.MissingRequiredClaimError as e:
        raise IdentityError(f"token is missing the {e.claim!r} claim") from e
    except (jwt.PyJWTError, ValueError) as e:  # bad signature, key/alg mismatch, bad encoding
        raise IdentityError("invalid token signature") from e


async def authenticate_token(token: str) -> Principal:
    return principal_for_claims(await verify_token(token))
