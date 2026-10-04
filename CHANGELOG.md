# Changelog

## [0.4.0] — 2026-10

Added
- **Private document Q&A** (`gateway/rag.py`). Upload PDFs, Markdown, text, CSV, JSON, or HTML into named collections; ask questions and get answers drawn only from those documents, with numbered citations and the matching excerpts. Passages are paragraph-aware with overlap, embedded by a local Ollama model (`nomic-embed-text` by default), stored in the gateway's SQLite file, and ranked by cosine similarity. Nothing leaves the machine.
- Prompt-injection guard: retrieved text is passed as numbered sources, and the system prompt tells the model to ignore instructions inside documents.
- Each returned source is flagged `cited` when the answer references it; the audit entry records the same flag, and the admin page shows cited sources first with the rest collapsed.
- Endpoints: `GET /v1/collections`, `GET|POST /v1/collections/{c}/documents`, `POST /v1/collections/{c}/documents/text`, `DELETE /v1/collections/{c}/documents/{id}`, `POST /v1/collections/{c}/ask`, and an OpenAI-shaped `POST /v1/embeddings`. Adding and removing documents is admin-only; uploads are size-limited (`GATEWAY_MAX_UPLOAD_MB`).
- Audit events `document_added`, `document_removed`, and `document_question`. Question entries always record the key, collection, model, and source passages; question and answer text follow the prompt-logging and redaction settings.
- **Documents card** on the admin page: collections, upload or paste, remove (two-click confirm), and a question box that shows the answer with clickable citations and the source excerpts.
- Screenshot of the Documents card in the README.
- `scripts/ingest_folder.py`: load a folder into a collection; re-runs only add new files, `--replace` refreshes changed ones, `--dry-run` previews.
- Installers and Docker Compose pull and configure the embedding model.
- 10 new tests (20 total): retrieval and citations, injection guard, audited questions with redaction, admin-only changes, validation and upload limits, PDF parsing, unit-length embeddings, chunk overlap, and the ingest script. Shared fixtures moved to `tests/conftest.py`.

Fixed
- Admin page tables scroll inside their cards on phones instead of widening the page.

## [0.3.0] — 2026-10

Added
- **Tamper-evident audit log** (`gateway/audit.py`). Admin actions are always recorded (bootstrap, key created/revoked, model pull started/finished); prompts and completions are recorded when enabled. Entries are SHA-256 hash-chained.
- `GET /admin/audit/verify` and `GET /admin/audit/recent`, plus an **Audit log** card on the admin page showing chain status and recent events.
- **PII redaction** for audit entries (`GATEWAY_REDACT_PROMPTS`), on by default in the healthcare and finance profiles.
- Test suite (10 tests) against a mocked Ollama: auth, admin-only routes, OpenAI-compatible output, usage accounting, rate limits, revocation, last-admin guard, background pulls, redaction, tamper detection. CI runs ruff, format check, pytest, and shellcheck.

Fixed
- macOS installer used Ollama's Linux install script; it now uses Homebrew or points to the macOS app.
- Windows installer used an NSIS silent flag on Ollama's Inno Setup installer; now `/VERYSILENT`. Also installs git/Python via winget if missing and doesn't start a second Ollama if one is already running.
- All installers wait for health checks instead of fixed sleeps.
- Model pulls no longer block the request for minutes; they run in the background and return 202.
- Admin key in the browser moved from localStorage to sessionStorage (cleared when the tab closes). Key labels are HTML-escaped.
- FastAPI startup moved from the deprecated `on_event` to `lifespan`; dependencies use `Annotated`.
- The last active admin key can't be revoked (409), so an operator can't lock everyone out.
- `/v1/models` returns 502 instead of 500 when Ollama is down, and the admin page shows a clear message instead of an error.


## [0.2.0] — 2026-10

Added — regulated-workload profiles and compliance documentation.

- `profiles/healthcare.env` — HIPAA-adjacent defaults: localhost bind, lower
  RPM, full prompt audit log on, `/var/log` + `/var/lib` paths for standard
  service placement.
- `profiles/finance.env` — fintech defaults drawn from dispute-ops deployment
  patterns: larger reasoning model, mandatory audit logging, WORM-ready layout.
- `docs/compliance.md` — what the kit covers, what it deliberately doesn't,
  air-gap install procedure, retention periods by sector, and a monthly
  operating procedure for audit evidence.
- Author line (`Corey Mathie, 2026`) added to every source file.

## [0.1.0] — 2026-10

Initial release.

- Ollama + FastAPI OpenAI-compatible gateway on `:8080`
- API-key auth with per-key rate limits
- Single-file admin UI with dark/light themes
- One-liner installers for macOS, Linux, and Windows
- Docker Compose option
- Prompt + response JSONL audit log (toggleable)
