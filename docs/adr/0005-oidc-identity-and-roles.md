# ADR 0005: OIDC access tokens alongside API keys, with roles from IdP groups

## Status

Accepted, v0.6.0.

## Context

Through v0.5 every caller was an application holding an API key. Regulated deployments also need to know
*which person* asked (HIPAA 164.312(a)(2)(i) unique user identification, SOC 2 CC6.3 role-based access) and to
manage access in the organization's identity provider (Entra ID, Okta, Keycloak, ...) rather than in a second
user database.

## Decision

1. **Resource-server validation only.** The gateway verifies OIDC **access tokens** (JWTs) that clients obtain
   from the IdP; it does not run a login flow itself. Tokens and API keys arrive on the same
   `Authorization: Bearer` header. A bearer value that parses as a JWT is verified as a token when
   `GATEWAY_OIDC_ENABLED=true`; anything else is looked up as an API key (`gateway/auth.py::require_key`).
2. **Strict verification** (`gateway/identity.py::verify_token`, using PyJWT with `cryptography`):
   - algorithm allow-list of RS256 and ES256 only, checked against the token header *before* key lookup:
     `none` and every HMAC algorithm are refused, which closes the RSA-public-key-as-HMAC-secret confusion
     attack; the JWKS entry must have the matching key type (and curve), and an `alg` on the JWK must equal the
     header's;
   - required claims `iss` (exact match), `aud` (must contain the configured audience), `exp`, `sub`; `nbf`
     honored; clock skew configurable (default 60 s, capped at 600 s);
   - tokens over 16 KiB are refused before parsing.
3. **JWKS handling** (`gateway/identity.py::JWKSCache`): discovered from the issuer (the discovery document's
   `issuer` must match) or configured directly; https required unless explicitly allowed; cached for
   `GATEWAY_OIDC_JWKS_CACHE_SECONDS`; an unknown `kid` triggers one refetch, at most every 30 s (key rotation
   works; a stream of invented `kid`s does not hammer the IdP). If the IdP is unreachable, the last good key set
   is used for one more cache period, then requests fail with 503 (fail closed). Before the first successful
   fetch, token requests receive 503, not 401.
4. **Roles from groups.** `GATEWAY_OIDC_GROUP_ROLES` maps IdP groups (claim name configurable, dotted paths for
   nested claims such as Keycloak's `realm_access.roles`) to three role kinds: `admin`, `user`, and
   `reader:<collection>` / `reader:*`. A valid token whose groups map to no role receives 403. API keys keep
   their v0.5 meaning: admin keys have `admin`, other keys `user`.
5. **Privacy in telemetry.** Token callers are labelled `oidc` in Prometheus metrics (never their user name).
   Their user label (`user:<username claim>`) appears in audit entries and `/admin/users`. Rate limits apply
   per token subject (`iss|sub`).

**Implementation and evidence.** `gateway/identity.py` (verification, JWKS cache, roles, collection access
decision), `gateway/auth.py` (`require_key`, `require_user`, `require_admin`), `gateway/main.py::me`,
`admin-ui/index.html` (sign-in with key or token, role-aware cards). Tests: `tests/test_identity.py` (fake IdP
with locally generated RSA and EC keys in `tests/idp.py`, plus a real HTTP JWKS endpoint on 127.0.0.1).
Operator guide: [identity.md](../identity.md).

## Consequences

**Positive**

- Per-person identity and IdP-managed access with no new user store; existing API-key clients are unaffected
  (OIDC is off by default).

**Negative**

- Group changes take effect when the IdP issues the next token (token lifetime is the revocation delay). There
  is no token introspection or revocation-list check.
- The admin page accepts a pasted access token; it does not implement an authorization-code + PKCE login.
  **SCIM provisioning is not implemented** (roadmap): users and groups exist only in tokens.
- Reader-only principals can use document Q&A for their collections but not raw chat, embeddings or the model
  list (`gateway/auth.py::require_user`).

## Alternatives considered

- **Gateway-side login (OIDC client with sessions).** More moving parts (redirects, cookies, CSRF) in a
  component whose clients are mostly programs. Deferred; a reverse proxy such as oauth2-proxy can front the
  admin page today.
- **Token introspection (RFC 7662) on every call.** Immediate revocation, but an IdP round-trip per request and
  an availability dependency. Not chosen; local JWT validation with short-lived tokens instead.
- **Trusting `X-Forwarded-User` headers from a proxy.** Simple, but any path around the proxy becomes an
  impersonation path. Not chosen.
