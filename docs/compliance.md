# Compliance notes

Regulated institutions (credit unions, banks, healthcare and legal teams) often cannot send prompts or
documents to a public API, and a self-hosted model is only usable once access, evidence and retention meet the
same bar as other systems of record. This document covers the operating side of that posture: sector profiles,
what the platform provides and what it deliberately leaves to other controls, the audit log and its retention,
air-gapped installation, and a monthly review procedure. The framework mappings are in
[controls.md](controls.md); threats and residual risks in [threat-model.md](threat-model.md).

Nothing here is a certification or a compliance claim. The platform provides technical safeguards and evidence
that an institution's compliance program can use.

## Sector profiles

```bash
cp profiles/healthcare.env .env    # or profiles/finance.env
# then edit for local paths and model choice
```

The profiles are opinionated defaults, not legal advice; each setting needs review by the institution's
compliance function. [controls.md](controls.md) maps these controls to HIPAA 164.312, SOC 2 CC6/CC7, NIST AI RMF
/ AI 600-1, ISO/IEC 42001 and the OWASP LLM Top 10, with the code and test behind each row and the known gaps.

## What the platform provides

- **No outbound calls.** The gateway talks only to the configured inference server: Ollama on `127.0.0.1` by
  default, or the `OPENAI_COMPAT_BASE_URL` set for vLLM or another OpenAI-compatible server (on the same host
  or private network). No analytics, no update ping. Prometheus metrics are pulled from `/metrics` by the
  institution's own scraper (protected with `GATEWAY_METRICS_TOKEN`), and OpenTelemetry spans are exported only
  if an exporter is configured; neither carries prompt or completion text.
- **Localhost-only bind.** The default `GATEWAY_HOST=127.0.0.1` keeps the API off the network. Remote access
  goes through a reverse proxy on the VPN interface.
- **Per-application keys with revocation, and per-person SSO.** No key is shared: each application has its own,
  with its own rate limit and audit trail. With OIDC enabled, people use their identity provider's access tokens
  and receive roles from their groups ([ADR 0005](adr/0005-oidc-identity-and-roles.md)).
- **Need-to-know retrieval.** Collections and documents can carry access lists; a caller's question searches
  only documents it may read, and every answer's audit entry records the access decision.
- **Tamper-evident audit log.** Admin actions are always recorded; prompts and responses are recorded with
  `GATEWAY_LOG_PROMPTS=true`, PII-redacted with `GATEWAY_REDACT_PROMPTS=true`. Entries are hash-chained, and
  `/admin/audit/verify` reports the first tampered line.
- **Local usage metrics.** The admin UI reads directly from the local SQLite file; `/metrics` exposes counters
  labelled by key *label*, never the key itself.
- **Documents stay on the host.** Uploaded documents, their passages and their embeddings are stored in the
  gateway's SQLite file (encrypted at rest when enabled); embedding and answering both run through the local
  inference server. Only admins can add or remove documents or change their access lists, and every change is
  audited.
- **Every document question is traceable.** A `document_question` entry records the key, the collection, the
  model and the exact document passages the answer drew on, whether or not prompt logging is on. That answers
  the auditor's question "what did the system rely on when it said this?"

## What the platform does not provide, by design

- **Formal DLP.** A commercial DLP at egress belongs in deployments sensitive enough to need one.
- **Zero-trust networking.** Pair with Tailscale, Twingate or the enterprise VPN. The gateway is not the only
  perimeter.
- **Model safety / content moderation.** Llama Guard, Lakera or a custom classifier can be placed in front of
  `/v1/chat/completions` where the use case needs it. The built-in prompt-injection guard flags suspicious
  passages but is defense in depth, not a filter ([ADR 0004](adr/0004-prompt-injection-defense-in-depth.md)).
- **Full encryption at rest.** With `GATEWAY_ENCRYPT_AT_REST=true` the gateway seals document passages and
  vectors (and, with `GATEWAY_AUDIT_ENCRYPT_TEXT=true`, prompt and answer text in the audit log) under keys the
  operator controls ([operations.md](operations.md)). API keys (stored unhashed), document titles, access lists
  and audit metadata stay readable in the files; full-disk or volume encryption is required as well. A KMS key
  provider and key hashing are on the roadmap.
- **Clinical validation / medical device certification.** Healthcare deployments that make clinical decisions
  need a formal validation program that this document does not substitute for.

## Audit log events

`$GATEWAY_LOG_DIR/audit.jsonl` holds one JSON entry per event:

| Event | When |
|---|---|
| `admin_bootstrap` | First admin key created at startup |
| `key_created`, `key_revoked` | Admin key management (keys are stored masked) |
| `model_pull_started`, `model_pull_finished` | Model downloads, with who requested them |
| `model_verification` | Served models checked against the lock file (startup, after pulls, admin), with counts and problems |
| `completion` | Each prompt and response, if `GATEWAY_LOG_PROMPTS=true` (redacted if `GATEWAY_REDACT_PROMPTS=true`) |
| `document_added`, `document_removed` | Changes to a collection, with who made them |
| `document_question` | Always: which caller asked which collection, the access decision, and which passages were retrieved, cited and flagged (with the rule that admitted each). Question and answer text follow the prompt-logging and redaction settings. |
| `document_question_denied` | A caller asked a collection whose documents it may not read |
| `collection_acl_changed`, `document_acl_changed` | Access-list changes, before and after, with who made them |
| `policy_changed` | Runtime policy applied from the console (`PUT /admin/policy`), before and after, with who applied it |

Each entry includes the SHA-256 of the previous entry. Editing or deleting a line breaks the chain from that
point. `GET /admin/audit/verify` (and the admin page) reports `chain intact` or the first tampered line number;
the console's Audit screen shows each entry as a decision timeline. Design and limits:
[ADR 0003](adr/0003-audit-log-hash-chain-vs-worm.md).

## WORM / immutable log retention

The hash chain detects edits to any entry that has a later entry after it. The newest entries can be rewritten
(with recomputed hashes) by someone with write access, so the WORM copy is what protects the tail
([ADR 0003](adr/0003-audit-log-hash-chain-vs-worm.md)). Archives are taken frequently for that reason.

`audit.jsonl` is the operational copy. Rotation: verify the chain (`GET /admin/audit/verify`), copy the file to
WORM storage, then move it aside. The next entry starts a new chain; the archived file's last `entry_hash` is
recorded in the retention log so the two can be checked together. Archive targets:

- **AWS**: S3 Object Lock in Compliance mode
- **Azure**: immutable Blob storage
- **GCS**: Bucket Lock with retention policy
- **On-prem**: a WORM NAS or an immutable ZFS dataset with retention set at the OS level

Retention periods common in each sector:

- Healthcare (US, HIPAA): 6 years from creation
- Finance (SOX / SOC2): 7 years
- Legal: matter-dependent; consult the firm's records policy
- Fintech (various): usually 5-7 years, often extended by consent decrees

## Air-gapped deployment

1. On a connected host, pull Ollama, the target model and the embedding model
   (`ollama pull llama3.1:8b && ollama pull nomic-embed-text`). Model artifacts live under `~/.ollama/models`.
2. On a connected host, run `pip download -r requirements.txt -d wheels/`.
3. Transfer `~/.ollama/models/` and `wheels/` to the air-gapped host.
4. Install Ollama from an offline package (`ollama/ollama` releases page) and copy the models directory to
   `~/.ollama` there.
5. Install the gateway: `pip install --no-index --find-links=wheels/ -r requirements.txt`.
6. Start the gateway. No outbound connection is needed at runtime.

`scripts/airgap_bundle.sh` automates steps 1–3 into one folder with a `SHA256SUMS` file (wheels, container
images, the Ollama manifests and blobs of the named models, Hugging Face snapshots for vLLM, source, Helm chart,
model lock file and ML-BOM); `--dry-run` shows the plan and `--verify` checks the folder after transfer. See
[kubernetes.md](kubernetes.md) for the cluster variant.

## Scoped network listening

`GATEWAY_HOST=127.0.0.1` is replaced with the specific VPN or private subnet interface IP, never `0.0.0.0`. On
Linux, confirm with `ss -tlnp`.

## Where the audit evidence lives

| Evidence | Location |
|---|---|
| Which keys exist and how much each has used | `GATEWAY_DB_PATH` (SQLite `api_keys`: request and token counters) |
| Prompt + response text (optional, redactable) | `GATEWAY_LOG_DIR/audit.jsonl`, `completion` events |
| Key creation / revocation, model downloads | `audit.jsonl` (`key_created`, `key_revoked`, `model_pull_*`) and SQLite `api_keys` |
| Which documents were in a collection, and who added or removed them | SQLite `documents` table; `audit.jsonl` (`document_added`, `document_removed`) |
| Which documents an answer relied on | `audit.jsonl`, `document_question` events (`sources`: document id, passage, score) |
| Proof the log was not edited | `GET /admin/audit/verify` or the admin page's chain status |

## Monthly operating procedure

1. Check that the admin page shows `chain intact`, then confirm the latest archive is in the WORM store.
2. Review the admin keys list; rotate any that have not been used in >90 days.
3. Review per-key request and token counts on the admin page; investigate anything unusual.
4. Rotate any key that has leaked outside the intended application.
5. Review each collection's document list; remove superseded policies so answers do not cite outdated versions.
