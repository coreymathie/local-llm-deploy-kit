# Corey Mathie, 2026
"""API-key auth + in-memory rate limiter."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, status

from .config import settings
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


async def require_key(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    key = authorization.split(" ", 1)[1].strip()
    rec = get_active_key(key)
    if not rec:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked key")
    _rate_limit(key)
    return rec


async def require_admin(authorization: str | None = Header(default=None)):
    rec = await require_key(authorization)
    if not rec.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin key required")
    return rec
