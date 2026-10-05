# Corey Mathie, 2026
"""Bearer authentication (API keys and, if enabled, OIDC JWTs), role checks, in-memory rate limiter.

Every dependency returns a `gateway.identity.Principal`. The caller's metrics label (an API key's
label, or the constant "oidc" for token callers) is stored on the request so the metrics middleware
can count requests, including rejected ones, without putting secrets or user names in label values.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request, status

from . import identity
from .config import settings
from .identity import Principal
from .store import get_active_key

_WINDOW_SEC = 60.0
_calls: dict[str, deque[float]] = defaultdict(deque)


def _rate_limit(key: str) -> None:
    now = time.monotonic()
    q = _calls[key]
    while q and q[0] < now - _WINDOW_SEC:
        q.popleft()
    if len(q) >= settings.GATEWAY_RATE_LIMIT_PER_MIN:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"rate limit exceeded ({settings.GATEWAY_RATE_LIMIT_PER_MIN}/min)",
        )
    q.append(now)


def _set_label(request: Request, label: str) -> None:
    try:
        request.state.key_label = label
    except AttributeError:  # pragma: no cover - plain objects in the browser demo
        pass


async def require_key(request: Request, authorization: str | None = Header(default=None)) -> Principal:
    """Any authenticated caller with at least one role. (Name kept from v0.5: it now accepts tokens too.)"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if settings.GATEWAY_OIDC_ENABLED and identity.looks_like_jwt(token):
        _set_label(request, identity.OIDC_METRICS_LABEL)
        try:
            principal = await identity.authenticate_token(token)
        except identity.IdentityError as e:
            headers = {"WWW-Authenticate": 'Bearer error="invalid_token"'} if e.status_code == 401 else None
            raise HTTPException(e.status_code, str(e), headers=headers) from e
        if not principal.roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "token is valid but none of its groups has a role here")
        _rate_limit("oidc:" + principal.subject)
        return principal
    rec = get_active_key(token)
    if not rec:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked key")
    _set_label(request, rec.label)
    _rate_limit(token)
    return identity.principal_for_key(rec)


async def require_user(request: Request, authorization: str | None = Header(default=None)) -> Principal:
    """Chat, embeddings and the model list: the user or admin role (collection readers are not enough)."""
    principal = await require_key(request, authorization)
    if not principal.is_user:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the user role is required")
    return principal


async def require_admin(request: Request, authorization: str | None = Header(default=None)) -> Principal:
    principal = await require_key(request, authorization)
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin key required")
    return principal
