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
    texts = ["hotel cap is $180 per night", "laptops lock after 5 minutes", "meals are $75 per day"]
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
        ("travel.md", "Hotel stays are capped at $180 per night."),
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


@pytest.fixture(scope="module")
def eval_report():
    return rag_eval.evaluate()  # about 10 s: shared by the tests below


def test_rag_eval_meets_thresholds_and_has_no_acl_leaks(eval_report):
    report = eval_report
    assert report["questions"] >= 40 and report["restricted_documents"] == 3 and report["declines"] >= 5
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


def test_golden_set_is_well_formed_and_covers_the_library():
    from demo.engine import PERSONAS, persona_principal

    catalog, texts, questions = rag_eval.load_golden()
    assert len({q["id"] for q in questions}) == len(questions)
    restricted = {d["file"] for d in catalog["documents"] if d["acl"]}
    collection = {d["file"]: d["collection"] for d in catalog["documents"]}
    for q in questions:
        assert q["persona"] in PERSONAS, q["id"]
        readable = rag_eval.readable(catalog, persona_principal(q["persona"]))
        if q["doc"]:
            assert q["answer_contains"].lower() in texts[q["doc"]].lower(), q["id"]  # the answer is really in the doc
            assert q["doc"] in readable, q["id"]  # an answerable question is asked by someone cleared for it
            assert q["question"] not in texts[q["doc"]], q["id"]  # paraphrased, not copied
    answered = {q["doc"] for q in questions if q["doc"]}
    assert {collection[d] for d in answered} == {c["name"] for c in catalog["collections"]}  # every collection
    assert restricted <= answered  # each restricted document, asked by someone with access
    declines = [q for q in questions if not q["doc"]]
    assert {q["persona"] for q in declines} >= {"kiosk", "priya", "dana", "audrey"}


def test_eval_access_oracle_agrees_with_the_personas():
    from demo.engine import persona_principal

    catalog, _texts, _questions = rag_eval.load_golden()
    kiosk = rag_eval.readable(catalog, persona_principal("kiosk"))
    assert kiosk == {d["file"] for d in catalog["documents"] if d["collection"] == "member-info"}
    audrey = rag_eval.readable(catalog, persona_principal("audrey"))
    assert audrey == {d["file"] for d in catalog["documents"] if not d["acl"]}


def test_published_eval_numbers_match_a_fresh_run(eval_report):
    """docs/benchmarks.md, ADR 0006 and the README quote the eval table; it must match a fresh run exactly."""
    table = rag_eval.markdown(eval_report).split("\n\n", 1)[1]
    root = rag_eval.ROOT
    for doc in ("docs/benchmarks.md", "docs/adr/0006-hybrid-retrieval-and-rag-evals.md", "README.md"):
        assert table in (root / doc).read_text(), doc
