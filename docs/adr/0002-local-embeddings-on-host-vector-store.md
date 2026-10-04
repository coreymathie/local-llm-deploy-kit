# ADR 0002: Local embeddings and an on-host vector store (SQLite, exact search)

Status: Accepted, v0.4.0 (embedding calls moved behind the backend interface in v0.5.0; ranking extended by ADR 0006 and access control by ADR 0005 in v0.6.0)

## Context

Document Q&A must not send documents or questions off the machine. It also has to install with the
rest of the kit: one Python process, no extra services for a small team.

## Decision

- Embed passages and questions with a local model through the configured backend
  (`nomic-embed-text` on Ollama by default; any `/v1/embeddings` model with `openai_compatible`).
- Store passages and their float32 vectors as BLOBs in the gateway's existing SQLite file
  (`documents`, `chunks` tables).
- Retrieve with exact cosine similarity: load the collection's vectors, one matrix-vector product with
  numpy, take the top k (1–12).
- Chunk paragraph-aware at about 900 characters with 150 characters of overlap.

## Consequences

- Good: nothing leaves the host; one file to back up; no index to rebuild; deterministic results.
- Good: exact search has perfect recall for its embedding model, so it's a clean baseline for later
  retrieval evaluation.
- Cost: every question scores every passage in the collection. Fine for thousands of passages; a large
  library should move vectors to an ANN index (sqlite-vec, pgvector). **Not implemented** (roadmap).
- Cost: dense-only retrieval misses exact identifiers (policy numbers, error codes) that keyword search
  catches. Addressed in v0.6.0: hybrid BM25 + vector retrieval with an optional reranker and a RAG eval
  gate in CI ([ADR 0006](0006-hybrid-retrieval-and-rag-evals.md)).
- Cost: collection access was all-or-nothing in v0.4–v0.5. Addressed in v0.6.0: collection and document
  access lists applied before retrieval ([ADR 0005](0005-oidc-identity-and-roles.md), README "Who can read what").
- Cost: vectors and passages were stored unencrypted in SQLite through v0.5. Addressed in v0.6.0 as an
  opt-in: envelope encryption of passages and vectors ([ADR 0007](0007-envelope-encryption-at-rest.md)).

## Alternatives considered

- **A vector database service (Qdrant, Weaviate, Milvus).** Better at scale, but another service to run
  and secure for deployments that are often a single box.
- **pgvector.** Natural once the gateway moves from SQLite to Postgres; that move is the trigger.
- **FAISS in memory.** Fast, but the index would need persisting and rebuilding alongside SQLite.

## Where it lives

- Code: `gateway/rag.py` (`chunk_text`, `embed`, `add_document`, `retrieve`, `build_messages`, `cited_numbers`)
- Tests: `tests/test_documents.py` (retrieval picks the right document, citations, chunk overlap,
  unit-length embeddings, PDF parsing), `tests/test_backends.py::test_openai_compatible_document_qa_end_to_end`
