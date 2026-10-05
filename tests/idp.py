# Corey Mathie, 2026
"""A fake OIDC identity provider for tests: locally generated RSA/EC keys, a JWKS document, signed tokens.

`FakeIdP.transport()` is an httpx MockTransport serving the discovery document and the JWKS, counting
fetches, so tests can rotate keys and take the provider offline.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

ISSUER = "https://idp.example.test/realms/corp"
AUDIENCE = "llm-gateway"
JWKS_URL = ISSUER + "/protocol/openid-connect/certs"


class FakeIdP:
    def __init__(self, issuer: str = ISSUER):
        self.issuer = issuer
        self.keys: dict[str, tuple[str, object]] = {}  # kid -> (alg, private key)
        self.published: list[str] = []
        self.jwks_fetches = 0
        self.discovery_fetches = 0
        self.online = True
        self.add_key("rsa-1", "RS256")
        self.add_key("ec-1", "ES256")

    def add_key(self, kid: str, alg: str, publish: bool = True) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if alg == "RS256" else None
        if alg == "ES256":
            key = ec.generate_private_key(ec.SECP256R1())
        self.keys[kid] = (alg, key)
        if publish:
            self.published.append(kid)

    def jwks(self) -> dict:
        out = []
        for kid in self.published:
            alg, key = self.keys[kid]
            algo = RSAAlgorithm if alg == "RS256" else ECAlgorithm
            jwk = json.loads(algo.to_jwk(key.public_key()))
            out.append({**jwk, "kid": kid, "alg": alg, "use": "sig"})
        return {"keys": out}

    def token(self, kid: str = "rsa-1", alg: str | None = None, key=None, headers: dict | None = None, **claims) -> str:
        key_alg, private = self.keys[kid]
        now = int(time.time())
        body = {
            "iss": self.issuer,
            "aud": AUDIENCE,
            "sub": claims.pop("sub", "u-" + uuid.uuid4().hex[:8]),
            "iat": now,
            "exp": now + 300,
            **claims,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return jwt.encode(body, key or private, algorithm=alg or key_alg, headers={"kid": kid, **(headers or {})})

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.online:
            raise httpx.ConnectError("identity provider is down", request=request)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            self.discovery_fetches += 1
            return httpx.Response(200, json={"issuer": self.issuer, "jwks_uri": JWKS_URL})
        if str(request.url) == JWKS_URL:
            self.jwks_fetches += 1
            return httpx.Response(200, json=self.jwks())
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
