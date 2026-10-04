# Corey Mathie, 2026
"""
Restore a backup made by scripts/backup.py, checking it before anything is overwritten.

    python scripts/restore.py /backups/lldk-backup-20261004T120000000000Z            # dry check + restore
    python scripts/restore.py BACKUP --check-only                                    # verify, change nothing
    python scripts/restore.py BACKUP --force          # replace existing files (moved aside, not deleted)

Checks, in order, and stops at the first failure: manifest present and of a known format; every file's
SHA-256 matches the manifest; SQLite integrity_check passes on the backup copy; the audit chain in the
backup verifies; every key-encryption key the backup needs is in the configured keyring (skip with
--skip-key-check when restoring to a machine whose keys arrive later). Then the database is restored
with SQLite's backup API and the audit log copied, and both are verified again in place.

Stop the gateway before restoring. Reads GATEWAY_DB_PATH, GATEWAY_LOG_DIR and the encryption key
settings from the environment or .env, or --db / --log-dir.
"""

from __future__ import annotations

import argparse
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

from gateway import audit, crypto  # noqa: E402
from gateway.config import settings  # noqa: E402
from scripts.backup import FORMAT, online_copy, sha256_file  # noqa: E402


class RestoreError(Exception):
    pass


def check_backup(src: Path, skip_key_check: bool = False) -> dict:
    manifest_path = src / "manifest.json"
    if not manifest_path.exists():
        raise RestoreError(f"no manifest.json in {src}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("format") != FORMAT:
        raise RestoreError(f"unknown backup format {manifest.get('format')!r}")
    for name, meta in manifest["files"].items():
        f = src / name
        if not f.exists():
            raise RestoreError(f"{name} is missing from the backup")
        if sha256_file(f) != meta["sha256"]:
            raise RestoreError(f"{name} does not match its checksum: the backup is corrupt or was altered")
    with closing(sqlite3.connect(src / "gateway.db")) as c:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RestoreError(f"SQLite integrity_check failed on the backup: {integrity}")
    if "audit.jsonl" in manifest["files"]:
        v = audit.verify(src / "audit.jsonl")
        if not v["ok"]:
            raise RestoreError(f"the backup's audit chain is broken at line {v['bad_line']}")
    needed = manifest.get("encryption", {}).get("keys_needed", [])
    if needed and not skip_key_check:
        try:
            ring = crypto.provider()
        except (crypto.CryptoError, OSError, ValueError) as e:
            raise RestoreError(f"the backup needs keys {needed} but no keyring loads: {e}") from e
        available = set(getattr(ring, "keys", {}) or {ring.active_id})
        missing = [k for k in needed if k not in available]
        if missing:
            raise RestoreError(f"the configured keyring lacks keys {missing} needed to read this backup")
    return manifest


def _move_aside(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    aside = path.with_name(f"{path.name}.pre-restore-{stamp}")
    path.rename(aside)
    return aside


def restore(src: Path, db: Path, log_dir: Path, force: bool = False, skip_key_check: bool = False) -> dict:
    manifest = check_backup(src, skip_key_check)
    audit_target = log_dir / "audit.jsonl"
    existing = [p for p in (db, audit_target) if p.exists()]
    if existing and not force:
        raise RestoreError(
            f"refusing to overwrite {', '.join(map(str, existing))}; pass --force (they are moved aside)"
        )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    moved = [str(m) for m in (_move_aside(db, stamp), _move_aside(audit_target, stamp)) if m]
    db.parent.mkdir(parents=True, exist_ok=True)
    online_copy(src / "gateway.db", db)
    if "audit.jsonl" in manifest["files"]:
        log_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / "audit.jsonl", audit_target)
    with closing(sqlite3.connect(db)) as c:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
    after = audit.verify(audit_target) if audit_target.exists() else {"ok": True, "entries": 0, "bad_line": None}
    if integrity != "ok" or not after["ok"]:
        raise RestoreError(f"post-restore check failed: integrity={integrity}, audit={after}")
    return {"restored_from": str(src), "db": str(db), "audit": after, "moved_aside": moved}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("backup", type=Path)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--log-dir", type=Path, default=None)
    ap.add_argument("--force", action="store_true", help="replace existing files (moved aside with a suffix)")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--skip-key-check", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.check_only:
            manifest = check_backup(args.backup, args.skip_key_check)
            print(json.dumps({"backup_ok": True, "created_at": manifest["created_at"]}))
            return 0
        out = restore(
            args.backup,
            args.db or Path(settings.GATEWAY_DB_PATH),
            args.log_dir or Path(settings.GATEWAY_LOG_DIR),
            force=args.force,
            skip_key_check=args.skip_key_check,
        )
    except RestoreError as e:
        print(f"restore refused: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
