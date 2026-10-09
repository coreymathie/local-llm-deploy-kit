# Configuration

The gateway is configured entirely through environment variables (or a `.env` file), validated at startup by
`gateway/config.py`. This document lists every setting with its default, and the sector profiles that set
stricter defaults for regulated deployments. Identity settings are explained in [identity.md](identity.md);
encryption and key handling in [operations.md](operations.md).

## Profiles

```bash
cp .env.example .env                 # defaults
cp profiles/healthcare.env .env      # or profiles/finance.env
```

The healthcare and finance profiles set `GATEWAY_COLLECTION_DEFAULT_ACCESS=restricted` and
`GATEWAY_MODEL_POLICY=enforce`. `profiles/compose-ollama.env` switches the Docker Compose stack from the
simulated backend to Ollama. Profiles are opinionated defaults, not legal advice; see
[compliance.md](compliance.md).

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `BACKEND` | `ollama` | `ollama` or `openai_compatible` (vLLM, SGLang, TGI, NIM, ...) |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Where Ollama listens |
| `OPENAI_COMPAT_BASE_URL` | `http://127.0.0.1:8000/v1` | OpenAI-compatible server, including `/v1` |
| `OPENAI_COMPAT_API_KEY` | empty | Sent upstream as a bearer token if set |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `8080` | Gateway bind address |
| `GATEWAY_DEFAULT_MODEL` | `llama3.1:8b` | Used when a request omits `model` |
| `GATEWAY_RATE_LIMIT_PER_MIN` | `60` | Requests per minute, applied to each key (same limit for every key) |
| `GATEWAY_LOG_PROMPTS` | `false` | Record prompts/completions in the audit log |
| `GATEWAY_REDACT_PROMPTS` | `false` | Redact SSNs, card numbers, DOBs, emails, phones in those entries |
| `GATEWAY_LOG_DIR` | `./logs` | Audit log location |
| `GATEWAY_DB_PATH` | `./gateway.db` | SQLite for keys, usage, documents and vectors |
| `GATEWAY_ADMIN_BOOTSTRAP_KEY` | auto | Leave blank to generate one on first run |
| `GATEWAY_EMBED_MODEL` | `nomic-embed-text` | Embedding model for documents and `/v1/embeddings` |
| `GATEWAY_MAX_UPLOAD_MB` | `20` | Largest document upload |
| `GATEWAY_METRICS_ENABLED` | `true` | Serve `/metrics` |
| `GATEWAY_METRICS_TOKEN` | empty | If set, `/metrics` requires `Authorization: Bearer <token>` |
| `GATEWAY_OIDC_ENABLED` | `false` | Accept OIDC access tokens (JWTs) alongside API keys |
| `GATEWAY_OIDC_ISSUER` / `GATEWAY_OIDC_AUDIENCE` | empty | Required when OIDC is on; exact `iss`, required `aud` |
| `GATEWAY_OIDC_JWKS_URL` | empty | Signing keys URL; blank means discovery from the issuer |
| `GATEWAY_OIDC_ALGORITHMS` | `RS256,ES256` | Allow-list; only RS256 and ES256 are accepted |
| `GATEWAY_OIDC_CLOCK_SKEW_SECONDS` | `60` | Leeway for `exp`/`nbf` (0–600) |
| `GATEWAY_OIDC_JWKS_CACHE_SECONDS` | `3600` | JWKS cache lifetime |
| `GATEWAY_OIDC_GROUPS_CLAIM` / `GATEWAY_OIDC_USERNAME_CLAIM` | `groups` / `sub` | Claims to read (dotted paths allowed) |
| `GATEWAY_OIDC_GROUP_ROLES` | `{}` | JSON: IdP group → roles |
| `GATEWAY_OIDC_ALLOW_HTTP` | `false` | Allow `http://` issuer/JWKS URLs |
| `GATEWAY_COLLECTION_DEFAULT_ACCESS` | `open` | Collections with no access list: `open` to user-role callers, or `restricted` |
| `GATEWAY_RETRIEVAL_MODE` | `hybrid` | `hybrid` (BM25 + vectors, RRF), `vector` (v0.5 behavior) or `bm25` |
| `GATEWAY_RRF_K` | `60` | Reciprocal Rank Fusion constant |
| `GATEWAY_RERANKER` | `none` | `none`, `lexical`, or `cross_encoder` (needs `sentence-transformers`; untested here) |
| `GATEWAY_RERANK_CANDIDATES` | `20` | Fused passages passed to the reranker |
| `GATEWAY_CROSS_ENCODER_MODEL` | empty | Local path of the cross-encoder model |
| `GATEWAY_ENCRYPT_AT_REST` | `false` | Seal passages and vectors with per-document keys |
| `GATEWAY_ENCRYPTION_KEY_FILE` / `GATEWAY_ENCRYPTION_KEY` | empty | Keyring file (`scripts/keys.py generate`) or one base64 32-byte key |
| `GATEWAY_AUDIT_ENCRYPT_TEXT` | `false` | Seal prompt/response/question/answer fields in audit entries |
| `GATEWAY_MODEL_LOCK_FILE` | empty | Pinned model digests (`models.lock.example.json`, `scripts/pin_models.py`) |
| `GATEWAY_MODEL_POLICY` | `warn` | `off`, `warn` (serve and flag) or `enforce` (403 for unpinned or unverified models); `enforce` in the healthcare and finance profiles |
| `GATEWAY_MODEL_VERIFY_INTERVAL_SECONDS` | `300` | Re-check Ollama digests when the last check is older than this |

Settings are validated at startup: an unknown backend is rejected
(`test_backends.py::test_unknown_backend_is_rejected_at_startup`), and `.env.example` and the profiles may use
only known settings (`test_deploy_config.py::test_env_example_and_profiles_only_use_known_settings`).
