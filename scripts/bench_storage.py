# Corey Mathie, 2026
"""
Measure what encryption at rest costs on retrieval, and how long backup and restore take, on a
synthetic collection. Prints JSON. Numbers in docs/operations.md come from this script.

    python scripts/bench_storage.py --docs 500

Uses the demo embedder (no model server needed); timings are medians of repeated runs on this host.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo.engine import DEMO_EMBED_MODEL, DemoBackend  # noqa: E402
from gateway import backends, crypto, rag, store  # noqa: E402
from gateway.config import settings  # noqa: E402
from scripts import backup, restore  # noqa: E402


def synthetic(i: int, paragraphs: int = 4) -> str:
    return "\n\n".join(
        f"Section {j} of policy {i}. " + " ".join(f"term{(i * 7 + j * 13 + k) % 997}" for k in range(120))
        for j in range(paragraphs)
    )


async def _build(tmp: Path, docs: int, encrypt: bool) -> None:
    settings.GATEWAY_DB_PATH = str(tmp / "gw.db")
    settings.GATEWAY_LOG_DIR = str(tmp / "logs")
    settings.GATEWAY_ENCRYPT_AT_REST = encrypt
    settings.GATEWAY_ENCRYPTION_KEY = base64.b64encode(os.urandom(32)).decode()
    crypto.set_provider(None)
    store.init_db()
    rag.init_rag()
    for i in range(docs):
        await rag.add_document("bench", f"d{i}.md", synthetic(i), "bench", "2026-01-01T00:00:00+00:00")


def _median_ms(fn, runs: int) -> float:
    times = []
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t)
    return round(statistics.median(times) * 1000, 1)


def measure(docs: int) -> dict:
    saved = settings.model_dump()
    backends.set_backend(DemoBackend())
    settings.GATEWAY_EMBED_MODEL = DEMO_EMBED_MODEL
    out: dict = {"documents": docs, "cpus": os.cpu_count()}
    try:
        for encrypt in (False, True):
            with tempfile.TemporaryDirectory() as t:
                tmp = Path(t)
                asyncio.run(_build(tmp, docs, encrypt))
                access = rag.unrestricted_access("bench")
                res = {
                    "passages": len(rag.candidates("bench", access)),
                    "db_mb": round(Path(settings.GATEWAY_DB_PATH).stat().st_size / 1e6, 1),
                    "load_passages_ms": _median_ms(lambda a=access: rag.candidates("bench", a), 7),
                    "hybrid_retrieve_ms": _median_ms(
                        lambda a=access: asyncio.run(rag.retrieve("bench", "term5 term77 policy", 4, a)), 5
                    ),
                }
                if encrypt:
                    t0 = time.perf_counter()
                    target = backup.backup(tmp / "bk", Path(settings.GATEWAY_DB_PATH), Path(settings.GATEWAY_LOG_DIR))
                    res["backup_ms"] = round((time.perf_counter() - t0) * 1000)
                    t0 = time.perf_counter()
                    restore.restore(target, tmp / "restored" / "gw.db", tmp / "restored" / "logs")
                    res["restore_ms"] = round((time.perf_counter() - t0) * 1000)
                out["encrypted" if encrypt else "plaintext"] = res
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)
        crypto.set_provider(None)
        backends.set_backend(None)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs", type=int, default=500)
    args = ap.parse_args(argv)
    print(json.dumps(measure(args.docs), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
