# Testing and evidence

The test suite is the platform's primary evidence base: each control in [controls.md](controls.md) and
[threat-model.md](threat-model.md) points at a test that exercises it. This document describes what the suite
covers, how upstream services are simulated, and how the documentation itself is kept honest.

## Scope

- **251 automated tests** (186 through v0.6, 32 added in v0.7, 33 since; parametrized cases counted
  individually), all passing, run in CI with `ruff check`, `ruff format --check`, `shellcheck` and (CI only)
  `helm lint`.
- Upstream HTTP is mocked for both backends: Ollama's native API and the OpenAI wire format (chat, SSE
  streaming with usage, embeddings, models, errors before the first token).
- **Answer-quality evals gate CI.** `scripts/rag_eval.py --check` runs the real ingestion, access-control and
  retrieval code over the sample library (59 documents, 3 restricted) with 59 golden questions asked as five
  personas (9 of them to decline) and fails on a drop below `evals/thresholds.json` or any ACL leak. Results and their limits are in
  [benchmarks.md](benchmarks.md#retrieval-quality-measured-golden-set).
- **Documentation is tested.** Every `tests/…::test_name` and `gateway/…::symbol` referenced in `docs/` and the
  README must exist (`tests/test_docs.py`), and every metric the Grafana dashboard queries must be exported
  (`tests/test_metrics.py`).
- **The console is tested.** Its browser engine runs under CPython in the suite
  (`tests/test_demo_engine.py`, `tests/test_console_engine.py`), and `scripts/demo_smoke.py` drives the real
  console headlessly with Playwright in both modes: every screen and its key interaction in demo mode, then the
  gateway and the simulated backend under uvicorn in live mode (42 checks, no console errors, no horizontal
  scroll at 390 px).

## Coverage by test file

| Test file | Covers |
|---|---|
| `test_helm_chart.py`, `test_airgap_bundle.py` | Values validated against `values.schema.json` (unsafe values rejected); templates rendered by a Go `text/template` harness (not helm) and checked (hardening, probes, GPU, NetworkPolicy, offline vLLM, keyring, OIDC read back through `Settings`, failing renders); air-gap dry run, real bundle, checksum verify and tamper detection, shellcheck |
| `test_supply_chain.py` | Lock-file validation; enforce/warn across chat, embeddings, ingestion and questions; digest swap caught by admin re-verify, by the interval, and re-checked after pulls; startup verification audited and bounded; weight-file hashing for OpenAI-compatible servers; ML-BOM validated against the CycloneDX 1.6 schema; trust-on-first-use pinning |
| `test_encryption.py`, `test_backup_restore.py` | Sealed passages and vectors on disk, row binding, wrong key, hidden documents' keys never unwrapped, encrypting existing data, rotation and retirement, sealed audit fields with key-free verification, startup validation; full backup/restore drill and refusal cases |
| `test_retrieval.py` | BM25, RRF, rank modes, lexical and cross-encoder rerankers, per-mode `/ask` (BM25 doesn't embed the question), the eval gate meets thresholds with no ACL leaks, and published eval numbers match a fresh run |
| `test_acl.py` | Permission-aware retrieval: no cross-group leakage in answers, citations, injection flags, counts, listings or prompts; SQL filtering before scoring; collection ACLs, restricted default, OIDC groups, per-user grants; audited ACL changes; v0.5 database upgrade |
| `test_identity.py` | OIDC: RS256/ES256, JWKS discovery, caching, rotation and outage; `alg` allow-list (none, HMAC confusion); `iss`/`aud`/`exp`/`nbf`/skew; groups → roles on admin routes and collections; metrics privacy; real HTTP JWKS |
| `test_gateway.py` | Auth, admin-only routes, OpenAI-shaped responses, usage accounting, rate limit, revocation, last-admin guard, background pulls, redacted audit entries, tamper detection |
| `test_documents.py` | Ingestion, retrieval picks the right document, citations, injection guard prompt, audited questions with redaction, PDFs, upload limits |
| `test_backends.py` | Both backends: chat, streaming + `include_usage`, embeddings order, models, upstream auth, 501 pull, error mapping, pooled HTTP client |
| `test_metrics.py`, `test_telemetry.py` | `/metrics` labels (route templates, key labels, never key values), tokens, TTFT, 401/429, token protection; GenAI span attributes, no prompt text, no-op without OTel |
| `test_injection_flags.py`, `test_redact.py` | Injection heuristics (payloads flagged, ordinary text not); redaction patterns |
| `test_console.py`, `test_console_engine.py` | Console endpoints (overview, audit entries, access matrix, runtime policy with validation, effect and audit, rate-limit window, lock validation, ML-BOM), admin-only access, per-request retrieval switches, the simulated mock backend and the compose stack end to end, eval data drift, page assets; the browser engine's console calls (pin, re-pull, enforce, policy, in-browser eval) |
| `test_sample_library.py`, `test_sample_company.py` | The demo library and usage file are reproducible and consistent with each other: one department list for owners, personas and usage; reviews within 12 months; no duplicated or carry-over wording; the kiosk reads only Member information; each persona reads what its role allows; every suggested question cites the document it demonstrates; *All sources* hides restricted documents and declines off-topic questions. Usage: headcounts sum to employees, 30-day actives at least the busiest day, topics add up to questions for every range and are asked only by departments that can read them |
| `test_loadtest.py`, `test_demo_engine.py`, `test_deploy_config.py`, `test_docs.py`, `test_ingest_folder.py` | Load-test math and an in-process run; demo engine; compose/profile sanity; doc references; folder ingest |

## Running the checks

```bash
pip install -r requirements.txt ruff opentelemetry-sdk
ruff check gateway tests scripts demo && ruff format --check gateway tests scripts demo && pytest -q
python scripts/rag_eval.py --check   # retrieval eval gate (also run inside pytest)
python scripts/rag_eval.py --console-data   # refresh demo/data/rag_eval.json (a test checks it matches)
python scripts/demo_smoke.py --live          # optional: headless browser test of both console modes
                                             # (needs playwright + chromium; --pyodide-dir for an offline Pyodide)
```

CI (`.github/workflows/ci.yml`) also runs `scripts/sample_library.py --check`,
`scripts/generate_sample_company.py --check`, `shellcheck scripts/*.sh`, `helm lint`, `helm template` for both
example values files, and an air-gap bundle dry run. A separate job runs the console smoke test in live mode;
demo mode loads Pyodide from a CDN and does not fail the build on a CDN outage.
