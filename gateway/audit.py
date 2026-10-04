# Corey Mathie, 2026
"""
Tamper-evident audit log for the gateway.

Every admin action (key created, key revoked, model pull) is always recorded.
Prompts and completions are recorded when GATEWAY_LOG_PROMPTS=true, optionally
PII-redacted with GATEWAY_REDACT_PROMPTS=true.

Each JSONL entry carries the SHA-256 of the previous entry, so editing or
deleting any line breaks the chain from that point on. `verify()` walks the
file and returns the first bad line. The admin UI shows the result.

With GATEWAY_AUDIT_ENCRYPT_TEXT=true, prompt/response/question/answer
fields are sealed with AES-256-GCM before hashing (gateway/crypto.py), so
verify() needs no key and the file holds no readable prompt text.

Rotation: archive the file to WORM storage (S3 Object Lock, immutable Blob,
etc.) and start a new one. The first entry of the new file records the last
hash of the archived file as an anchor, so the two can be verified together.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import crypto
from .config import settings

GENESIS_HASH = "0" * 64
_lock = threading.Lock()


def _canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _path() -> Path:
    return Path(settings.GATEWAY_LOG_DIR) / "audit.jsonl"


def _tail_hash(path: Path) -> str:
    if not path.exists():
        return GENESIS_HASH
    last = GENESIS_HASH
    with path.open() as f:
        for line in f:
            if line.strip():
                last = json.loads(line)["entry_hash"]
    return last


def append(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Append one entry. Thread-safe within the process."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        prev = _tail_hash(path)
        payload = crypto.protect_payload(payload)  # seals free-text fields if GATEWAY_AUDIT_ENCRYPT_TEXT
        core = {"ts": datetime.now(UTC).isoformat(), "event": event, "payload": payload, "prev_hash": prev}
        entry = {**core, "entry_hash": _sha256(_canonical(core))}
        with path.open("a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    return entry


def verify(path: Path | None = None) -> dict[str, Any]:
    """Walk the chain. Returns {"ok": bool, "entries": int, "bad_line": int | None}."""
    path = path or _path()
    if not path.exists():
        return {"ok": True, "entries": 0, "bad_line": None}
    prev = GENESIS_HASH
    n = 0
    with path.open() as f:
        for i, line in enumerate(f, start=1):
            if not line.strip():
                continue
            n += 1
            try:
                row = json.loads(line)
                core = {k: row[k] for k in ("ts", "event", "payload", "prev_hash")}
            except (ValueError, KeyError):
                return {"ok": False, "entries": n, "bad_line": i}
            if row["prev_hash"] != prev or _sha256(_canonical(core)) != row["entry_hash"]:
                return {"ok": False, "entries": n, "bad_line": i}
            prev = row["entry_hash"]
    return {"ok": True, "entries": n, "bad_line": None}


def recent(limit: int = 50, reveal: bool = True) -> list[dict[str, Any]]:
    """Newest entries first. Sealed fields are opened for display when the key is available."""
    path = _path()
    if not path.exists():
        return []
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    rows = [json.loads(line) for line in lines[-limit:]][::-1]
    if reveal:
        for row in rows:
            if any(crypto.is_sealed(v) for v in row.get("payload", {}).values()):
                row["payload"] = crypto.reveal_payload(row["payload"])
    return rows
