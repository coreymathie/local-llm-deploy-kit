# Corey Mathie, 2026
"""
Ranking for document Q&A: BM25 (pure Python), vector similarity, Reciprocal Rank Fusion, and an
optional reranker. Pure Python apart from the vector scores computed in rag.py, so it also runs in the
browser demo.

Modes (GATEWAY_RETRIEVAL_MODE):
  vector  cosine similarity of embeddings only (the v0.5 behavior)
  bm25    lexical BM25 only (no embedding call for the question)
  hybrid  both rankings fused with Reciprocal Rank Fusion: score(d) = sum over rankings of 1 / (k + rank)

Rerankers (GATEWAY_RERANKER), applied to the top GATEWAY_RERANK_CANDIDATES fused passages:
  none           keep the fused order
  lexical        query-term coverage (IDF-weighted) plus adjacent-term (bigram) matches; no model
  cross_encoder  a local sentence-transformers CrossEncoder (GATEWAY_CROSS_ENCODER_MODEL, a local path).
                 Interface only in this repository: it needs `sentence-transformers` and a model on
                 disk, neither of which is installed or tested here.

Every function receives only passages the caller may read (rag.candidates filters before ranking).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from itertools import pairwise
from typing import Protocol

STOPWORDS = frozenset(
    "a an and are as at be by can do does for from has have how i if in is it its may must of on or our "
    "should that the their there these this to was we what when where which who will with within you your "
    "per any all not no".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase terms without stopwords, with a light plural fold ("receipts" -> "receipt")."""
    out = []
    for w in re.findall(r"[a-z0-9$]+", text.lower()):
        if w in STOPWORDS or len(w) < 2:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def bm25_scores(query: Sequence[str], docs: Sequence[Sequence[str]], k1: float = 1.2, b: float = 0.75) -> list[float]:
    """Okapi BM25 of each tokenized doc for the tokenized query (IDF with the +1 floor, never negative)."""
    n = len(docs)
    if not n or not query:
        return [0.0] * n
    df: Counter = Counter()
    for d in docs:
        df.update(set(d))
    avgdl = sum(len(d) for d in docs) / n or 1.0
    q_terms = Counter(query)
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q_terms if df[t]}
    scores = []
    for d in docs:
        tf = Counter(d)
        norm = k1 * (1 - b + b * len(d) / avgdl)
        s = 0.0
        for t, qtf in q_terms.items():
            f = tf.get(t, 0)
            if f and t in idf:
                s += qtf * idf[t] * f * (k1 + 1) / (f + norm)
        scores.append(s)
    return scores


def ranking(scores: Sequence[float], positive_only: bool = False) -> list[int]:
    """Indices sorted by descending score (stable for ties). BM25 drops zero scores: no term matched."""
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    return [i for i in order if scores[i] > 0] if positive_only else order


def rrf(rankings: Sequence[Sequence[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion (Cormack et al., 2009). Ranks start at 1."""
    fused: dict[int, float] = {}
    for r in rankings:
        for rank, idx in enumerate(r, start=1):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank)
    return fused


class Reranker(Protocol):
    name: str

    def rerank(self, query: str, passages: Sequence[str]) -> list[float]: ...


class LexicalReranker:
    """Query-term coverage weighted by IDF over the candidate set, plus a bonus for matching adjacent terms.

    A cheap, deterministic second stage that favors passages covering *all* of the question's rare terms,
    which first-stage BM25 can under-rank when one frequent term dominates. Not a learned model.
    """

    name = "lexical"

    def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        q = list(dict.fromkeys(tokenize(query)))
        if not q:
            return [0.0] * len(passages)
        toks = [tokenize(p) for p in passages]
        n = len(toks)
        df = Counter(t for d in toks for t in set(d))
        weight = {t: math.log(1 + (n + 1) / (df[t] + 0.5)) for t in q}
        total = sum(weight.values())
        q_bigrams = set(pairwise(q))
        out = []
        for d in toks:
            terms = set(d)
            coverage = sum(weight[t] for t in q if t in terms) / total
            bigrams = set(pairwise(d))
            bonus = 0.25 * len(q_bigrams & bigrams) / len(q_bigrams) if q_bigrams else 0.0
            out.append(coverage + bonus)
        return out


class CrossEncoderReranker:
    """Cross-encoder reranking with a local sentence-transformers model. Not exercised in this repo's tests."""

    name = "cross_encoder"

    def __init__(self, model_path: str):
        if not model_path:
            raise ValueError("GATEWAY_CROSS_ENCODER_MODEL must point at a local cross-encoder model directory")
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            raise RuntimeError("GATEWAY_RERANKER=cross_encoder needs `pip install sentence-transformers`") from e
        self._model = CrossEncoder(model_path, local_files_only=True)

    def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        return [float(s) for s in self._model.predict([(query, p) for p in passages])]


_rerankers: dict[tuple[str, str], Reranker] = {}


def get_reranker(name: str, model_path: str = "") -> Reranker | None:
    if name == "none":
        return None
    key = (name, model_path)
    if key not in _rerankers:
        if name == "lexical":
            _rerankers[key] = LexicalReranker()
        elif name == "cross_encoder":
            _rerankers[key] = CrossEncoderReranker(model_path)
        else:
            raise ValueError(f"unknown reranker {name!r}: none, lexical or cross_encoder")
    return _rerankers[key]


def rank(
    query: str,
    texts: Sequence[str],
    vector_scores: Sequence[float] | None,
    mode: str = "hybrid",
    top_k: int = 4,
    rrf_k: int = 60,
    reranker: Reranker | None = None,
    rerank_candidates: int = 20,
) -> list[tuple[int, dict[str, float]]]:
    """Pick the top_k passages. Returns (index, scores) pairs; scores["final"] is what the order follows.

    `vector_scores` may be None in bm25 mode (the question is not embedded).
    """
    if mode not in {"vector", "bm25", "hybrid"}:
        raise ValueError(f"unknown retrieval mode {mode!r}")
    n = len(texts)
    if not n:
        return []
    bm25 = bm25_scores(tokenize(query), [tokenize(t) for t in texts]) if mode != "vector" else None
    if mode == "vector":
        order = ranking(vector_scores)
        first = {i: float(vector_scores[i]) for i in order}
    elif mode == "bm25":
        order = ranking(bm25)
        first = {i: bm25[i] for i in order}
    else:
        fused = rrf([ranking(vector_scores), ranking(bm25, positive_only=True)], k=rrf_k)
        order = sorted(fused, key=lambda i: -fused[i])
        first = fused

    pool = order[: max(top_k, rerank_candidates)] if reranker else order[:top_k]
    detail = {
        i: {
            **({"vector": round(float(vector_scores[i]), 4)} if vector_scores is not None else {}),
            **({"bm25": round(bm25[i], 4)} if bm25 is not None else {}),
            **({"rrf": round(first[i], 5)} if mode == "hybrid" else {}),
            "final": first[i],
        }
        for i in pool
    }
    if reranker:
        rr = reranker.rerank(query, [texts[i] for i in pool])
        for i, s in zip(pool, rr, strict=True):
            detail[i]["rerank"] = round(s, 4)
            detail[i]["final"] = s
        # Stable: ties keep the first-stage order.
        pool = sorted(pool, key=lambda i: -detail[i]["final"])
    return [(i, detail[i]) for i in pool[:top_k]]
