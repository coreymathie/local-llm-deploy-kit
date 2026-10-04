# Corey Mathie, 2026
"""SQLite-backed persistence for API keys + usage counters."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config import settings
from .models import ApiKey

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    requests_total INTEGER NOT NULL DEFAULT 0,
    tokens_total INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(revoked_at);
"""


def _conn() -> sqlite3.Connection:
    Path(settings.GATEWAY_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(settings.GATEWAY_DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        c.commit()


def _row_to_key(row: sqlite3.Row) -> ApiKey:
    return ApiKey(
        key=row["key"],
        label=row["label"],
        is_admin=bool(row["is_admin"]),
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
        requests_total=row["requests_total"],
        tokens_total=row["tokens_total"],
    )


def create_key(label: str, is_admin: bool = False, key: str | None = None) -> ApiKey:
    key = key or ("sk-local-" + secrets.token_urlsafe(32))
    created = datetime.now(UTC).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO api_keys (key, label, is_admin, created_at) VALUES (?, ?, ?, ?)",
            (key, label, int(is_admin), created),
        )
        c.commit()
    return ApiKey(key=key, label=label, is_admin=is_admin, created_at=created)


def list_keys() -> list[ApiKey]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
    return [_row_to_key(r) for r in rows]


def get_active_key(key: str) -> ApiKey | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM api_keys WHERE key = ? AND revoked_at IS NULL",
            (key,),
        ).fetchone()
    return _row_to_key(row) if row else None


def revoke_key(key: str) -> None:
    revoked = datetime.now(UTC).isoformat()
    with _conn() as c:
        c.execute("UPDATE api_keys SET revoked_at = ? WHERE key = ?", (revoked, key))
        c.commit()


def record_usage(key: str, tokens: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE api_keys SET requests_total = requests_total + 1, tokens_total = tokens_total + ? WHERE key = ?",
            (tokens, key),
        )
        c.commit()


def ensure_bootstrap_admin() -> str | None:
    """Create the first admin key if none exists. Returns the key value if newly created."""
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM api_keys WHERE is_admin = 1").fetchone()[0]
    if n > 0:
        return None
    k = settings.GATEWAY_ADMIN_BOOTSTRAP_KEY or None
    created = create_key("bootstrap admin", is_admin=True, key=k)
    return created.key
