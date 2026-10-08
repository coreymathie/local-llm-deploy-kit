# ADR 0006: Hybrid retrieval (BM25 + vectors, RRF), optional reranking, and a RAG eval gate in CI

Status: Accepted, v0.6.0 (extends ADR 0002)

## Context

ADR 0002 chose exact cosine search over local embeddings and listed two costs: dense-only retrieval
misses exact identifiers that keyword search catches, and there was no retrieval evaluation, so any
change to chunking, embedding or ranking was unmeasured.

## Decision

1. **Hybrid by default.** `gateway/retrieval.py` computes BM25 (pure Python, over the passages the
   caller may read) and the cosine ranking, and fuses them with Reciprocal Rank Fusion
   (`1 / (k + rank)`, `k = 60`). RRF needs no score normalization between two incomparable scales.
   `GATEWAY_RETRIEVAL_MODE=vector` restores the v0.5 behavior; `bm25` skips the question embedding.
2. **Pluggable second stage.** A `Reranker` interface (`rerank(query, passages) -> scores`) over the top
   `GATEWAY_RERANK_CANDIDATES` fused passages. Shipped: `lexical` (IDF-weighted query-term coverage plus
   bigram matches; deterministic, no model). `cross_encoder` loads a local sentence-transformers
   CrossEncoder from a path; it is **interface only here**: the package and a model were not available
   offline in the build environment, so it is untested. Default: `none`.
3. **Access control first.** Ranking only ever sees passages that `rag.access_for` admitted (ADR 0005,
   OWASP LLM08); BM25 statistics are therefore computed per caller, so term frequencies of hidden
   documents can't influence scores either.
4. **Evals gate CI.** `scripts/rag_eval.py --check` runs the real ingestion, access and retrieval code
   over a bundled golden set (`evals/golden/`: fictional documents, questions, expected document and
   answer phrase, two restricted documents) with the demo's deterministic embedder and extractive
   answerer, so it runs offline in seconds. It fails if recall@1, recall@k, MRR, citation accuracy or
   answer-contains drop below `evals/thresholds.json`, or if any restricted passage is retrieved for a
   caller without access.

## Measured (k = 4; demo hashed bag-of-words embedder, not a neural model; 43 questions, 12 documents)

| Configuration | recall@1 | recall@4 | MRR | citation accuracy | answer contains | ACL leaks |
|---|---:|---:|---:|---:|---:|---:|
| `vector+none` | 0.907 | 0.977 | 0.942 | 0.907 | 0.954 | 0 |
| `bm25+none` | 1.000 | 1.000 | 1.000 | 0.907 | 0.977 | 0 |
| `hybrid+none` | 0.954 | 1.000 | 0.977 | 0.884 | 0.977 | 0 |
| `hybrid+lexical` | 1.000 | 1.000 | 1.000 | 0.907 | 0.954 | 0 |

Read with care: the questions and documents were written by the same author and share vocabulary,
which favors BM25; the embedder is a hashing stand-in, which handicaps the vector ranking; citation
accuracy is limited by the extractive answerer citing extra sources. These numbers are a regression
baseline for the pipeline, not a claim about answer quality with a real embedding model and LLM.

## Consequences

- Good: exact identifiers and rare terms are found even when the embedding model misses them; the
  eval makes retrieval changes measurable and blocks regressions and ACL leaks in CI.
- Cost: BM25 tokenizes every candidate passage on each question (no inverted index). Fine for thousands
  of passages; an index (SQLite FTS5 or Postgres full-text) is the next step at scale.
- `score` in `/ask` responses is now the final ranking score (an RRF value in hybrid mode, the rerank
  score with a reranker), with components in `scores`. Clients that read `score` as cosine similarity
  should read `scores.vector` or set `GATEWAY_RETRIEVAL_MODE=vector`.

## Alternatives considered

- **Weighted score sum** (alpha * cosine + (1 - alpha) * normalized BM25): needs per-corpus tuning of
  alpha and normalization; RRF is parameter-light.
- **LLM-as-judge evals**: measure answer faithfulness better, but need a model in CI. Roadmap, for
  deployments that run the eval against their own model.

## Where it lives

`gateway/retrieval.py`, `gateway/rag.py::retrieve`, `scripts/rag_eval.py`, `evals/`. Tests:
`tests/test_retrieval.py`, `tests/test_demo_engine.py::test_demo_retrieval_switches_use_the_gateway_settings`.
