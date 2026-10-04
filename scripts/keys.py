# Corey Mathie, 2026
"""
Encryption key management for the gateway's envelope encryption (gateway/crypto.py).

    python scripts/keys.py generate --keyring /etc/lldk/keyring.json --activate   # new KEK (never printed)
    python scripts/keys.py status                                                 # what is encrypted, under which key
    python scripts/keys.py encrypt-existing      # encrypt documents stored before encryption was enabled
    python scripts/keys.py rewrap                # key rotation: re-wrap every document key under the active KEK
    python scripts/keys.py retire --keyring /etc/lldk/keyring.json --id 2026-04

Reads GATEWAY_DB_PATH, GATEWAY_LOG_DIR and GATEWAY_ENCRYPTION_KEY_FILE / GATEWAY_ENCRYPTION_KEY from the
environment or .env, like the gateway. Rotation: generate --activate, restart the gateway (new documents
use the new key), run rewrap, verify with status, then retire the old key once no document, backup or
audit entry you must still read depends on it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import audit, crypto, rag, store  # noqa: E402
from gateway.config import settings  # noqa: E402


def audit_key_ids(path: Path | None = None) -> dict[str, int]:
    """KEK ids referenced by sealed audit fields (those entries stay readable only while the key is kept)."""
    path = path or audit._path()
    counts: dict[str, int] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            for v in json.loads(line).get("payload", {}).values():
                if crypto.is_sealed(v):
                    counts[v["kek"]] = counts.get(v["kek"], 0) + 1
    return counts


def _load_keyring(path: str) -> tuple[dict[str, bytes], str]:
    if not Path(path).exists():
        return {}, ""
    ring = crypto.LocalKeyring.from_file(path)
    return dict(ring.keys), ring.active_id


def cmd_generate(args) -> int:
    keys, active = _load_keyring(args.keyring)
    kid = args.id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if kid in keys:
        print(f"key id {kid!r} already exists", file=sys.stderr)
        return 1
    keys[kid] = crypto.new_key()
    if args.activate or not active:
        active = kid
    crypto.write_keyring(args.keyring, keys, active)
    print(json.dumps({"keyring": args.keyring, "added": kid, "active": active, "keys": sorted(keys)}))
    return 0


def cmd_status(_args) -> int:
    store.init_db()
    rag.init_rag()
    status = rag.encryption_status()
    status["audit_fields_by_key"] = audit_key_ids()
    status["encrypt_at_rest"] = settings.GATEWAY_ENCRYPT_AT_REST
    try:
        status["active_key"] = crypto.provider().active_id
    except (crypto.CryptoError, OSError, ValueError) as e:
        status["active_key"] = None
        status["key_error"] = str(e)
    print(json.dumps(status, indent=2))
    return 0


def cmd_encrypt_existing(_args) -> int:
    store.init_db()
    rag.init_rag()
    n = rag.encrypt_existing()
    audit.append("documents_encrypted", {"documents": n, "key": crypto.provider().active_id, "by": "scripts/keys.py"})
    print(json.dumps({"encrypted_documents": n}))
    return 0


def cmd_rewrap(_args) -> int:
    store.init_db()
    rag.init_rag()
    n = rag.rewrap_keys()
    audit.append(
        "encryption_keys_rewrapped", {"documents": n, "to": crypto.provider().active_id, "by": "scripts/keys.py"}
    )
    print(json.dumps({"rewrapped_documents": n, "active_key": crypto.provider().active_id}))
    return 0


def cmd_retire(args) -> int:
    keys, active = _load_keyring(args.keyring)
    if args.id not in keys:
        print(f"no key {args.id!r} in {args.keyring}", file=sys.stderr)
        return 1
    if args.id == active:
        print("can't retire the active key; generate and activate another first", file=sys.stderr)
        return 1
    store.init_db()
    rag.init_rag()
    docs = rag.encryption_status()["documents_by_key"].get(args.id, 0)
    if docs:
        print(f"{docs} documents still use {args.id!r}; run rewrap first", file=sys.stderr)
        return 1
    sealed = audit_key_ids().get(args.id, 0)
    if sealed and not args.force:
        print(
            f"{sealed} audit fields are sealed with {args.id!r} and become unreadable without it; "
            "archive the key with the audit log, or pass --force",
            file=sys.stderr,
        )
        return 1
    del keys[args.id]
    crypto.write_keyring(args.keyring, keys, active)
    print(json.dumps({"retired": args.id, "keys": sorted(keys)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="add a new random 256-bit KEK to a keyring file (mode 0600)")
    g.add_argument("--keyring", required=True)
    g.add_argument("--id", default="")
    g.add_argument("--activate", action="store_true")
    sub.add_parser("status", help="documents per key, plaintext documents, audit fields per key")
    sub.add_parser("encrypt-existing", help="encrypt documents stored in plaintext")
    sub.add_parser("rewrap", help="re-wrap all document keys under the active KEK")
    r = sub.add_parser("retire", help="remove a KEK from the keyring once nothing needs it")
    r.add_argument("--keyring", required=True)
    r.add_argument("--id", required=True)
    r.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    return {
        "generate": cmd_generate,
        "status": cmd_status,
        "encrypt-existing": cmd_encrypt_existing,
        "rewrap": cmd_rewrap,
        "retire": cmd_retire,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
