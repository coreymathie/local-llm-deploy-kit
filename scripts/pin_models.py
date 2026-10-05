# Corey Mathie, 2026
"""
Write or update the model lock file from what the backend serves now.

    python scripts/pin_models.py --out models.lock.json                      # Ollama: pin served digests
    python scripts/pin_models.py --out models.lock.json \\
        --file Qwen/Qwen2.5-7B-Instruct=/models/qwen/model-00001-of-00004.safetensors   # openai_compatible

This is trust on first use: it records whatever is served at this moment. Compare the digests with the
publisher's (for example the registry manifest or the model repository's file hashes) before relying on
them, and commit the lock file so changes are reviewed. Existing entries keep their source, license
and purpose; their digests are replaced only with --update.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import backends, supply_chain  # noqa: E402


async def pin(out: Path, files: list[str], update: bool) -> dict:
    existing = {p.name: p for p in supply_chain.load_lock(out)} if out.exists() else {}
    backend = backends.get_backend()
    try:
        served = await supply_chain.served_models(backend)
    finally:
        await backends.aclose_clients()
    by_model: dict[str, list[str]] = {}
    for spec in files:
        name, _, path = spec.partition("=")
        by_model.setdefault(name, []).append(path)
    entries = []
    names = sorted(set(served) | set(existing) | set(by_model))
    for name in names:
        old = existing.get(name)
        entry = {"name": name, "backend": old.backend if old else backend.name}
        for k in ("source", "license", "purpose"):
            if old and getattr(old, k):
                entry[k] = getattr(old, k)
        digest = (served.get(name) or {}).get("digest", "")
        if old and old.digest and not update:
            entry["digest"] = "sha256:" + old.digest
        elif digest:
            entry["digest"] = "sha256:" + supply_chain._norm_digest(digest)
        paths = by_model.get(name)
        if paths:
            entry["files"] = [{"path": p, "sha256": supply_chain.sha256_file(p)} for p in paths]
        elif old and old.files:
            entry["files"] = old.files
        entries.append(entry)
    doc = {"version": 1, "models": entries}
    out.write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--file", action="append", default=[], metavar="MODEL=PATH", help="weight file to hash and pin")
    ap.add_argument("--update", action="store_true", help="replace existing digests with what is served now")
    args = ap.parse_args(argv)
    doc = asyncio.run(pin(args.out, args.file, args.update))
    print(json.dumps({"lock_file": str(args.out), "models": [m["name"] for m in doc["models"]]}))
    print("Trust on first use: check these digests against the publisher before relying on them.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
