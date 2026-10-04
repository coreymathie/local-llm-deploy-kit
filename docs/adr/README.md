# Architecture decision records

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-inference-backend.md) | Ollama is the default backend; any OpenAI-compatible server (vLLM, SGLang, TGI, NIM) is pluggable | Accepted (0.5.0) |
| [0002](0002-local-embeddings-on-host-vector-store.md) | Local embeddings, vectors in the gateway's SQLite file, exact cosine search | Accepted (0.4.0) |
| [0003](0003-audit-log-hash-chain-vs-worm.md) | Hash-chained JSONL audit log on the host; WORM storage for retention | Accepted (0.3.0) |
| [0004](0004-prompt-injection-defense-in-depth.md) | Prompt-injection guard is defense in depth, not a fix | Accepted (0.4.0, flags added 0.5.0) |
| [0005](0005-oidc-identity-and-roles.md) | OIDC access tokens alongside API keys; roles (admin, user, reader:&lt;collection&gt;) from IdP groups | Accepted (0.6.0) |
| [0006](0006-hybrid-retrieval-and-rag-evals.md) | Hybrid BM25 + vector retrieval fused with RRF, pluggable reranker, RAG eval gate in CI | Accepted (0.6.0) |
| [0007](0007-envelope-encryption-at-rest.md) | Envelope encryption (AES-256-GCM, per-document data keys, pluggable KEK provider) for passages, vectors and audit text; keys never in backups | Accepted (0.6.0) |
| [0008](0008-model-pinning-and-ml-bom.md) | Pinned model digests verified against what the backend serves, an off/warn/enforce policy, and a CycloneDX 1.6 ML-BOM | Accepted (0.6.0) |

Format: context, decision, consequences, alternatives, and where the decision lives in code and tests.
