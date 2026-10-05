# Corey Mathie, 2026
"""
OIDC/JWT authentication alongside API keys: JWKS verification (cache, rotation, outage), the algorithm
allow-list, audience/issuer/expiry/clock-skew checks, and groups -> roles on the admin API and on
collections. Keys are generated locally; the identity provider is faked (tests/idp.py), plus one test
against a real HTTP JWKS endpoint on 127.0.0.1.
"""

import asyncio
import base64
import hashlib
import hmac
import http.server
import json
import threading
import time

import pytest
from cryptography.hazmat.primitives import serialization

from gateway import auth, identity
from gateway.config import settings

from .conftest import ADMIN, new_key
from .idp import AUDIENCE, ISSUER, JWKS_URL, FakeIdP

ROLES = {"llm-admins": ["admin"], "staff": ["user"], "hr-team": ["reader:hr"]}


@pytest.fixture
def idp(client, monkeypatch):
    fake = FakeIdP()
    monkeypatch.setattr(settings, "GATEWAY_OIDC_ENABLED", True)
    monkeypatch.setattr(settings, "GATEWAY_OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(settings, "GATEWAY_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setattr(settings, "GATEWAY_OIDC_JWKS_URL", "")  # use discovery
    monkeypatch.setattr(settings, "GATEWAY_OIDC_GROUP_ROLES", ROLES)
    monkeypatch.setattr(identity, "HTTP_TRANSPORT", fake.transport())
    identity.reset_cache()
    identity.validate_settings()
    yield fake
    identity.reset_cache()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unsigned(header: dict, claims: dict, signature: bytes = b"") -> str:
    return f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}.{b64(signature)}"


CHAT = {"messages": [{"role": "user", "content": "hi"}]}


def test_rs256_and_es256_tokens_map_groups_to_roles(client, idp):
    alice = idp.token("rsa-1", sub="alice", groups=["staff"])
    me = client.get("/v1/me", headers=bearer(alice)).json()
    assert me["kind"] == "oidc" and me["label"] == "user:alice"
    assert me["roles"] == ["user"] and me["groups"] == ["staff"]
    assert client.post("/v1/chat/completions", json=CHAT, headers=bearer(alice)).status_code == 200

    root = idp.token("ec-1", sub="root", groups=["llm-admins", "staff"])
    assert client.get("/v1/me", headers=bearer(root)).json()["roles"] == ["admin", "user"]
    assert idp.discovery_fetches == 1 and idp.jwks_fetches == 1  # discovered once, keys cached


def test_admin_api_respects_roles_and_audits_the_user(client, idp):
    staff = idp.token(sub="alice", groups=["staff"])
    admin = idp.token("ec-1", sub="root", groups=["llm-admins"])
    assert client.get("/admin/keys", headers=bearer(staff)).status_code == 403
    assert client.get("/admin/audit/verify", headers=bearer(staff)).status_code == 403
    assert client.get("/admin/keys", headers=bearer(admin)).status_code == 200
    r = client.post("/admin/keys", json={"label": "made-by-sso"}, headers=bearer(admin))
    assert r.status_code == 200
    entry = next(e for e in client.get("/admin/audit/recent", headers=ADMIN).json() if e["event"] == "key_created")
    assert entry["payload"]["by"] == "user:root"


def test_reader_role_is_limited_to_its_collection(client, idp):
    for collection in ("hr", "eng"):
        r = client.post(
            f"/v1/collections/{collection}/documents/text",
            json={"title": f"{collection}.md", "text": f"The {collection} handbook says hello."},
            headers=ADMIN,
        )
        assert r.status_code == 201
    reader = bearer(idp.token(sub="hana", groups=["hr-team"]))
    assert [c["name"] for c in client.get("/v1/collections", headers=reader).json()] == ["hr"]
    assert client.get("/v1/me", headers=reader).json()["collections"] == ["hr"]
    assert client.get("/v1/collections/eng/documents", headers=reader).json() == []
    assert len(client.get("/v1/collections/hr/documents", headers=reader).json()) == 1
    assert client.post("/v1/collections/hr/ask", json={"question": "hello?"}, headers=reader).status_code == 200
    denied = client.post("/v1/collections/eng/ask", json={"question": "hello?"}, headers=reader)
    unknown = client.post("/v1/collections/nope/ask", json={"question": "hello?"}, headers=reader)
    assert denied.status_code == unknown.status_code == 404  # same answer as a missing collection
    assert denied.json()["detail"].replace("eng", "X") == unknown.json()["detail"].replace("nope", "X")
    assert client.post("/v1/chat/completions", json=CHAT, headers=reader).status_code == 403
    assert client.get("/v1/models", headers=reader).status_code == 403
    assert client.post("/v1/embeddings", json={"input": "x"}, headers=reader).status_code == 403


def test_valid_token_without_a_mapped_group_is_forbidden(client, idp):
    r = client.get("/v1/me", headers=bearer(idp.token(groups=["contractors"])))
    assert r.status_code == 403 and "role" in r.json()["detail"]
    assert client.get("/v1/me", headers=bearer(idp.token(groups=None))).status_code == 403


def test_expiry_not_before_and_clock_skew(client, idp):
    now = int(time.time())
    within_skew = idp.token(groups=["staff"], exp=now - 30)  # default skew is 60 s
    assert client.get("/v1/me", headers=bearer(within_skew)).status_code == 200
    expired = client.get("/v1/me", headers=bearer(idp.token(groups=["staff"], exp=now - 120)))
    assert expired.status_code == 401 and expired.json()["detail"] == "token expired"
    assert expired.headers["www-authenticate"].startswith("Bearer")
    early = client.get("/v1/me", headers=bearer(idp.token(groups=["staff"], nbf=now + 120)))
    assert early.status_code == 401 and early.json()["detail"] == "token not valid yet"
    assert client.get("/v1/me", headers=bearer(idp.token(groups=["staff"], nbf=now + 30))).status_code == 200


def test_audience_issuer_and_required_claims(client, idp):
    cases = {
        "token audience mismatch": idp.token(groups=["staff"], aud="some-other-app"),
        "token issuer mismatch": idp.token(groups=["staff"], iss="https://evil.example.test"),
        "token is missing the 'exp' claim": idp.token(groups=["staff"], exp=None),
        "token is missing the 'aud' claim": idp.token(groups=["staff"], aud=None),
    }
    for detail, token in cases.items():
        r = client.get("/v1/me", headers=bearer(token))
        assert (r.status_code, r.json()["detail"]) == (401, detail)


def test_algorithm_allow_list_blocks_none_hmac_confusion_and_other_algs(client, idp, monkeypatch):
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "mallory", "exp": int(time.time()) + 300, "groups": ["llm-admins"]}
    alg_none = unsigned({"alg": "none", "kid": "rsa-1"}, claims)
    # Classic key confusion: HMAC-sign with the RSA *public* key as the secret.
    public_pem = (
        idp.keys["rsa-1"][1]
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    head_body = unsigned({"alg": "HS256", "kid": "rsa-1", "typ": "JWT"}, claims).rsplit(".", 1)[0]
    hs256 = head_body + "." + b64(hmac.new(public_pem, head_body.encode(), hashlib.sha256).digest())
    rs384 = idp.token("rsa-1", alg="RS384", **{k: v for k, v in claims.items() if k not in {"iss", "aud"}})
    for token, alg in ((alg_none, "none"), (hs256, "HS256"), (rs384, "RS384")):
        r = client.get("/admin/keys", headers=bearer(token))
        assert (r.status_code, r.json()["detail"]) == (401, f"token algorithm {alg!r} is not allowed")

    # An ES256 header pointing at an RSA key id is not matched to that key.
    mismatch = client.get("/v1/me", headers=bearer(idp.token("ec-1", headers={"kid": "rsa-1"}, groups=["staff"])))
    assert mismatch.status_code == 401
    # Narrowing the allow-list to RS256 rejects ES256 tokens.
    monkeypatch.setattr(settings, "GATEWAY_OIDC_ALGORITHMS", "RS256")
    assert client.get("/v1/me", headers=bearer(idp.token("ec-1", groups=["staff"]))).status_code == 401
    assert client.get("/v1/me", headers=bearer(idp.token("rsa-1", groups=["staff"]))).status_code == 200


def test_forged_and_tampered_tokens_are_rejected(client, idp):
    idp.add_key("attacker", "RS256", publish=False)
    forged = idp.token("rsa-1", key=idp.keys["attacker"][1], groups=["llm-admins"])
    r = client.get("/admin/keys", headers=bearer(forged))
    assert (r.status_code, r.json()["detail"]) == (401, "invalid token signature")

    good = idp.token(sub="alice", groups=["staff"])
    head, body, sig = good.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["groups"] = ["llm-admins"]
    tampered = f"{head}.{b64(json.dumps(claims).encode())}.{sig}"
    assert client.get("/admin/keys", headers=bearer(tampered)).status_code == 401


def test_jwks_cache_key_rotation_and_unknown_kid_throttling(idp):
    clock = [0.0]
    cache = identity.JWKSCache(jwks_url=JWKS_URL, issuer=ISSUER, ttl=3600, clock=lambda: clock[0])
    verify = lambda token: asyncio.run(identity.verify_token(token, cache=cache))  # noqa: E731

    assert verify(idp.token("rsa-1"))["iss"] == ISSUER
    verify(idp.token("rsa-1"))
    assert cache.fetch_count == 1  # cached

    idp.add_key("rsa-2", "RS256")  # the provider rotates: publishes a new key and starts using it
    clock[0] = 10.0  # too soon after the last fetch: an unknown kid doesn't trigger another fetch yet
    with pytest.raises(identity.IdentityError, match="unknown key"):
        verify(idp.token("rsa-2"))
    assert cache.fetch_count == 1
    clock[0] = 40.0
    verify(idp.token("rsa-2"))  # refetched on the unknown kid, accepted
    assert cache.fetch_count == 2

    idp.published.remove("rsa-1")  # the old key is retired
    clock[0] = 100.0
    with pytest.raises(identity.IdentityError, match="unknown key"):
        verify(_forged(idp))
    assert cache.fetch_count == 3  # one refetch for the unknown kid...
    for _ in range(5):
        with pytest.raises(identity.IdentityError, match="unknown key"):
            verify(idp.token("rsa-1"))  # ...and the retired key fails without hammering the provider
    assert cache.fetch_count == 3

    clock[0] = 100.0 + 3601  # TTL expiry: next verification refreshes
    verify(idp.token("rsa-2"))
    assert cache.fetch_count == 4


def _forged(idp: FakeIdP) -> str:
    idp.add_key("made-up", "RS256", publish=False)
    return idp.token("made-up")


def test_identity_provider_outage_uses_cached_keys_then_fails_closed(idp):
    clock = [0.0]
    cache = identity.JWKSCache(jwks_url=JWKS_URL, issuer=ISSUER, ttl=3600, clock=lambda: clock[0])
    token = idp.token("rsa-1")
    asyncio.run(identity.verify_token(token, cache=cache))
    idp.online = False
    clock[0] = 3601.0  # stale, refresh fails: the last good keys keep working for one more TTL
    assert asyncio.run(identity.verify_token(token, cache=cache))["sub"]
    clock[0] = 7201.0
    with pytest.raises(identity.IdentityUnavailable):
        asyncio.run(identity.verify_token(token, cache=cache))


def test_provider_down_at_first_use_is_503_not_401(client, idp):
    idp.online = False
    r = client.get("/v1/me", headers=bearer(idp.token(groups=["staff"])))
    assert r.status_code == 503


def test_discovery_must_name_the_configured_issuer(client, idp, monkeypatch):
    impostor = FakeIdP(issuer="https://other-idp.example.test")
    monkeypatch.setattr(identity, "HTTP_TRANSPORT", impostor.transport())
    identity.reset_cache()
    r = client.get("/v1/me", headers=bearer(idp.token(groups=["staff"])))
    assert r.status_code == 503 and "issuer" in r.json()["detail"]


def test_api_keys_keep_working_and_tokens_are_ignored_when_oidc_is_off(client, idp, monkeypatch):
    key = new_key(client, label="billing-app")
    assert client.get("/v1/me", headers=bearer(key)).json() == {
        "kind": "api_key",
        "label": "billing-app",
        "roles": ["user"],
        "groups": [],
        "collections": [],
    }
    assert client.get("/v1/me", headers=ADMIN).json()["roles"] == ["admin"]
    monkeypatch.setattr(settings, "GATEWAY_OIDC_ENABLED", False)
    r = client.get("/v1/me", headers=bearer(idp.token(groups=["llm-admins"])))
    assert (r.status_code, r.json()["detail"]) == (401, "invalid or revoked key")


def test_malformed_and_oversized_tokens(client, idp):
    assert client.get("/v1/me", headers=bearer("a.b.c")).status_code == 401  # not a JWT: looked up as a key
    huge = idp.token(groups=["staff"], padding="x" * 20_000)
    r = client.get("/v1/me", headers=bearer(huge))
    assert (r.status_code, r.json()["detail"]) == (401, "token too large")


def test_nested_and_string_group_claims(client, idp, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_OIDC_GROUPS_CLAIM", "realm_access.roles")
    t = idp.token(sub="kc-user", realm_access={"roles": ["staff", "offline_access"]})
    assert client.get("/v1/me", headers=bearer(t)).json()["roles"] == ["user"]
    monkeypatch.setattr(settings, "GATEWAY_OIDC_GROUPS_CLAIM", "scp")
    monkeypatch.setattr(settings, "GATEWAY_OIDC_USERNAME_CLAIM", "preferred_username")
    t = idp.token(sub="x1", preferred_username="dana", scp="hr-team staff")
    me = client.get("/v1/me", headers=bearer(t)).json()
    assert me["label"] == "user:dana" and me["roles"] == ["reader:hr", "user"]


def test_token_users_are_metered_without_user_names_in_metrics(client, idp):
    alice = bearer(idp.token(sub="alice", groups=["staff"]))
    assert client.post("/v1/chat/completions", json=CHAT, headers=alice).status_code == 200
    body = client.get("/metrics").text
    assert 'key="oidc"' in body and "alice" not in body
    users = client.get("/admin/users", headers=ADMIN).json()
    assert users[0]["label"] == "user:alice" and users[0]["requests_total"] == 1 and users[0]["tokens_total"] == 12


def test_rate_limit_applies_per_token_subject(client, idp, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_RATE_LIMIT_PER_MIN", 2)
    auth._calls.clear()
    alice = bearer(idp.token(sub="alice", groups=["staff"]))
    bob = bearer(idp.token(sub="bob", groups=["staff"]))
    assert [client.get("/v1/me", headers=alice).status_code for _ in range(3)] == [200, 200, 429]
    assert client.get("/v1/me", headers=bob).status_code == 200


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("GATEWAY_OIDC_ALGORITHMS", "HS256", "may only list"),
        ("GATEWAY_OIDC_ALGORITHMS", "RS256,none", "may only list"),
        ("GATEWAY_OIDC_AUDIENCE", "", "are required"),
        ("GATEWAY_OIDC_ISSUER", "http://idp.internal", "https"),
        ("GATEWAY_OIDC_GROUP_ROLES", {"x": ["superuser"]}, "unknown role"),
        ("GATEWAY_OIDC_CLOCK_SKEW_SECONDS", 3600, "between 0 and 600"),
    ],
)
def test_unsafe_oidc_settings_fail_at_startup(idp, monkeypatch, field, value, message):
    monkeypatch.setattr(settings, field, value)
    with pytest.raises(ValueError, match=message):
        identity.validate_settings()


def test_real_http_jwks_endpoint_on_localhost(idp, monkeypatch):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(idp.jwks()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/jwks.json"
        monkeypatch.setattr(identity, "HTTP_TRANSPORT", None)  # real httpx client, real socket
        monkeypatch.setattr(settings, "GATEWAY_OIDC_ALLOW_HTTP", True)
        monkeypatch.setattr(settings, "GATEWAY_OIDC_JWKS_URL", url)
        identity.validate_settings()
        identity.reset_cache()
        claims = asyncio.run(identity.verify_token(idp.token("ec-1", sub="erin")))
        assert claims["sub"] == "erin"
    finally:
        server.shutdown()
