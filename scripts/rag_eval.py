# Corey Mathie, 2026
"""
RAG evaluation over the bundled golden set (evals/golden/): fictional documents, questions with the
document that answers them and a phrase the answer must contain, and access lists for two restricted
documents.

    python scripts/rag_eval.py                 # Markdown table for every retrieval configuration
    python scripts/rag_eval.py --json          # machine-readable
    python scripts/rag_eval.py --check         # exit 1 if any gated metric is below evals/thresholds.json
    python scripts/rag_eval.py --console-data  # write demo/data/rag_eval.json for the console's Evals screen

Runs offline and deterministically: it uses the gateway's real ingestion, access control and retrieval
code (gateway/rag.py, gateway/retrieval.py) with the browser demo's embedder (hashed bag-of-words, not a
neural model) and its extractive answerer (no LLM). Numbers therefore measure the retrieval pipeline
with that embedder, not answer quality with a real model.

Metrics, per configuration (retrieval mode + reranker), at k passages:
  recall@1, recall@k   share of questions whose expected document is in the top 1 / top k passages
  mrr                  mean reciprocal rank of the first passage from the expected document (0 if absent)
  citation_accuracy    share of answers that cite at least one source and only sources from the
                       expected document
  answer_contains      share of answers containing the expected phrase (case-insensitive)
  acl_leaks            passages from restricted documents retrieved (top 12) for a caller without
                       access, over every question; must be 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "evals" / "golden"
THRESHOLDS = ROOT / "evals" / "thresholds.json"
CONSOLE_DATA = ROOT / "demo" / "data" / "rag_eval.json"
CONFIGS = [("vector", "none"), ("bm25", "none"), ("hybrid", "none"), ("hybrid", "lexical")]
COLLECTION = "golden"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_golden() -> tuple[dict[str, str], list[dict], dict[str, list[str]]]:
    docs = {p.name: p.read_text() for p in sorted((GOLDEN / "docs").glob("*.md"))}
    questions = [json.loads(line) for line in (GOLDEN / "questions.jsonl").read_text().splitlines() if line.strip()]
    acl = json.loads((GOLDEN / "acl.json").read_text())
    return docs, questions, acl


async def _evaluate(k: int, configs: list[tuple[str, str]]) -> dict:
    from gateway import backends, rag, store
    from gateway.config import settings
    from gateway.identity import Principal

    docs, questions, acl = load_golden()
    store.init_db()
    rag.init_rag()
    for title, text in docs.items():
        await rag.add_document(COLLECTION, title, text, "rag-eval", "2026-01-01T00:00:00+00:00", acl=acl.get(title, []))
    restricted = set(acl)
    insider = Principal("api_key", "eval-insider", "eval-insider", frozenset({"user"}), frozenset({"hr", "security"}))
    outsider = Principal("api_key", "eval-outsider", "eval-outsider", frozenset({"user"}), frozenset())
    backend = backends.get_backend()

    results = {}
    for mode, reranker in configs:
        settings.GATEWAY_RETRIEVAL_MODE = mode
        settings.GATEWAY_RERANKER = reranker
        r1 = rk = rr = cites = contains = leaks = 0
        failures = []
        for q in questions:
            sources = await rag.retrieve(COLLECTION, q["question"], k, rag.access_for(insider, COLLECTION))
            titles = [s.title for s in sources]
            rank = titles.index(q["doc"]) + 1 if q["doc"] in titles else 0
            r1 += rank == 1
            rk += rank > 0
            rr += 1 / rank if rank else 0
            reply = await backend.chat(
                backends.ChatParams(model="eval", messages=rag.build_messages(q["question"], sources))
            )
            cited = {titles[n - 1] for n in rag.cited_numbers(reply.content) if 0 < n <= len(titles)}
            cite_ok = bool(cited) and cited == {q["doc"]}
            has = q["answer_contains"].lower() in reply.content.lower()
            cites += cite_ok
            contains += has
            if not (rank == 1 and cite_ok and has):
                failures.append({"id": q["id"], "rank": rank, "cited": sorted(cited), "contains": has})
            hidden = await rag.retrieve(COLLECTION, q["question"], 12, rag.access_for(outsider, COLLECTION))
            leaks += sum(s.title in restricted for s in hidden)
        n = len(questions)
        results[f"{mode}+{reranker}"] = {
            "mode": mode,
            "reranker": reranker,
            "recall@1": round(r1 / n, 4),
            f"recall@{k}": round(rk / n, 4),
            "mrr": round(rr / n, 4),
            "citation_accuracy": round(cites / n, 4),
            "answer_contains": round(contains / n, 4),
            "acl_leaks": leaks,
            "failures": failures,
        }
    return {
        "k": k,
        "questions": len(questions),
        "documents": len(docs),
        "restricted_documents": len(restricted),
        "embedder": "demo hashed bag-of-words (512-d), not a neural model",
        "answerer": "demo extractive (no LLM)",
        "results": results,
    }


def evaluate(k: int = 4, configs: list[tuple[str, str]] | None = None) -> dict:
    """Run the eval in a throwaway database; restores the gateway settings and backend afterwards."""
    return asyncio.run(evaluate_async(k, configs))


async def evaluate_async(k: int = 4, configs: list[tuple[str, str]] | None = None) -> dict:
    """evaluate() for callers already inside an event loop (the console runs it in the browser)."""
    from demo.engine import DEMO_EMBED_MODEL, DemoBackend
    from gateway import backends
    from gateway.config import settings

    saved = settings.model_dump()
    with tempfile.TemporaryDirectory() as tmp:
        settings.GATEWAY_DB_PATH = str(Path(tmp) / "eval.db")
        settings.GATEWAY_EMBED_MODEL = DEMO_EMBED_MODEL
        backends.set_backend(DemoBackend())
        try:
            return await _evaluate(k, configs or CONFIGS)
        finally:
            for name, value in saved.items():
                setattr(settings, name, value)
            backends.set_backend(None)


def check(report: dict, thresholds: dict) -> list[str]:
    """Gated metrics below their minimum (or leaks above zero)."""
    problems = []
    for config, minimums in thresholds["configs"].items():
        got = report["results"].get(config)
        if got is None:
            problems.append(f"{config}: not evaluated")
            continue
        for metric, minimum in minimums.items():
            if got[metric] < minimum:
                problems.append(f"{config}: {metric} {got[metric]} < {minimum}")
    for config, got in report["results"].items():
        if got["acl_leaks"] > thresholds.get("max_acl_leaks", 0):
            problems.append(f"{config}: {got['acl_leaks']} restricted passages leaked")
    return problems


def markdown(report: dict) -> str:
    k = report["k"]
    lines = [
        f"Golden set: {report['documents']} fictional documents ({report['restricted_documents']} restricted), "
        f"{report['questions']} questions, k={k}. Embedder: {report['embedder']}. Answers: {report['answerer']}.",
        "",
        f"| Configuration | recall@1 | recall@{k} | MRR | citation accuracy | answer contains | ACL leaks |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, r in report["results"].items():
        lines.append(
            f"| `{name}` | {r['recall@1']:.3f} | {r[f'recall@{k}']:.3f} | {r['mrr']:.3f} | "
            f"{r['citation_accuracy']:.3f} | {r['answer_contains']:.3f} | {r['acl_leaks']} |"
        )
    return "\n".join(lines)


def console_data(report: dict) -> dict:
    """What the console's Evals screen shows: this report, the gates, and the files that produced it.

    No timestamp, so the committed file only changes when results change (a test compares it to a fresh run).
    """
    _docs, questions, _acl = load_golden()
    thresholds = json.loads(THRESHOLDS.read_text())
    files = [p.relative_to(ROOT).as_posix() for p in sorted((GOLDEN / "docs").glob("*.md"))]
    return {
        "generated_by": "python scripts/rag_eval.py --console-data",
        "measured": True,
        "report": report,
        "thresholds": thresholds,
        "problems": check(report, thresholds),
        "questions": {q["id"]: {"question": q["question"], "doc": q["doc"]} for q in questions},
        "golden_files": [*files, "evals/golden/questions.jsonl", "evals/golden/acl.json", "evals/thresholds.json"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=4, help="passages retrieved per question (default 4, the API default)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true", help="fail if a metric is below evals/thresholds.json")
    ap.add_argument("--console-data", action="store_true", help=f"write {CONSOLE_DATA.relative_to(ROOT)}")
    args = ap.parse_args(argv)

    report = evaluate(args.k)
    if args.console_data:
        CONSOLE_DATA.parent.mkdir(parents=True, exist_ok=True)
        CONSOLE_DATA.write_text(json.dumps(console_data(report), indent=2) + "\n")
        print(f"wrote {CONSOLE_DATA.relative_to(ROOT)}", file=sys.stderr)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(markdown(report))
    if args.check:
        problems = check(report, json.loads(THRESHOLDS.read_text()))
        if problems:
            print("\nFAILED:", *problems, sep="\n  ", file=sys.stderr)
            return 1
        print("\nAll gated metrics meet evals/thresholds.json.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
