# Document Q&A

Document Q&A answers staff questions from an organization's own policies and procedures, with numbered
citations to the passages used and only from documents the caller may read. This document covers ingestion,
the request and response contract, the retrieval pipeline, and the endpoints. Access rules are in
[identity.md](identity.md); design rationale in [ADR 0002](adr/0002-local-embeddings-on-host-vector-store.md),
[ADR 0004](adr/0004-prompt-injection-defense-in-depth.md) and
[ADR 0006](adr/0006-hybrid-retrieval-and-rag-evals.md).

## Ingestion

A folder of policies, contracts or procedures loads into a collection:

```bash
export GATEWAY_ADMIN_KEY=sk-local-...
python scripts/ingest_folder.py ./employee-handbook --collection handbook   # re-run any time; only new files are added
```

Documents can also be uploaded from the admin page's **Documents** card:

![Documents card: the sample policies collection with per-document access lists, and a cited answer to a hotel-cap question](admin-documents.png)

Only admins add or remove documents and change access lists; every change is audited.

## Asking

Any key with read access can ask:

```bash
curl -s http://localhost:8080/v1/collections/handbook/ask \
  -H "Authorization: Bearer sk-local-..." -H "Content-Type: application/json" \
  -d '{"question": "How much notice do vacation requests need?"}'
```

```json
{
  "answer": "Vacation requests need manager approval two weeks in advance [1].",
  "sources": [
    {"n": 1, "title": "pto-policy.md", "chunk": 0, "score": 0.71, "cited": true, "injection_flags": [], "excerpt": "Paid time off. Full-time..."},
    {"n": 2, "title": "remote-work.md", "chunk": 0, "score": 0.38, "cited": false, "injection_flags": [], "excerpt": "Remote work. Employees..."}
  ],
  "model": "llama3.1:8b",
  "usage": {"total_tokens": 312}
}
```

(Illustrative response shape; scores depend on the embedding model.)

## Retrieval pipeline

1. **Chunking and embedding.** Documents are split into overlapping, paragraph-aware passages and embedded
   with a local model (`nomic-embed-text` by default).
2. **Access filter.** Only passages the caller may read are candidates ([identity.md](identity.md)).
3. **Ranking.** Retrieval ranks candidates by BM25 and by cosine similarity of embeddings and fuses the two
   rankings with Reciprocal Rank Fusion (`GATEWAY_RETRIEVAL_MODE=hybrid`, the default; `vector` is the v0.5
   behavior; `bm25` skips embedding the question).
4. **Reranking (optional).** `GATEWAY_RERANKER=lexical`, or `cross_encoder` with a local sentence-transformers
   model (untested here), reorders the top candidates.
5. **Answering.** The chat model answers from the top passages only, which are passed as numbered data, not
   instructions.

Each source's `score` is the final ranking score, with `scores` holding the components (`vector`, `bm25`,
`rrf`, `rerank`). Each source is marked `cited` if the answer references it, and `injection_flags` lists any
injection heuristics the passage matched.

## Endpoints

| Endpoint | Who | Purpose |
|---|---|---|
| `GET /v1/collections` | any caller | Collections the caller may read, with counts of only the documents it may read |
| `GET /v1/collections/{c}/documents` | any caller | Documents in a collection the caller may read (access lists shown to admins only) |
| `POST /v1/collections/{c}/documents` | admin | Upload a file (PDF, TXT, MD, CSV, JSON, HTML); optional form field `acl` |
| `POST /v1/collections/{c}/documents/text` | admin | Add text directly (`{"title", "text", "acl"?}`) |
| `DELETE /v1/collections/{c}/documents/{id}` | admin | Remove a document and its passages |
| `PUT /v1/collections/{c}/documents/{id}/acl` | admin | Replace a document's access list (`{"principals": [...]}`; `[]` inherits the collection's) |
| `GET`/`PUT /admin/collections/{c}/acl` | admin | Read or replace a collection's access list |
| `POST /v1/collections/{c}/ask` | any caller with read access | `{"question", "top_k"?, "model"?, "retrieval_mode"?, "reranker"?}` → cited answer from the documents the caller may read, with `timings_ms` |
| `POST /v1/embeddings` | any key | OpenAI-shaped embeddings |
