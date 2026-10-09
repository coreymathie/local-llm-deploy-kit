# Operations

This document is the operating model for the gateway: what state it holds and how that state is encrypted,
how keys rotate, how backups and restores are verified, how recovery objectives are set, how the platform is
observed, how models are pinned, and how each component fails. Every procedure maps to a script and a test;
gaps are listed at the end. Related: [threat model](threat-model.md), [controls](controls.md),
[compliance notes](compliance.md), [configuration](configuration.md).

## State

| What | Where | Holds | Encrypted by the gateway |
|---|---|---|---|
| Database | `GATEWAY_DB_PATH` (SQLite) | API keys and usage, token-user usage, documents, passages, vectors, access lists | Passages and vectors when `GATEWAY_ENCRYPT_AT_REST=true`. Not: titles, sizes, access lists, uploader labels, API keys (plaintext) |
| Audit log | `$GATEWAY_LOG_DIR/audit.jsonl` | Hash-chained events | Free-text fields (prompt, response, question, answer) when `GATEWAY_AUDIT_ENCRYPT_TEXT=true`; metadata stays readable |
| Keyring | `GATEWAY_ENCRYPTION_KEY_FILE` (or `GATEWAY_ENCRYPTION_KEY`) | Key-encryption keys (KEKs) | It *is* the key material: protect it with OS permissions (written as mode 0600), a secrets manager, or a KMS |
| Configuration | `.env` / environment | Settings, possibly the metrics token and upstream API key | No |

Full-disk or volume encryption is required as well: it covers what the gateway does not (titles, API keys,
temporary files, the OS).

## Encryption at rest

Envelope encryption with AES-256-GCM (`gateway/crypto.py`, using `cryptography`); rationale in
[ADR 0007](adr/0007-envelope-encryption-at-rest.md).

- Each document receives a random 256-bit data key (DEK). Its passages' text and vectors are sealed with the
  DEK; the associated data names the document, passage number and field, so swapping ciphertexts between rows
  fails authentication (`test_encryption.py::test_ciphertexts_are_bound_to_their_row_and_key`).
- The DEK is stored wrapped by the active KEK, with the KEK's id. Decryption happens after the access filter,
  so a hidden document's DEK is never unwrapped for a caller who cannot read it
  (`test_encryption.py::test_hidden_documents_keys_are_never_unwrapped`).
- A wrong or missing key, or altered ciphertext, makes `/ask` fail closed with a 500 that names the keyring as
  the problem; nothing is returned in plaintext by fallback.
- Audit free-text fields are sealed per entry before hashing, so `GET /admin/audit/verify` works without keys;
  admins see opened fields in `/admin/audit/recent` while the key is available
  (`test_encryption.py::test_audit_text_fields_are_sealed_and_the_chain_verifies_without_keys`).

**Measured cost** on the build host with `python scripts/bench_storage.py --docs 500` (2 vCPUs, shared, Intel
Xeon 2.80 GHz; synthetic collection of 500 documents, 3,883 passages, 16.1 MB database; demo embedder; medians;
two runs, which varied by up to about 30%): loading and decrypting every passage for one question took
61.5–83.5 ms versus 21.8–33.5 ms in plaintext, so decryption added about 40–50 ms; a full hybrid retrieval took
199–248 ms encrypted versus 182–240 ms plaintext. At that size most of the retrieval time is BM25 tokenization
in Python, not decryption. Results vary with hardware and data and should be re-measured per deployment.

### Enabling encryption

```bash
python scripts/keys.py generate --keyring /etc/private-llm-platform/keyring.json --activate
# .env: GATEWAY_ENCRYPT_AT_REST=true, GATEWAY_ENCRYPTION_KEY_FILE=/etc/private-llm-platform/keyring.json
python scripts/keys.py encrypt-existing   # documents stored before encryption was enabled; then VACUUM runs
python scripts/keys.py status
```

`encrypt-existing` rewrites the database file (`VACUUM`) so freed pages do not keep the old plaintext, but
**backups, snapshots and replicas taken before encryption still contain plaintext**: replace them on the
retention schedule. The gateway refuses to start if encryption is on and the key cannot be loaded
(`test_encryption.py::test_misconfigured_encryption_fails_at_startup`).

### Key rotation

1. `python scripts/keys.py generate --keyring PATH --activate` (new documents use the new KEK after the
   gateway restarts).
2. `python scripts/keys.py rewrap`: every DEK is unwrapped with its old KEK and wrapped with the active one.
   Passages are not re-encrypted, so rotation is quick and does not touch the bulk of the data. Audited as
   `encryption_keys_rewrapped`.
3. `python scripts/keys.py status`: confirm no document uses the old key.
4. `python scripts/keys.py retire --keyring PATH --id OLD`: refuses while the key is active, still wraps a
   document, or sealed audit fields reference it (those cannot be re-sealed without breaking the hash chain;
   archive the old key with the audit archive, or pass `--force`).

Retired keys are kept wherever old backups are kept: a backup is readable only with the KEKs listed in its
manifest. Tested end to end in `test_encryption.py::test_encrypting_existing_documents_and_rotating_the_key`.

Rotating the DEKs themselves (re-encrypting passages) is not implemented; re-ingesting a document gives it a
new DEK.

### KMS integration

Key providers implement two calls, `wrap(dek) -> (kek_id, wrapped)` and `unwrap(kek_id, wrapped) -> dek`
(`gateway/crypto.py::KeyProvider`), and are installed with `gateway/crypto.py::set_provider`. A KMS provider
maps them to AWS KMS `Encrypt`/`Decrypt`, Azure Key Vault `wrapKey`/`unwrapKey`, Google Cloud KMS
`encrypt`/`decrypt`, or HashiCorp Vault Transit `encrypt`/`decrypt`, so the KEK never leaves the KMS and every
unwrap is logged there. **No KMS provider ships in this repository**, and none has been tested; the local
keyring is the only implemented provider. With a KMS, one unwrap per document per question becomes one KMS
call: DEKs need a short-lived in-memory cache or the KMS becomes the bottleneck.

### Deleting data

Deleting a document deletes its passages, vectors and wrapped DEK. Older backups still contain them and remain
readable while their KEK exists; destroying a KEK makes every backup that needs it unreadable (crypto-shredding
at backup granularity, not per document).

## Backup

```bash
python scripts/backup.py --out /backups        # -> /backups/lldk-backup-<UTC timestamp>/
```

- The database is copied with SQLite's online backup API: consistent while the gateway runs.
- The audit log is copied and the copy's chain verified; the manifest records entries and last hash.
- `manifest.json` lists SHA-256 and size per file, the SQLite integrity check, and the KEK ids the backup
  needs. **Keys are never included**; the keyring belongs on a different system with different access.
- API keys are stored in plaintext in the database; backups need the same protection as the live database.

Backups are shipped off the host (object storage with versioning or object lock). The audit log's WORM archive
(see [compliance.md](compliance.md)) is a separate, append-only copy; both are required.

## Restore

Stop the gateway, then:

```bash
python scripts/restore.py /backups/lldk-backup-... --check-only   # verify only
python scripts/restore.py /backups/lldk-backup-...                # refuses to overwrite live files
python scripts/restore.py /backups/lldk-backup-... --force        # live files are moved aside, not deleted
```

Before touching anything, restore checks that every file matches its checksum, the SQLite integrity check
passes, the audit chain verifies (an attacker who edits the log *and* the manifest is still caught), and the
configured keyring holds every KEK the backup needs. After copying, it checks integrity and the chain again.
Tested as a full drill (backup while running, delete everything, restore, start a new gateway, ask a question
through an ACL, keep appending to the audit chain) in
`test_backup_restore.py::test_backup_restore_drill_brings_back_documents_keys_acls_and_the_audit_chain`,
plus refusal cases in `test_backup_restore.py::test_restore_refuses_altered_or_incomplete_backups_and_leaves_live_files_alone`
and `test_backup_restore.py::test_restore_needs_the_keys_and_does_not_overwrite_without_force`.

**Measured** on the build host for the 16.1 MB synthetic database above (`scripts/bench_storage.py`, two runs):
`backup` took 111–117 ms and `restore` 114–119 ms. That is only the copy-and-verify step, not a recovery time.

## Recovery objectives (RTO / RPO)

This repository does not promise recovery times; they depend on hosts, storage and people. How to set and
measure them:

- **RPO (data that can be lost)** is at most the interval between successful, off-host backups, plus the delay
  before audit archives reach WORM storage. Hourly backups mean up to an hour of new documents, keys and audit
  entries. For less, use storage-level replication or snapshots (deployer choice; not provided or tested here).
- **RTO (time to serve again)** = provision or repair the host + install the platform (or start the container)
  + fetch the backup and the keyring + `restore.py` + start the gateway + smoke test. Measure it with a restore
  drill on the schedule the compliance program requires (quarterly is common) and record the result as audit
  evidence. The restore step itself is small next to provisioning and model downloads: pre-stage model files
  (see the air-gap bundle in [kubernetes.md](kubernetes.md)) if the model server must be rebuilt too.
- The model server is stateless apart from model files; its recovery is reinstalling it and the pinned models.

## Observability

- **`GET /metrics`** (Prometheus): `gateway_http_requests_total{method,route,status,key}`,
  `gateway_http_request_duration_seconds`, `gateway_llm_tokens_total{key,model,type}`,
  `gateway_llm_request_duration_seconds{operation,backend,model}`,
  `gateway_llm_time_to_first_token_seconds`, `gateway_backend_errors_total{operation,backend,reason}`,
  `gateway_info`. The `key` label is the key's **label** (e.g. `hr-bot`), never the secret; unknown paths
  collapse to `route="unmatched"`. `GATEWAY_METRICS_TOKEN` requires a bearer token for scraping.
- **Grafana**: [`deploy/grafana-dashboard.json`](../deploy/grafana-dashboard.json) (requests by route, 5xx
  ratio, latency and TTFT percentiles, tokens/s per key, backend latency and errors, rejected requests by
  status). Scrape config: [`deploy/prometheus.yml`](../deploy/prometheus.yml).
- **OpenTelemetry**: each backend call opens a client span named `chat <model>` / `embeddings <model>` with
  `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.request.temperature`,
  `gen_ai.request.max_tokens`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and `error.type`.
  No-op unless `opentelemetry` is installed; export uses the standard SDK setup
  (`pip install opentelemetry-distro opentelemetry-exporter-otlp`, then
  `opentelemetry-instrument uvicorn gateway.main:app`). Span content is tested with the SDK's in-memory
  exporter; export to a live collector has not been tested here.
- No prompt or completion text appears in metrics or spans; token callers are labelled `oidc`, never by user
  name.

## Model supply chain

```bash
python scripts/pin_models.py --out models.lock.json        # pins what is served now (trust on first use)
GATEWAY_MODEL_LOCK_FILE=./models.lock.json GATEWAY_MODEL_POLICY=enforce uvicorn gateway.main:app
python scripts/mlbom.py --lock models.lock.json --verify --out mlbom.cdx.json   # CycloneDX 1.6 ML-BOM
```

`GET /admin/models/verification` shows each model's status (`verified`, `mismatch`, `missing`, `unpinned`,
`unverifiable`, `error`); `POST /admin/models/verify` re-checks immediately. Pinning records what is served at
that moment: digests must be compared with the publisher's before they are relied on
([ADR 0008](adr/0008-model-pinning-and-ml-bom.md)).

## Failure modes

### Gateway, identity and inference

| Situation | Behavior | Notes |
|---|---|---|
| Inference server down or unreachable | `502 model runtime unavailable: <error>`; `/health` reports `backend_ok: false`; `gateway_backend_errors_total` increments | For streaming too: the first event is read before the response starts, so clients receive a 502, not a broken 200 stream |
| Inference server returns an error | Status and body passed through (e.g. 404 unknown model) | Same contract as v0.4 |
| Stream fails after the first token | The stream ends early; usage, token metrics and the `completion` audit entry for that request are not recorded | Not retried; the backend error is counted in `gateway_backend_errors_total` |
| Key over its limit | `429 rate limit exceeded (N/min)`, counted per key label in metrics | Limiter is in memory, per process |
| More than one gateway process | Each process enforces its own limit (effective limit × N); audit appends are serialized per process only | Run one process per host, or add a shared store (roadmap). The Helm schema caps `replicaCount` at 1 |
| Revoked or unknown key | `401` | |
| Expired, forged or wrong-audience token | `401` with `WWW-Authenticate: Bearer error="invalid_token"` | Detail says which check failed |
| Identity provider unreachable | Cached signing keys keep working for one more cache period (`GATEWAY_OIDC_JWKS_CACHE_SECONDS`), then token requests receive `503`; API keys are unaffected | No keys fetched yet: `503` immediately |
| IdP rotates its signing key | Unknown `kid` triggers one JWKS refetch (at most every 30 s); retired keys stop working after the next refetch | |
| `/v1/pull` with a non-Ollama backend | `501` | The inference server owns its models |
| Model not pinned, or its digest changed (`GATEWAY_MODEL_POLICY=enforce`) | `403 model 'x' is not allowed by the model policy (unpinned \| mismatch \| missing \| error)`; audited `model_verification` | `warn` serves it, logs once per verification and counts `gateway_model_policy_decisions_total` |
| Backend unreachable during model verification | Startup continues after at most 10 s; under `enforce` requests receive `403 (error)` until verification succeeds | Fails closed |
| Audit file edited | `GET /admin/audit/verify` returns the first bad line | Rewriting the newest entries is not detectable without the WORM copy (ADR 0003) |
| Caller lacks access to a collection or document | Same `404`/empty list as a missing collection; denial audited | No existence oracle through the API |
| Poisoned document | Passage flagged (`injection_flags`); its instructions are not supposed to be followed, but its claims can be cited | ADR 0004; only admins can add documents |
| Large collections | Exact search scores every readable passage on each question, and BM25 tokenizes them per question | Measured 182–248 ms per hybrid retrieval over 3,883 passages on a 2-vCPU host (`scripts/bench_storage.py`); ANN and full-text indexes are roadmap |
| Cross-encoder reranker without `sentence-transformers` or a local model | Fails on the first question with a clear error | Interface only in this repo; not tested here |
| Concurrent writes | SQLite serializes writers | Postgres for heavy multi-writer loads (roadmap) |

### Data, keys and recovery

| Situation | Behavior |
|---|---|
| Encryption on, key missing or invalid at startup | Gateway refuses to start with a message naming the setting |
| Key removed while running, wrong key, or ciphertext altered | `/ask` on that collection returns `500 stored documents could not be decrypted`; logged; nothing returned in plaintext |
| Keyring lost | Encrypted passages and sealed audit fields are unrecoverable. The keyring is backed up separately |
| Backup altered or incomplete | `restore.py` refuses before changing anything |
| Restore target exists | Refused without `--force`; with it, existing files are renamed `*.pre-restore-<timestamp>` |

## Not implemented

KMS providers; per-document DEK rotation without re-ingest; encryption of titles, access lists and API keys
(API keys are not hashed at rest); automatic scheduled backups (use cron or an existing scheduler);
point-in-time recovery.
