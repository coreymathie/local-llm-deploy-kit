# Changelog

## Unreleased

The console is set in a business and opens on an employee assistant, the way an internal Copilot-style tool
does, with a 57-document library behind it. 230 tests, RAG evals with no ACL leaks (hybrid citation accuracy
0.837 → 0.884), and a 41-check browser smoke test across both console modes.

### Harbor Assistant
- **Ask** is the landing screen: streamed answers with numbered citations and source cards; a citation opens
  the document with the quoted passage highlighted, its owner, version, review date and who may read it.
  Conversation history, suggested questions per person, copy, ask again, helpful / not helpful, ask as someone
  else or compare two people side by side (`demo/assistant.js`).
- **All sources I can read** (default) searches every collection the person may read. The access decision runs
  per collection before any passage is scored; passages are then ranked together. In live mode the console asks
  each readable collection in parallel and keeps the strongest answer.
- Answers quote only sentences from the best-matching document, and a sentence holding a number from the
  question wins over its neighbours. A question nothing answers well gets "I couldn't find that in the documents
  you can access" instead of an unrelated quote (vector-similarity floor; console only, not applied to the eval).
- A document retrieved for a question that contains instruction-like text shows a warning on the answer, cited
  or not.

### Console: views, navigation, themes
- **Business and technical views** (header switch or `?view=technical`). The business view uses plain language
  (access as "Restricted to the hr group", document titles, friendly audit-event names); the technical view adds
  retrieval controls and scores, API keys and token limits, raw access-list entries, digests, the lock file,
  ML-BOM and runtime modules.
- Navigation regrouped as Assistant (Ask), Admin (Usage and impact, Documents, People and keys, Audit log) and
  Governance (Access policy, Models, Answer quality, Settings). Overview is now **Usage and impact**.
- Light and dark themes (follow the system; header toggle), a tour invite instead of an automatic tour, a
  not-found page, and screens that need no engine (Ask, Usage and impact) render before Pyodide finishes.
- Documents: titles, owners and review dates, a filter, and a plain-language access matrix. Audit log: paging
  and a "Log intact" summary. Access policy: an "in effect now" summary above the JSON. Answer quality: labelled
  as measured on the benchmark library.
- The header keeps its controls at full size on narrower desktops; the breadcrumb gives way first.
- Fixed: feedback buttons toggled twice per click after the chat re-rendered (stacked click listeners), and an
  answer to a question sent while the chat re-rendered was painted into the old page and never shown.

### Document library
- `scripts/sample_library.py` writes 44 more documents across five collections (staff policies, member services,
  lending, compliance, branch operations) and `demo/data/library.json`, the catalog the console and the usage
  file read (owners, departments, versions, review dates, access lists). OFAC screening is restricted to
  `group:compliance`. CI checks the library is current; `tests/test_sample_library.py` checks the catalog, that
  the usage file's collections match it, and cross-collection answers and access.
- The original documents lose the "(fictional sample)" title suffix: the workspace is labelled instead.

### Console: business impact
- **Overview › Business impact** for a sample company, Cypress Harbor Credit Union (fictional: 340 employees,
  10 departments, 11 branches). Over 7, 30 or 90 days: questions answered, answered from documents with
  citations, employees using it, hours saved and cost per answer (assumptions shown), restricted content
  withheld, member data sent outside, answers rated helpful, each against the previous period with trend lines;
  questions per day; adoption by department; topics; response time; collections and who may read them;
  governance checks; recent activity. The previous Overview is now **Overview › This session**.
- **Navigation**: screens grouped by job (Monitor, Use, Govern, Configure) with sub-pages, breadcrumbs in the
  header, a command palette (Ctrl/Cmd+K or `/`) over screens, actions and people to ask as, `g` + letter
  shortcuts with a `?` sheet, an audit-entry badge, a workspace label for the sample company, and a collapsible
  sidebar (`demo/shell.js`, shared in design with the portfolio's other consoles).
- Charts: the y axis of stacked columns sizes itself to its labels; horizontal bars take a label width.

### Demo data: sample company
- `scripts/generate_sample_company.py` writes `demo/data/sample_company.json` from a fixed seed and stated
  assumptions; CI checks it's current, and `tests/test_sample_company.py` checks its arithmetic and that it's
  labelled fictional.
- The demo library grows from 5 to 13 fictional credit-union documents: card disputes, wire verification,
  consumer lending, member identity verification, complaint handling, branch security, AI acceptable use, and a
  BSA/AML escalation procedure restricted to `group:compliance`. The existing documents keep their facts.
- People are named for the setting (Priya Shah, HR; Dana Ortiz, engineering; Audrey Kim, internal audit;
  branch lobby kiosk), and Marcus Bell (BSA officer, groups staff + compliance) is added. New tests check that
  only the compliance group retrieves the BSA procedure and that the new policies answer with citations.

## [0.7.0] — 2026-10

The project is now **Private LLM Platform** (repository `private-llm-platform`, formerly
`local-llm-deploy-kit`). Python package and import paths (`gateway`, `scripts`) and the Helm chart name are
unchanged.

Upgrade notes (behavior changes; details under Changed)
- `docker compose up` now starts the gateway with a **simulated backend** (`mock-llm`, no model) instead of
  Ollama. For real answers: `docker compose --env-file profiles/compose-ollama.env --profile ollama up -d`.
  The gateway's own default backend is still Ollama (`BACKEND=ollama` in `gateway/config.py`, the installers
  and the Helm chart). Container names changed from `lldk-*` to `plp-*`.
- Renamed paths in the healthcare and finance profiles (`/var/lib/private-llm-platform`,
  `/var/log/private-llm-platform`, `/etc/private-llm-platform`) and the installers' default directory
  (`~/.private-llm-platform`); move existing state or set the old paths explicitly.

Added
- **Product console** in `demo/` (`index.html`, `app.js`, `screens.js`, `adapters.js`, `ui.js`, `styles.css`),
  replacing the single-page demo at the same URL. Screens: Overview (KPIs, activity and audit-event charts,
  guided cards), Chat (cited answers with a source panel, persona/key switcher, side-by-side compare,
  per-request retrieval mode and reranker, model-only chat), Documents (library, paste/upload, collection and
  document access lists, access matrix), Users & Keys (keys with groups, rate-limit window, burst test,
  revoke; SSO group-to-role mapping, token users), Audit (filterable log, per-entry decision timeline,
  demo-only tamper/restore), Models (verification, off/warn/enforce, lock validation, ML-BOM), Policies
  (validate/apply the runtime policy with a before/after scenario), Evals (rag_eval scorecard against the
  thresholds), Settings. Hash routing with deep links, a five-step guided tour, light and dark themes,
  phone layout. Inline-SVG charts, no chart library.
- **Two modes from one codebase**: `DemoAdapter` runs the gateway's modules in Pyodide 0.26.4 (now also
  `console.py`, `supply_chain.py`, `scripts/mlbom.py`, `scripts/rag_eval.py`), with a simulated Ollama model
  inventory for the supply-chain screens; `LiveAdapter` calls the gateway's HTTP API with an admin key or
  token entered in Settings. Mode from `GET ./api-mode` or `?mode=`.
- The gateway serves the console at **`/console`** (static files, no secrets; every data call needs a bearer
  credential) and `GET /console/api-mode`.
- `gateway/console.py`: overview counters, audit rows with line numbers and summaries, access matrix, and the
  runtime-editable policy (validated with the `Settings` field types and `identity.check_role`), shared by
  the API and the browser engine.
- Admin endpoints: `GET /admin/overview`, `GET /admin/audit/entries`, `GET /admin/access-matrix`,
  `GET /admin/policy`, `POST /admin/policy/validate`, `PUT /admin/policy` (in memory, audited as
  `policy_changed`), `GET /admin/rate-limits`, `POST /admin/models/lock/validate`, `GET /admin/models/mlbom`.
- `/v1/collections/{c}/ask` accepts `retrieval_mode` and `reranker` per request (ranking only; access is
  unchanged) and returns `timings_ms` (access, retrieval, generation), also recorded in the audit entry.
- `scripts/mock_openai_server.py` `MOCK_MODE=simulated`: hashed embeddings and extractive, cited answers
  from the demo engine, so the live console works with no model. Default mode (canned tokens) unchanged.
- `scripts/rag_eval.py --console-data` writes `demo/data/rag_eval.json` (a test checks it matches a fresh
  run); `evaluate_async()` for callers inside an event loop (the console re-runs the eval in the browser).
- `supply_chain.parse_lock()` validates a parsed lock document (used by `load_lock()` and the console).
- `scripts/demo_smoke.py` rewritten: every screen in demo mode with its key interaction, plus `--live`
  (gateway and simulated mock backend under uvicorn), console-error check, no horizontal scroll at 390 px
  on every screen, desktop and mobile screenshots. CI job `console-smoke` (live mode required, demo mode
  allowed to fail on CDN problems).
- `profiles/compose-ollama.env`; `docs/img/console.png`.
- Tests: `tests/test_console.py` (endpoints, admin-only access, policy effects and audit, retrieval
  overrides, simulated mock, compose stack end to end, eval data drift, page assets) and
  `tests/test_console_engine.py` (the browser engine's console calls).

Changed
- `docker-compose.yml`: default services `gateway` + `mock-llm`; `ollama` and `vllm` are profiles; the
  gateway image is built from the `Dockerfile` (which now includes `demo/`).
- Admin page title and a link to the console; `/admin` keeps working.
- Rate limit in the browser demo is 30/min (was 10).
- Renamed references to the repository (README, badges, Pages URL, Helm chart home, installers, ML-BOM
  application component, OpenTelemetry tracer name, Grafana dashboard title, air-gap bundle source archive).

## [0.6.0] — 2026-10

Upgrade notes (behavior changes; details under Changed)
- Ranking: hybrid retrieval is the default, and `/ask` sources' `score` is now the final ranking score, not cosine similarity (that is `scores.vector`). `GATEWAY_RETRIEVAL_MODE=vector` restores v0.5 ranking.
- Healthcare and finance profiles now set `GATEWAY_COLLECTION_DEFAULT_ACCESS=restricted` and `GATEWAY_MODEL_POLICY=enforce`: grant collection access and pin models (`scripts/pin_models.py`) before users can ask questions.
- Startup now fails on an unsafe OIDC configuration, encryption enabled without a loadable key, or a malformed model lock file.
- The SQLite schema gains columns and tables, added in place on startup; v0.5 databases keep working, but a database opened by 0.6 is not tested with 0.5.
- Internal Python API: auth dependencies return `Principal`; `rag.retrieve()` and `rag.list_documents()` take an access decision.

Added
- **OIDC / JWT authentication alongside API keys** (`gateway/identity.py`, ADR 0005). With `GATEWAY_OIDC_ENABLED=true`, bearer JWTs are verified against the issuer's JWKS (discovered from the issuer or configured), with an RS256/ES256 allow-list checked before key lookup (no `none`, no HMAC), key-type matching, required `iss`/`aud`/`exp`/`sub`, configurable clock skew, a 16 KiB size cap, JWKS caching, refetch on unknown `kid` for key rotation (throttled to once per 30 s), and fail-closed behavior when the IdP stays unreachable. Unsafe settings fail at startup.
- **Roles**: `admin`, `user`, `reader:<collection>` / `reader:*`, mapped from a configurable groups claim (`GATEWAY_OIDC_GROUP_ROLES`; dotted claim paths for nested claims). API keys keep their meaning (admin keys: `admin`; others: `user`). Reader-only callers can list and ask their collections but not chat, embed or list models. `GATEWAY_COLLECTION_DEFAULT_ACCESS` (`open` by default, the v0.5 behavior, or `restricted`).
- `GET /v1/me` (caller identity, roles, readable collections) and `GET /admin/users` (token users and their usage). Token callers are rate-limited per subject, attributed as `user:<username>` in the audit log, and labelled `oidc` in metrics (no user names in label values).
- Admin page: sign in with an API key or a pasted access token; cards are shown by role.
- `tests/test_identity.py` (fake IdP with locally generated RSA and EC keys in `tests/idp.py`, plus a real HTTP JWKS endpoint on 127.0.0.1).
- Dependencies: `PyJWT`, `cryptography`.
- **Permission-aware retrieval** (OWASP LLM08). Collection access lists (`GET|PUT /admin/collections/{c}/acl`) and document access lists (`acl` on upload/text add, `PUT /v1/collections/{c}/documents/{id}/acl`) with entries `group:<name>`, `key:<label>`, `user:<username>`. `rag.access_for()` decides per caller before any passage is loaded; `rag.candidates()` filters in SQL, so hidden passages are never scored, prompted, cited, flagged or counted. Audit: `access` decision on every `document_question` (basis, documents visible/hidden, rule per source), `document_question_denied`, `collection_acl_changed`, `document_acl_changed` (before/after). API keys can carry `groups` (set at creation).
- Admin page: access-list fields for uploads and collections; an Access column for admins.
- Browser demo panel 5: switch between SSO personas and API keys in different groups and see different answers and citations over two restricted sample documents; `scripts/demo_smoke.py` checks it.
- `tests/test_acl.py`, including a test that a v0.5 database upgrades in place.
- **Hybrid retrieval** (`gateway/retrieval.py`, ADR 0006): pure-Python BM25 over the caller's readable passages, fused with the cosine ranking by Reciprocal Rank Fusion. `GATEWAY_RETRIEVAL_MODE` (`hybrid` default, `vector`, `bm25`), `GATEWAY_RRF_K`. Pluggable `Reranker` interface with a deterministic `lexical` reranker; `cross_encoder` (local sentence-transformers model) is interface only, not tested here. `/ask` sources carry `scores` (`vector`, `bm25`, `rrf`, `rerank`); responses and audit entries record the `retrieval` configuration.
- **RAG eval gate**: `scripts/rag_eval.py` over `evals/golden/` (12 fictional documents, 2 restricted; 43 questions) reports recall@1, recall@k, MRR, citation accuracy, answer-contains and ACL leaks per configuration, offline and deterministic (demo embedder, extractive answers); `--check` enforces `evals/thresholds.json` and runs in CI and in pytest. Measured values in `docs/benchmarks.md` are checked against a fresh run by a test.
- Browser demo: retrieval mode selector and lexical-reranker toggle, with per-source score components (17 smoke checks).
- **Encryption at rest** (`gateway/crypto.py`, ADR 0007): envelope encryption with AES-256-GCM, a data key per document sealing its passages' text and vectors (bound to document, passage and field), wrapped by a key-encryption key from a keyring file or environment key behind a `KeyProvider` interface (KMS providers documented, not shipped). Optional sealing of audit free-text fields (`GATEWAY_AUDIT_ENCRYPT_TEXT`); the hash chain still verifies without keys. Decryption happens after the access filter. Fails closed (startup check; 500 on decryption failure).
- `scripts/keys.py`: `generate` (0600 keyring), `status`, `encrypt-existing` (then `VACUUM`), `rewrap` (rotation without re-encrypting passages), `retire` (refuses while a key is active or still referenced).
- `scripts/backup.py` / `scripts/restore.py`: online SQLite backup plus audit log with a SHA-256 manifest and needed key ids (never keys); restore checks checksums, SQLite integrity, the audit chain and key availability before changing anything, moves existing files aside with `--force`, and re-verifies. `scripts/bench_storage.py` measures encryption overhead and backup/restore time.
- `docs/operations.md`: state inventory, encryption, rotation, KMS interface, backup, restore, RTO/RPO guidance (no promised numbers; measured figures labeled).
- Profiles: commented encryption settings for healthcare and finance.
- **Model supply chain** (`gateway/supply_chain.py`, ADR 0008): a model lock file (`GATEWAY_MODEL_LOCK_FILE`, `models.lock.example.json`) pins Ollama manifest digests or SHA-256 of weight files; verification at startup (bounded), after pulls, on `POST /admin/models/verify` and lazily every `GATEWAY_MODEL_VERIFY_INTERVAL_SECONDS` for Ollama; statuses verified/mismatch/missing/unpinned/unverifiable/error; `GATEWAY_MODEL_POLICY` off/warn/enforce across chat, embeddings, document questions and ingestion; audited `model_verification`; metric `gateway_model_policy_decisions_total`; `GET /admin/models/verification`.
- **Helm chart** `deploy/helm/local-llm-gateway`: gateway Deployment (one replica, Recreate, non-root, read-only root filesystem, dropped capabilities, seccomp, startup/liveness/readiness probes), PVC, ConfigMap, Secret or `existingSecret`, keyring Secret mount, model lock ConfigMap, default-deny NetworkPolicies (gateway and vLLM), optional ServiceMonitor, optional vLLM Deployment with `nvidia.com/gpu` resources, cache PVC, shared memory and offline mode, `helm test` pod; `values.schema.json`; example values for GPU and air-gapped installs; renders fail on incomplete security settings. **Not validated with helm here** (it couldn't be downloaded in the build environment): tests validate values against the schema and render the templates with a stdlib Go `text/template` harness (`tests/helm_render/render.go`); CI adds `helm lint` and `helm template`.
- `Dockerfile` for the gateway image (non-root, state under `/data`, optional offline wheel install); not built here (no Docker daemon).
- `scripts/airgap_bundle.sh`: wheels (cross-platform), saved images, Ollama manifests and blobs, Hugging Face snapshots, source, chart, model lock, ML-BOM, `INSTALL.txt` and `SHA256SUMS`; `--dry-run` and `--verify`. `docs/kubernetes.md`.
- `scripts/pin_models.py` (trust-on-first-use pinning, keeps license/source metadata) and `scripts/mlbom.py` (CycloneDX 1.6 ML-BOM of pinned models and resolved Python packages, validated against the CycloneDX schema in tests; CI installs `cyclonedx-python-lib[json-validation]` for that test).

Changed
- `GET /v1/collections` lists only collections the caller may read; `GET /v1/collections/{c}/documents` returns an empty list and `/ask` returns the same 404 as for a missing collection when the caller may not read it (no existence oracle). With default settings, every API key can still read every collection, as in v0.5.
- Database: new columns `documents.kek_id` and `documents.wrapped_dek` (encryption at rest), added in place.
- `audit.verify()` accepts an optional path (used to verify backups); `audit.recent()` opens sealed fields for display.
- Database: new columns `api_keys.groups` and `documents.acl` and tables `principal_usage`, `collection_acls`, added in place on startup (additive; v0.5 databases keep working).
- **Profiles (behavior change):** `profiles/healthcare.env` and `profiles/finance.env` set `GATEWAY_COLLECTION_DEFAULT_ACCESS=restricted`, so collections need an explicit grant before non-admin callers can read them, and `GATEWAY_MODEL_POLICY=enforce` with a lock file path, so no model is served until it is pinned (`scripts/pin_models.py`).
- Default `GATEWAY_MODEL_POLICY=warn`: without a lock file every model is reported `unpinned` (logged once per verification run, counted in metrics) but served as before; the gateway now calls the backend's model list at startup and every 5 minutes (Ollama).
- **Retrieval default (behavior change):** `GATEWAY_RETRIEVAL_MODE=hybrid` is the new default, and a source's `score` is now the final ranking score (an RRF value in hybrid mode) instead of cosine similarity; the cosine value is in `scores.vector`. Set `GATEWAY_RETRIEVAL_MODE=vector` for v0.5 ranking.
- `rag.retrieve()` takes a required `access` argument and `rag.list_documents()` an `access` decision (internal API).
- Auth dependencies return a `Principal` (identity, roles, groups) instead of the `ApiKey` record; `require_key` keeps its name and now accepts tokens too.

Fixed
- Admin page escapes API key values in the keys table (they were inserted into an attribute unescaped).
- Browser demo: long suggestion chips wrap instead of widening the page on phones.

## [0.5.0] — 2026-10

Added
- **Pluggable inference backends** (`gateway/backends.py`). `BACKEND=ollama` (default, unchanged API behavior) or `BACKEND=openai_compatible` for vLLM, SGLang, TGI, NVIDIA NIM or any server with `/v1/chat/completions`, `/v1/embeddings` and `/v1/models` (`OPENAI_COMPAT_BASE_URL`, optional `OPENAI_COMPAT_API_KEY`). Routes and RAG call the interface; client keys never go upstream; `/v1/pull` returns 501 on non-Ollama backends. Opt-in `vllm` Docker Compose profile for NVIDIA hosts.
- `stream_options.include_usage` on `/v1/chat/completions` streams (a final usage chunk, as in the OpenAI API). Streaming requests now read the first upstream event before answering, so a failing backend returns 502/4xx instead of a broken 200 stream.
- **Prometheus `/metrics`** (`gateway/metrics.py`): requests by route template, status and key *label*; latency, backend latency and time-to-first-token histograms; tokens per key and model; backend errors by reason; build info. Optional bearer protection (`GATEWAY_METRICS_TOKEN`), off switch (`GATEWAY_METRICS_ENABLED`). Grafana dashboard and scrape config in `deploy/`.
- **OpenTelemetry GenAI spans** (`gateway/telemetry.py`) for chat, streaming chat and embeddings, with `gen_ai.*` attributes and `error.type`; no prompt text; no-op without OpenTelemetry installed.
- **Prompt-injection flags**: `rag.injection_signals()` marks retrieved passages that look like injected instructions; flags appear in `/ask` responses (`injection_flags`) and in `document_question` audit entries. Flag-and-audit, not blocking (ADR 0004).
- `scripts/loadtest.py`: async load test reporting TTFT, latency p50/p95/p99, RPS, error rate and tokens/s per concurrency level, as Markdown or JSON. `scripts/mock_openai_server.py`: OpenAI-compatible stub for measuring gateway overhead.
- **Browser demo** (`demo/`, served by GitHub Pages): the gateway's `store`, `auth`, `audit`, `redact`, `rag` and `backends` modules run unmodified in Pyodide, with panels for keys and rate limiting, redaction, audit tampering and verification, and cited document Q&A over three fictional sample policies. Demo mode uses hashed bag-of-words embeddings and extractive answers (no LLM), labelled on the page. `scripts/demo_smoke.py` tests it headlessly with Playwright.
- Docs: ADRs 0001–0004, `docs/threat-model.md` (STRIDE + OWASP LLM Top 10 2025), `docs/controls.md` (HIPAA 164.312, SOC 2 CC6/CC7, NIST AI RMF / AI 600-1, ISO/IEC 42001, with code, test and gaps per row), `docs/benchmarks.md` (method, template, measured gateway overhead). README restructured.
- 69 new tests (89 total), including tests that every code/test reference in the docs exists and every metric the dashboard queries is exported. CI also lints `demo/` and installs `opentelemetry-sdk` so span tests run.

Changed
- Upstream HTTP uses one pooled client per event loop instead of a new client per call. Building a client cost about 48 ms of blocking CPU on the test host and capped throughput at about 17 requests/s; measured gateway throughput with a stub upstream rose to about 100–110 requests/s (`docs/benchmarks.md`).
- httpx no longer logs every upstream request at INFO.
- `/health` returns `backend` and `backend_ok`; the `ollama` field is kept when the backend is Ollama. The admin page shows the backend name.
- `prometheus-client` added to `requirements.txt`.

Fixed
- Phone redaction left the opening parenthesis of numbers like `(415) 555-0134` in place.
- Profile comments claimed redaction of account numbers and PHI; they now list what is actually redacted.
- Admin page escapes error messages before rendering them.

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
