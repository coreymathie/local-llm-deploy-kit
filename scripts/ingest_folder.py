# Corey Mathie, 2026
"""
Load a folder of documents into a gateway collection.

    python scripts/ingest_folder.py ./handbook --collection handbook
    python scripts/ingest_folder.py ./policies --collection policies --dry-run

Walks the folder, uploads every supported file (.pdf, .txt, .md, .csv, .json,
.html), and skips files whose relative path is already a document title in the
collection, so re-running it only adds what's new. Use --replace to re-upload
files that changed.

The admin key is read from GATEWAY_ADMIN_KEY (or --key). The gateway does the
parsing and embedding, so this script only needs httpx.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

SUPPORTED = {".pdf", ".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm"}


def find_files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED and not any(part.startswith(".") for part in p.parts)
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--collection", required=True, help="lowercase letters, digits, - and _")
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_URL", "http://localhost:8080"))
    ap.add_argument("--key", default=os.environ.get("GATEWAY_ADMIN_KEY", ""))
    ap.add_argument("--replace", action="store_true", help="re-upload files whose title already exists")
    ap.add_argument("--dry-run", action="store_true", help="list what would be uploaded and stop")
    args = ap.parse_args(argv)

    if not args.folder.is_dir():
        print(f"not a folder: {args.folder}", file=sys.stderr)
        return 2
    files = find_files(args.folder)
    if not files:
        print("no supported files found")
        return 0
    if args.dry_run:
        for f in files:
            print(f"would upload {f.relative_to(args.folder).as_posix()} ({f.stat().st_size:,} bytes)")
        return 0
    if not args.key:
        print("set GATEWAY_ADMIN_KEY or pass --key", file=sys.stderr)
        return 2

    base = f"{args.gateway.rstrip('/')}/v1/collections/{args.collection}/documents"
    headers = {"Authorization": f"Bearer {args.key}"}
    added = skipped = failed = 0
    with httpx.Client(timeout=300.0, headers=headers) as client:
        r = client.get(base)
        if r.status_code != 200:
            print(f"can't read collection: {r.status_code} {r.text}", file=sys.stderr)
            return 1
        existing = {d["title"]: d["id"] for d in r.json()}

        for f in files:
            title = f.relative_to(args.folder).as_posix()
            if title in existing:
                if not args.replace:
                    skipped += 1
                    continue
                client.delete(f"{base}/{existing[title]}")
            with f.open("rb") as fh:
                r = client.post(base, files={"file": (title, fh)})
            if r.status_code == 201:
                added += 1
                print(f"added    {title}  ({r.json()['chunks']} passages)")
            else:
                failed += 1
                is_json = r.headers.get("content-type", "").startswith("application/json")
                detail = r.json().get("detail", r.text) if is_json else r.text
                print(f"FAILED   {title}  {r.status_code}: {detail}", file=sys.stderr)

    print(f"\n{added} added, {skipped} already present, {failed} failed -> collection '{args.collection}'")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
