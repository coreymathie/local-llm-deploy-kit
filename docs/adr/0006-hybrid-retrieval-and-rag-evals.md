# ADR 0006: Hybrid retrieval (BM25 + vectors, RRF), optional reranking, and a RAG eval gate in CI

## Status

Accepted, v0.6.0 (extends ADR 0002).

## Context

ADR 0002 chose exact cosine search over local embeddings and listed two costs: dense-only retrieval misses exact
identifiers that keyword search catches, and there was no retrieval evaluation, so any change to chunking,
embedding or ranking was unmeasured.

## Decision

1. **Hybrid by default.** `gateway/retrieval.py` computes BM25 (pure Python, over the passages the caller may
   read) and the cosine ranking, and fuses them with Reciprocal Rank Fusion (`1 / (k + rank)`, `k = 60`). RRF
   needs no score normalization between two incomparable scales. `GATEWAY_RETRIEVAL_MODE=vector` restores the
   v0.5 behavior; `bm25` skips the question embedding.
2. **Pluggable second stage.** A `Reranker` interface (`rerank(query, passages) -> scores`) over the top
   `GATEWAY_RERANK_CANDIDATES` fused passages. Shipped: `lexical` (IDF-weighted query-term coverage plus bigram
   matches; deterministic, no model). `cross_encoder` loads a local sentence-transformers CrossEncoder from a
   path; it is **interface only here**: the package and a model were not available offline in the build
   environment, so it is untested. Default: `none`.
3. **Access control first.** Ranking only ever sees passages that `rag.access_for` admitted (ADR 0005, OWASP
   LLM08); BM25 statistics are therefore computed per caller, so term frequencies of hidden documents cannot
   influence scores either.
4. **Evals gate CI.** `scripts/rag_eval.py --check` runs the real ingestion, access and retrieval code over the
   console's sample library (`demo/data/library.json`: 59 documents in 6 collections, with collection and
   document access lists) and a golden set (`evals/golden/questions.jsonl`: the persona who asks, the expected
   document and answer phrase, or a question to decline) with the demo's deterministic embedder and extractive
   answerer, so it runs offline in seconds. It fails if recall@1, recall@k, MRR, citation accuracy,
   answer-contains or decline accuracy drop below `evals/thresholds.json`, or if any persona retrieves a passage
   from a document the catalog's access lists say it may not read.

**Implementation and evidence.** `gateway/retrieval.py`, `gateway/rag.py::retrieve`, `scripts/rag_eval.py`,
`evals/`. Tests: `tests/test_retrieval.py`,
`tests/test_demo_engine.py::test_demo_retrieval_switches_use_the_gateway_settings`.

## Consequences

**Measured baseline** (k = 4; demo hashed bag-of-words embedder, not a neural model; 59 questions, 50 answerable
and 9 to decline, over the 59-document sample library, asked as 5 personas):

| Configuration | recall@1 | recall@4 | MRR | citation accuracy | answer contains | decline accuracy | ACL leaks |
|---|---:|---:|---:|---:|---:|---:|---:|
| `vector+none` | 0.800 | 0.960 | 0.872 | 0.860 | 0.840 | 0.222 | 0 |
| `bm25+none` | 1.000 | 1.000 | 1.000 | 0.900 | 0.860 | 0.111 | 0 |
| `hybrid+none` | 0.880 | 1.000 | 0.940 | 0.920 | 0.840 | 0.222 | 0 |
| `hybrid+lexical` | 0.960 | 1.000 | 0.975 | 0.880 | 0.860 | 0.222 | 0 |

These numbers need careful reading: the questions are paraphrased, but the library and the questions share one
author, which favors BM25; the embedder is a hashing stand-in, which handicaps the vector ranking; citation
accuracy and answer-contains are limited by the extractive answerer picking or adding the wrong sentence; decline
accuracy is limited by a fixed relevance floor that bm25-only retrieval cannot apply. They are a regression
baseline for the pipeline, not a claim about answer quality with a real embedding model and LLM. An earlier
generic golden set (12 documents, 43 questions for an unrelated fictional company) was replaced because it did
not exercise the demo's own library, collections or personas; its numbers are no longer published.

**Positive**

- Exact identifiers and rare terms are found even when the embedding model misses them; the eval makes
  retrieval changes measurable and blocks regressions and ACL leaks in CI.

**Negative**

- BM25 tokenizes every candidate passage on each question (no inverted index). Acceptable for thousands of
  passages; an index (SQLite FTS5 or Postgres full-text) is the next step at scale.
- `score` in `/ask` responses is now the final ranking score (an RRF value in hybrid mode, the rerank score
  with a reranker), with components in `scores`. Clients that read `score` as cosine similarity should read
  `scores.vector` or set `GATEWAY_RETRIEVAL_MODE=vector`.

## Alternatives considered

- **Weighted score sum** (alpha * cosine + (1 - alpha) * normalized BM25): needs per-corpus tuning of alpha and
  normalization; RRF is parameter-light.
- **LLM-as-judge evals**: measure answer faithfulness better, but need a model in CI. Roadmap, for deployments
  that run the eval against their own model.
