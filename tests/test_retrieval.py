# Corey Mathie, 2026
"""Hybrid retrieval (BM25 + vectors fused with RRF), rerankers, retrieval settings, and the RAG eval gate."""

import json

import pytest

from gateway import rag, retrieval
from gateway.config import settings
from scripts import rag_eval

from .conftest import ADMIN, new_key


def test_bm25_prefers_rare_terms_and_scores_no_overlap_as_zero():
    docs = [retrieval.tokenize(t) for t in ("the policy covers travel", "travel policy for VPN tokens", "lunch menu")]
    scores = retrieval.bm25_scores(retrieval.tokenize("VPN policy"), docs)
    assert scores[1] > scores[0] > 0 and scores[2] == 0.0
    assert retrieval.bm25_scores([], docs) == [0.0, 0.0, 0.0]
    assert retrieval.tokenize("The receipts, Receipts!") == ["receipt", "receipt"]


def test_rrf_matches_the_formula_and_rewards_agreement():
    fused = retrieval.rrf([[0, 1, 2], [1, 0]], k=60)
    assert fused[0] == pytest.approx(1 / 61 + 1 / 62)
    assert fused[1] == pytest.approx(1 / 62 + 1 / 61)
    assert fused[2] == pytest.approx(1 / 63)


def test_rank_modes_and_score_details():
    texts = ["hotel cap is $210 per night", "laptops lock after 5 minutes", "per diem is $68"]
    vec = [0.1, 0.9, 0.2]  # a vector ranking that disagrees with the words
    assert [i for i, _ in retrieval.rank("hotel cap per night", texts, vec, mode="vector", top_k=3)] == [1, 2, 0]
    bm = retrieval.rank("hotel cap per night", texts, None, mode="bm25", top_k=1)
    assert bm[0][0] == 0 and set(bm[0][1]) == {"bm25", "final"}
    hy = retrieval.rank("hotel cap per night", texts, vec, mode="hybrid", top_k=3)
    assert set(hy[0][1]) == {"vector", "bm25", "rrf", "final"}
    with pytest.raises(ValueError, match="unknown retrieval mode"):
        retrieval.rank("x", texts, vec, mode="magic")


def test_lexical_reranker_prefers_passages_covering_all_query_terms():
    rr = retrieval.LexicalReranker()
    passages = ["receipts receipts receipts receipts", "itemized receipts are required over $25 for lunch"]
    scores = rr.rerank("receipt for a lunch over $25", passages)
    assert scores[1] > scores[0]
    ranked = retrieval.rank("receipt for lunch", passages, [0.9, 0.1], mode="hybrid", top_k=2, reranker=rr)
    assert ranked[0][0] == 1 and "rerank" in ranked[0][1]


def test_cross_encoder_is_an_explicit_opt_in_with_clear_errors(monkeypatch):
    assert retrieval.get_reranker("none") is None
    with pytest.raises(ValueError, match="GATEWAY_CROSS_ENCODER_MODEL"):
        retrieval.CrossEncoderReranker("")
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", None)  # simulate "not installed"
    with pytest.raises(RuntimeError, match="sentence-transformers"):
        retrieval.CrossEncoderReranker("/models/some-cross-encoder")
    with pytest.raises(ValueError, match="unknown reranker"):
        retrieval.get_reranker("llm-judge")


@pytest.mark.parametrize("mode", ["vector", "bm25", "hybrid"])
def test_ask_uses_the_configured_mode_and_reports_it(client, monkeypatch, mode):
    monkeypatch.setattr(settings, "GATEWAY_RETRIEVAL_MODE", mode)
    for title, text in (
        ("travel.md", "Hotel stays are capped at $210 per night."),
        ("it.md", "Laptops lock after 5 minutes."),
    ):
        client.post("/v1/collections/p/documents/text", json={"title": title, "text": text}, headers=ADMIN)
    embedded = []
    real = rag.embed

    async def spy(texts, model=None):
        embedded.extend(texts)
        return await real(texts, model)

    monkeypatch.setattr(rag, "embed", spy)
    r = client.post(
        "/v1/collections/p/ask",
        json={"question": "hotel cap per night?"},
        headers={"Authorization": f"Bearer {new_key(client)}"},
    )
    body = r.json()
    assert r.status_code == 200 and body["retrieval"] == {"mode": mode, "reranker": "none"}
    assert body["sources"][0]["title"] == "travel.md"
    assert embedded == ([] if mode == "bm25" else ["hotel cap per night?"])  # bm25 never embeds the question
    from gateway import audit

    entry = next(e for e in audit.recent(50) if e["event"] == "document_question")
    assert entry["payload"]["retrieval"]["mode"] == mode


def test_rag_eval_meets_thresholds_and_has_no_acl_leaks():
    report = rag_eval.evaluate()
    assert report["questions"] >= 40 and report["restricted_documents"] == 2
    assert rag_eval.check(report, json.loads(rag_eval.THRESHOLDS.read_text())) == []
    assert all(r["acl_leaks"] == 0 for r in report["results"].values())
    assert settings.GATEWAY_RETRIEVAL_MODE == "hybrid"  # settings restored after the run


def test_rag_eval_gate_fails_on_regressions_and_leaks():
    report = {"results": {"hybrid+none": {"recall@1": 0.5, "acl_leaks": 2}}}
    problems = rag_eval.check(
        report, {"max_acl_leaks": 0, "configs": {"hybrid+none": {"recall@1": 0.9}, "bm25+none": {}}}
    )
    assert problems == [
        "hybrid+none: recall@1 0.5 < 0.9",
        "bm25+none: not evaluated",
        "hybrid+none: 2 restricted passages leaked",
    ]


def test_golden_set_is_well_formed():
    docs, questions, acl = rag_eval.load_golden()
    assert set(acl) <= set(docs)
    for q in questions:
        assert q["doc"] in docs, q["id"]
        assert q["answer_contains"].lower() in docs[q["doc"]].lower(), q["id"]  # the answer is really in the doc
    assert len({q["id"] for q in questions}) == len(questions)


def test_published_eval_numbers_match_a_fresh_run():
    """docs/benchmarks.md and ADR 0006 quote the eval table; it must be exactly what the eval produces now."""
    table = rag_eval.markdown(rag_eval.evaluate()).split("\n\n", 1)[1]
    root = rag_eval.ROOT
    for doc in ("docs/benchmarks.md", "docs/adr/0006-hybrid-retrieval-and-rag-evals.md", "README.md"):
        assert table in (root / doc).read_text(), doc
