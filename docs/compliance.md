# Compliance notes

This kit was built because enterprise AI deployments keep failing at the same wall: *"We can't send our data to a public API."* Healthcare, legal, fintech, and anything touching regulated workloads all share this constraint. The compliance posture that makes an on-prem LLM gateway actually usable has fewer moving parts than people expect — this doc walks through them.

## Load a profile

```bash
cp profiles/healthcare.env .env    # or profiles/finance.env
# then edit for your paths and model choice
```

The profiles are opinionated defaults, not legal advice. Review each setting with your compliance team.

## What the kit covers out of the box

- **Nothing calls out.** The gateway only talks to Ollama on `127.0.0.1`. No telemetry, no analytics, no update ping.
- **Localhost-only bind.** Default `GATEWAY_HOST=127.0.0.1` keeps the API off the network. Put a reverse proxy on the VPN interface if remote access is needed.
- **Per-key auth with revocation.** No key is ever shared — each application gets its own with its own rate limit and audit trail.
- **Tamper-evident audit log.** Admin actions are always recorded; prompts and responses are recorded with `GATEWAY_LOG_PROMPTS=true`, PII-redacted with `GATEWAY_REDACT_PROMPTS=true`. Entries are hash-chained, and `/admin/audit/verify` reports the first tampered line.
- **Usage metrics are local-only.** The admin UI reads directly from the local SQLite file.
- **Documents stay on the machine.** Uploaded documents, their passages, and their embeddings are stored in the gateway's SQLite file; embedding and answering both run through the local Ollama. Only admin keys can add or remove documents, and both actions are audited.
- **Every document question is traceable.** A `document_question` entry records the key, the collection, the model, and the exact document passages the answer drew on, whether or not prompt logging is on. That answers the auditor's question "what did the system rely on when it said this?"

## What the kit does NOT cover (and shouldn't try to)

- **Formal DLP.** Use a commercial DLP at egress if the deployment is sensitive enough to need one.
- **Zero-trust networking.** Pair with Tailscale, Twingate, or your enterprise VPN. Don't rely on this gateway as the only perimeter.
- **Model safety / content moderation.** Add Llama Guard, Lakera, or a custom classifier in front of `/v1/chat/completions` if your use case needs it.
- **Clinical validation / medical device certification.** Healthcare deployments that make clinical decisions need a formal validation program this doc won't substitute for.

## WORM / immutable log retention

`audit.jsonl` is the operational copy. To rotate it: verify the chain (`GET /admin/audit/verify`), copy the file to WORM storage, then move it aside. The next entry starts a new chain; record the archived file's last `entry_hash` in your retention log so the two can be checked together. Ship archives to one of:
- **AWS**: S3 Object Lock in Compliance mode
- **Azure**: immutable Blob storage
- **GCS**: Bucket Lock with retention policy
- **On-prem**: a WORM NAS or an immutable ZFS dataset with retention set at the OS level

Retention periods common in each sector:
- Healthcare (US, HIPAA): 6 years from creation
- Finance (SOX / SOC2): 7 years
- Legal: matter-dependent; consult the firm's records policy
- Fintech (various): usually 5-7 years, often extended by consent decrees

## Air-gap deployment

1. On a connected host, pull Ollama, the target model, and the embedding model (`ollama pull llama3.1:8b && ollama pull nomic-embed-text`). Model artifacts live under `~/.ollama/models`.
2. On a connected host, run `pip download -r requirements.txt -d wheels/`.
3. Transfer `~/.ollama/models/` and `wheels/` to the air-gapped host.
4. Install Ollama from an offline package (`ollama/ollama` releases page) and copy the models directory to `~/.ollama` there.
5. Install the gateway: `pip install --no-index --find-links=wheels/ -r requirements.txt`.
6. Start the gateway. No outbound connection is needed at runtime.

## Scoped network listening

Replace `GATEWAY_HOST=127.0.0.1` with the specific VPN or private subnet interface IP, not `0.0.0.0`. On Linux, confirm with `ss -tlnp`.

## Where the audit evidence lives

| Evidence | Location |
|---|---|
| Which keys exist and how much each has used | `GATEWAY_DB_PATH` (SQLite `api_keys`: request and token counters) |
| Prompt + response text (optional, redactable) | `GATEWAY_LOG_DIR/audit.jsonl`, `completion` events |
| Key creation / revocation, model downloads | `audit.jsonl` (`key_created`, `key_revoked`, `model_pull_*`) and SQLite `api_keys` |
| Which documents were in a collection, and who added or removed them | SQLite `documents` table; `audit.jsonl` (`document_added`, `document_removed`) |
| Which documents an answer relied on | `audit.jsonl`, `document_question` events (`sources`: document id, passage, score) |
| Proof the log wasn't edited | `GET /admin/audit/verify` or the admin page's chain status |

A reasonable monthly operating procedure:
1. Check the admin page shows `chain intact`, then confirm the latest archive is in the WORM store
2. Review the admin keys list — rotate any that haven't been used in >90 days
3. Review per-key request and token counts on the admin page; investigate anything unusual
4. Rotate any key that's leaked outside the intended application
5. Review each collection's document list; remove superseded policies so answers don't cite outdated versions
