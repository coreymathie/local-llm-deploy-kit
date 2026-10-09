# Corey Mathie, 2026
"""
Answer-quality evaluation over the console's own sample library: the Cypress Harbor Credit Union documents
(demo/data/library.json, written by scripts/sample_library.py) and a golden set of staff and member questions
(evals/golden/questions.jsonl).

    python scripts/rag_eval.py                 # Markdown table for every retrieval configuration
    python scripts/rag_eval.py --json          # machine-readable
    python scripts/rag_eval.py --check         # exit 1 if any gated metric is below evals/thresholds.json
    python scripts/rag_eval.py --console-data  # write demo/data/rag_eval.json for the console's Evals screen

Each question names the persona who asks it (demo/engine.py's PERSONAS: groups, roles) and either the document
that answers it with a phrase the answer must contain, or no document: a question the assistant should decline,
because nothing in the library answers it or because the answer sits in a document the asker is not cleared for.

Runs offline and deterministically. Ingestion, collection and document access lists, access decisions and
retrieval are the gateway's own code (gateway/rag.py, gateway/identity.py, gateway/retrieval.py); questions go
through the console's "All sources" path (demo/engine.py: retrieve_all, then answer, which declines below the
relevance floor), with the browser demo's embedder (hashed bag-of-words, not a neural model) and its extractive
answerer (no LLM). The numbers measure that pipeline, not a real model's answers.

Metrics, per configuration (retrieval mode + reranker), at k passages:
  recall@1, recall@k   share of answerable questions whose expected document is in the top 1 / top k passages
  mrr                  mean reciprocal rank of the first passage from the expected document (0 if absent)
  citation_accuracy    share of answerable questions whose answer cites at least one source and only sources
                       from the expected document
  answer_contains      share of answerable questions whose answer contains the expected phrase (case-insensitive)
  decline_accuracy     share of decline questions answered with the assistant's "I don't know" and no citation
  acl_leaks            passages retrieved (top 12) from a document the asking persona may not read, counted for
                       every question asked as every persona; must be 0. Readability comes from the catalog's
                       access lists, decided here independently of the gateway's own access code.
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
QUESTIONS = GOLDEN / "questions.jsonl"
LIBRARY = ROOT / "demo" / "data" / "library.json"
THRESHOLDS = ROOT / "evals" / "thresholds.json"
CONSOLE_DATA = ROOT / "demo" / "data" / "rag_eval.json"
CONFIGS = [("vector", "none"), ("bm25", "none"), ("hybrid", "none"), ("hybrid", "lexical")]
LEAK_TOP_K = 12

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_golden() -> tuple[dict, dict[str, str], list[dict]]:
    """The library catalog, every document's text by file name, and the golden questions."""
    catalog = json.loads(LIBRARY.read_text())
    texts = {d["file"]: (ROOT / "demo" / d["path"]).read_text() for d in catalog["documents"]}
    questions = [json.loads(line) for line in QUESTIONS.read_text().splitlines() if line.strip()]
    return catalog, texts, questions


def readable(catalog: dict, principal) -> set[str]:
    """Documents a principal may read, from the catalog's access lists alone (the leak check's oracle).

    Admin reads everything; reader:* or reader:<c> opens a collection; otherwise the collection's list must name
    one of the principal's groups, key or user. A document's own list must also match, except for admins.
    """
    if "admin" in principal.roles:
        return {d["file"] for d in catalog["documents"]}
    names = {f"group:{g}" for g in principal.groups} | {principal.label, f"key:{principal.label}"}
    readers = {r.split(":", 1)[1] for r in principal.roles if r.startswith("reader:")}
    open_cols = {
        c["name"]
        for c in catalog["collections"]
        if "*" in readers or c["name"] in readers or names & set(c.get("acl") or [])
    }
    return {
        d["file"]
        for d in catalog["documents"]
        if d["collection"] in open_cols and (not d["acl"] or names & set(d["acl"]))
    }


async def _evaluate(k: int, configs: list[tuple[str, str]]) -> dict:
    from demo import engine
    from gateway import rag, store

    catalog, texts, questions = load_golden()
    store.init_db()
    rag.init_rag()
    for d in catalog["documents"]:
        await rag.add_document(
            d["collection"], d["file"], texts[d["file"]], "rag-eval", "2026-01-01T00:00:00+00:00", acl=d["acl"]
        )
    for c in catalog["collections"]:
        rag.set_collection_acl(c["name"], c.get("acl") or [], "rag-eval", "2026-01-01T00:00:00+00:00")
    personas = sorted({q["persona"] for q in questions})
    principals = {p: engine.persona_principal(p) for p in personas}
    allowed = {p: readable(catalog, principals[p]) for p in personas}
    restricted = [d["file"] for d in catalog["documents"] if d["acl"]]

    results = {}
    for mode, reranker in configs:
        config = {"mode": mode, "reranker": reranker}
        r1 = rk = rr = cites = contains = declined = leaks = 0
        failures = []
        for q in questions:
            sources, _access, _where = await engine.retrieve_all(principals[q["persona"]], q["question"], k, config)
            titles = [s.title for s in sources]
            reply = (await engine.answer(q["question"], sources)).content if sources else engine.NO_ANSWER
            cited = sorted({titles[n - 1] for n in rag.cited_numbers(reply) if 0 < n <= len(titles)})
            if q["doc"]:
                rank = titles.index(q["doc"]) + 1 if q["doc"] in titles else 0
                cite_ok = bool(cited) and cited == [q["doc"]]
                has = q["answer_contains"].lower() in reply.lower()
                r1 += rank == 1
                rk += rank > 0
                rr += 1 / rank if rank else 0
                cites += cite_ok
                contains += has
                if not (rank == 1 and cite_ok and has):
                    failures.append({"id": q["id"], "rank": rank, "cited": cited, "contains": has})
            else:
                ok = reply == engine.NO_ANSWER and not cited
                declined += ok
                if not ok:
                    failures.append({"id": q["id"], "rank": 0, "cited": cited, "declined": False})
            for p in personas:  # the same question from every persona: nothing it can't read may be retrieved
                hidden, _a, _w = await engine.retrieve_all(principals[p], q["question"], LEAK_TOP_K, config)
                leaks += sum(s.title not in allowed[p] for s in hidden)
        answerable = sum(1 for q in questions if q["doc"]) or 1
        to_decline = sum(1 for q in questions if not q["doc"]) or 1
        results[f"{mode}+{reranker}"] = {
            "mode": mode,
            "reranker": reranker,
            "recall@1": round(r1 / answerable, 4),
            f"recall@{k}": round(rk / answerable, 4),
            "mrr": round(rr / answerable, 4),
            "citation_accuracy": round(cites / answerable, 4),
            "answer_contains": round(contains / answerable, 4),
            "decline_accuracy": round(declined / to_decline, 4),
            "acl_leaks": leaks,
            "failures": failures,
        }
    return {
        "k": k,
        "questions": len(questions),
        "answerable": sum(1 for q in questions if q["doc"]),
        "declines": sum(1 for q in questions if not q["doc"]),
        "documents": len(catalog["documents"]),
        "collections": len(catalog["collections"]),
        "restricted_documents": len(restricted),
        "personas": personas,
        "embedder": "demo hashed bag-of-words (512-d), not a neural model",
        "answerer": "demo extractive (no LLM), with the console's relevance floor",
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
        settings.GATEWAY_COLLECTION_DEFAULT_ACCESS = "open"  # every sample collection has its own list anyway
        settings.GATEWAY_RRF_K = 60
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
        f"Golden set: {report['questions']} questions ({report['answerable']} answerable, {report['declines']} to "
        f"decline) over the Cypress Harbor sample library: {report['documents']} documents in "
        f"{report['collections']} collections ({report['restricted_documents']} restricted), asked as "
        f"{len(report['personas'])} personas, k={k}. Embedder: {report['embedder']}. Answers: {report['answerer']}.",
        "",
        f"| Configuration | recall@1 | recall@{k} | MRR | citation accuracy | answer contains "
        "| decline accuracy | ACL leaks |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, r in report["results"].items():
        lines.append(
            f"| `{name}` | {r['recall@1']:.3f} | {r[f'recall@{k}']:.3f} | {r['mrr']:.3f} | "
            f"{r['citation_accuracy']:.3f} | {r['answer_contains']:.3f} | {r['decline_accuracy']:.3f} | "
            f"{r['acl_leaks']} |"
        )
    return "\n".join(lines)


def console_data(report: dict) -> dict:
    """What the console's Evals screen shows: this report, the gates, and the files that produced it.

    No timestamp, so the committed file only changes when results change (a test compares it to a fresh run).
    """
    catalog, _texts, questions = load_golden()
    thresholds = json.loads(THRESHOLDS.read_text())
    docs = [f"demo/{d['path']}" for d in catalog["documents"]]
    return {
        "generated_by": "python scripts/rag_eval.py --console-data",
        "measured": True,
        "report": report,
        "thresholds": thresholds,
        "problems": check(report, thresholds),
        "questions": {
            q["id"]: {"question": q["question"], "doc": q["doc"], "persona": q["persona"]} for q in questions
        },
        "golden_files": ["demo/data/library.json", *docs, "evals/golden/questions.jsonl", "evals/thresholds.json"],
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
