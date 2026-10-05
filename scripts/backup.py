# Corey Mathie, 2026
"""
Back up the gateway's state: the SQLite database (online, consistent, via SQLite's backup API, safe
while the gateway is running) and the hash-chained audit log, with a manifest of SHA-256 checksums.

    python scripts/backup.py --out /backups            # -> /backups/lldk-backup-<UTC timestamp>/

The backup never contains encryption keys. Documents encrypted at rest stay encrypted in the copy, so
a restore also needs the key-encryption keys listed in the manifest (`encryption.keys_needed`). Store
the keyring separately from the backups (different system, different access), or the encryption adds
nothing for a stolen backup. API keys in the database are stored in plaintext; protect backups like
the live database.

Reads GATEWAY_DB_PATH and GATEWAY_LOG_DIR from the environment or .env, or --db / --log-dir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import audit  # noqa: E402
from gateway.config import settings  # noqa: E402

FORMAT = 1


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def online_copy(src: Path, dst: Path) -> None:
    """Consistent snapshot of a live SQLite database (other connections may keep writing)."""
    with closing(sqlite3.connect(src)) as s, closing(sqlite3.connect(dst)) as d:
        s.backup(d)


def key_ids_in_db(db: Path) -> list[str]:
    with closing(sqlite3.connect(db)) as c:
        try:
            rows = c.execute("SELECT DISTINCT kek_id FROM documents WHERE kek_id IS NOT NULL").fetchall()
        except sqlite3.OperationalError:  # a database without documents or from before v0.6
            rows = []
    return sorted(r[0] for r in rows)


def audit_key_ids(path: Path) -> list[str]:
    from scripts.keys import audit_key_ids as scan

    return sorted(scan(path))


def backup(out_dir: Path, db: Path, log_dir: Path) -> Path:
    if not db.exists():
        raise FileNotFoundError(f"database not found: {db}")
    target = out_dir / ("lldk-backup-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    target.mkdir(parents=True)
    online_copy(db, target / "gateway.db")
    files = ["gateway.db"]
    audit_src = log_dir / "audit.jsonl"
    audit_info = {"present": audit_src.exists()}
    if audit_src.exists():
        # The log is append-only; copying then verifying the copy gives a consistent prefix of the chain.
        shutil.copyfile(audit_src, target / "audit.jsonl")
        files.append("audit.jsonl")
        check = audit.verify(target / "audit.jsonl")
        lines = [ln for ln in (target / "audit.jsonl").read_text().splitlines() if ln.strip()]
        audit_info.update(check, last_hash=json.loads(lines[-1])["entry_hash"] if lines else audit.GENESIS_HASH)
    with closing(sqlite3.connect(target / "gateway.db")) as c:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
    manifest = {
        "format": FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "source": {"db": str(db), "log_dir": str(log_dir)},
        "files": {
            name: {"sha256": sha256_file(target / name), "bytes": (target / name).stat().st_size} for name in files
        },
        "sqlite_integrity": integrity,
        "audit": audit_info,
        "encryption": {
            "keys_needed": sorted(
                set(key_ids_in_db(target / "gateway.db")) | set(audit_key_ids(target / "audit.jsonl"))
            ),
            "keyring_included": False,
        },
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="folder that receives a timestamped backup folder")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--log-dir", type=Path, default=None)
    args = ap.parse_args(argv)
    target = backup(args.out, args.db or Path(settings.GATEWAY_DB_PATH), args.log_dir or Path(settings.GATEWAY_LOG_DIR))
    manifest = json.loads((target / "manifest.json").read_text())
    print(
        json.dumps(
            {
                "backup": str(target),
                "files": manifest["files"],
                "audit": manifest["audit"],
                "keys_needed": manifest["encryption"]["keys_needed"],
            },
            indent=2,
        )
    )
    if manifest["audit"].get("ok") is False:
        print("warning: the audit log was already broken when backed up", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
