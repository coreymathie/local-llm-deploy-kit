# Controls mapping

This document maps the platform's technical controls to the HIPAA Security Rule, SOC 2, the NIST AI RMF and
its Generative AI Profile, ISO/IEC 42001 and the OWASP Top 10 for LLM Applications. Each row names the
enforcement point in code and the test that produces evidence for it, and states what the control does not
cover. Status values:

- **Implemented**: enforced in the gateway and covered by a test.
- **Partial**: supports the requirement but needs a deployer control or is limited (stated).
- **Deployer**: outside the gateway by design (host, network, process); guidance in the docs.
- **Roadmap**: not implemented.

This is an engineering mapping, not a compliance attestation or legal advice. Deploying the platform does not
make an institution HIPAA- or SOC 2-compliant; it provides technical safeguards and evidence that a compliance
program can use. Operating procedures (WORM retention, air-gap install, monthly review) are in
[compliance.md](compliance.md); threats and residual risks in [threat-model.md](threat-model.md).

## HIPAA Security Rule: technical safeguards (45 CFR 164.312)

| Standard | Platform control | Status | Code | Test |
|---|---|---|---|---|
| (a)(1) Access control; (a)(2)(i) unique user identification | One API key per application; OIDC access tokens identify individual users (`user:<username>`), with roles from IdP groups; document Q&A limited to documents the caller's groups may read (`gateway/rag.py::access_for`); every request authenticated; usage counted per key and per token user | Implemented when OIDC is enabled (API keys alone identify applications only) | `gateway/identity.py`, `gateway/auth.py`, `gateway/store.py::record_principal_usage` | `test_gateway.py::test_requires_bearer_key`; `test_identity.py::test_rs256_and_es256_tokens_map_groups_to_roles`, `::test_token_users_are_metered_without_user_names_in_metrics` |
| (a)(2)(ii) Emergency access procedure | Bootstrap admin key can be preset (`GATEWAY_ADMIN_BOOTSTRAP_KEY`); last admin key can't be revoked | Partial: no break-glass workflow | `gateway/store.py::ensure_bootstrap_admin`, `gateway/main.py::admin_revoke_key` | `test_gateway.py::test_bootstrap_admin_created_once`, `::test_cannot_revoke_last_admin` |
| (a)(2)(iii) Automatic logoff | Admin page keeps the admin key in `sessionStorage` (cleared when the tab closes) | Partial: no idle timeout; API keys don't expire (roadmap) | `admin-ui/index.html` | none |
| (a)(2)(iv) Encryption and decryption | Envelope encryption (AES-256-GCM) of passages and vectors, optional sealing of audit free-text fields, keyring with rotation and retirement | Implemented, opt-in (`GATEWAY_ENCRYPT_AT_REST`); API keys and document titles not encrypted; KMS provider is roadmap; Deployer: full-disk encryption too | `gateway/crypto.py`, `scripts/keys.py` | `test_encryption.py::test_passages_and_vectors_are_sealed_on_disk_and_readable_through_the_api`, `::test_encrypting_existing_documents_and_rotating_the_key` |
| (b) Audit controls | Hash-chained audit log of admin actions, document changes and every document question; optional prompt/response logging with redaction | Implemented | `gateway/audit.py`, `gateway/logging_setup.py`, `gateway/main.py` | `test_gateway.py::test_audit_log_records_admin_actions_and_redacted_prompts`; `test_documents.py::test_questions_are_audited_with_sources_and_redaction` |
| (c)(1) Integrity; (c)(2) mechanism to authenticate ePHI | Audit-log tampering detected by `verify()`; document changes restricted to admins and audited | Partial: protects the audit trail, not the documents or database contents; newest entries need a WORM anchor (ADR 0003) | `gateway/audit.py::verify` | `test_gateway.py::test_audit_verify_detects_tampering`; `test_demo_engine.py::test_tampering_is_detected_and_the_tail_limitation_is_reported` |
| (d) Person or entity authentication | Bearer API keys (256-bit random) for applications; OIDC JWTs for people, verified against the IdP's JWKS (RS256/ES256 allow-list, `iss`/`aud`/`exp`/`sub` required, clock skew bounded) | Partial: MFA is delegated to the IdP; API keys stored in plaintext in SQLite (hashing at rest is roadmap) | `gateway/identity.py::verify_token`, `gateway/auth.py` | `test_gateway.py::test_revoked_key_is_rejected`; `test_identity.py::test_forged_and_tampered_tokens_are_rejected`, `::test_algorithm_allow_list_blocks_none_hmac_confusion_and_other_algs` |
| (e)(1) Transmission security; (e)(2) integrity and encryption | Localhost bind by default; inference server on localhost by default; no outbound calls | Deployer: TLS at a reverse proxy (`docs/enterprise-notes.md`); `https://` upstream URL for a remote inference server | `gateway/config.py` (`GATEWAY_HOST=127.0.0.1`) | `test_deploy_config.py::test_every_published_port_is_bound_to_localhost` |

## SOC 2 (2017 Trust Services Criteria): CC6 and CC7

| Criterion | Platform control | Status | Code | Test |
|---|---|---|---|---|
| CC6.1 Logical access security | Authenticated API (API keys or OIDC tokens); admin-only routes for keys, documents and model pulls | Implemented | `gateway/auth.py`, `gateway/identity.py`, `gateway/main.py` | `test_gateway.py::test_non_admin_cannot_manage_keys`; `test_documents.py::test_only_admins_add_or_remove_documents`; `test_identity.py::test_admin_api_respects_roles_and_audits_the_user` |
| CC6.2 Registration and authorization before issuing credentials | Only admins create keys; each creation audited with the creator | Implemented | `gateway/main.py::admin_create_key` | `test_gateway.py::test_audit_log_records_admin_actions_and_redacted_prompts` |
| CC6.3 Role-based access; modifying and removing access | Roles admin, user and reader:&lt;collection&gt;, mapped from IdP groups; collection and document access lists enforced before retrieval (`test_acl.py::test_no_cross_group_leakage_in_answers_citations_flags_counts_or_prompts`); key revocation audited; last-admin guard; admin page shows only what the role allows | Implemented (role changes take effect on the user's next token; no SCIM) | `gateway/identity.py::roles_for_groups`, `gateway/auth.py::require_user`, `gateway/main.py::admin_revoke_key` | `test_identity.py::test_reader_role_is_limited_to_its_collection`, `::test_admin_api_respects_roles_and_audits_the_user`; `test_gateway.py::test_cannot_revoke_last_admin` |
| CC6.6 Protection against threats from outside the boundary | Localhost bind; per-caller rate limits; upload size limit; input validation; on Kubernetes a default-deny NetworkPolicy (ingress from configured peers; egress only to DNS, the backend and the IdP) and a hardened pod (non-root, read-only root filesystem, no capabilities) | Partial: perimeter and TLS are Deployer; the chart was validated by rendering, not on a cluster | `gateway/auth.py::_rate_limit`, `gateway/main.py::upload_document`, `deploy/helm/local-llm-gateway/` | `test_gateway.py::test_rate_limit`; `test_documents.py::test_input_validation`; `test_helm_chart.py::test_default_render_is_a_hardened_single_replica_gateway`, `::test_airgapped_values_go_offline_mount_the_keyring_and_configure_oidc` |
| CC6.7 Restricting transmission and movement of information | No outbound calls besides the configured inference server; prompts redacted before logging; caller keys never sent upstream; no prompt text in metrics or traces | Implemented | `gateway/backends.py`, `gateway/redact.py`, `gateway/telemetry.py` | `test_backends.py::test_openai_compatible_chat_keeps_gateway_contract`; `test_telemetry.py::test_chat_emits_genai_span` |
| CC6.8 Preventing unauthorized or malicious software | Model pulls admin-only and audited; pinned model digests verified against what is served, with an `enforce` policy (403); CycloneDX ML-BOM | Implemented for integrity against pins; Partial for provenance (no signature verification) | `gateway/supply_chain.py`, `gateway/main.py::pull_model`, `scripts/mlbom.py` | `test_supply_chain.py::test_enforce_serves_only_pinned_models_whose_digest_matches`, `::test_ml_bom_is_valid_cyclonedx_1_6_with_models_and_dependencies` |
| CC7.1 Detecting configuration changes and vulnerabilities | Settings validated at startup (unknown backend rejected); CI runs lint, format check, tests and shellcheck | Partial: no config-drift detection or dependency scanning | `gateway/config.py`, `.github/workflows/ci.yml` | `test_backends.py::test_unknown_backend_is_rejected_at_startup`; `test_deploy_config.py` |
| CC7.2 Monitoring for anomalies | Prometheus metrics (requests by route/status/key label, latency, TTFT, tokens, backend errors), Grafana dashboard, optional OTel GenAI spans | Implemented (metrics); alert rules are Deployer | `gateway/metrics.py`, `deploy/grafana-dashboard.json`, `gateway/telemetry.py` | `test_metrics.py`, `test_telemetry.py` |
| CC7.3 Evaluating security events | 401/403/429 visible per key label; audit-chain verification; injection flags on retrieved passages | Partial: no SIEM integration or automated triage | `gateway/metrics.py`, `gateway/audit.py`, `gateway/rag.py::injection_signals` | `test_metrics.py::test_rejections_show_up_by_status_with_key_name`; `test_injection_flags.py` |
| CC7.4 Responding to incidents | Immediate key revocation; audit trail shows what a key did and which documents answers used | Partial: no incident runbook shipped | `gateway/main.py::admin_revoke_key` | `test_gateway.py::test_revoked_key_is_rejected` |
| CC7.5 Recovering from incidents | Online backup with checksummed manifest; verified restore (checksums, integrity, audit chain, keys); RTO/RPO guidance and drill procedure in `docs/operations.md` | Implemented (procedure and tooling); scheduling and off-host copies are Deployer | `scripts/backup.py`, `scripts/restore.py` | `test_backup_restore.py::test_backup_restore_drill_brings_back_documents_keys_acls_and_the_audit_chain`, `::test_restore_refuses_altered_or_incomplete_backups_and_leaves_live_files_alone` |

## NIST AI RMF 1.0 and the Generative AI Profile (NIST AI 600-1)

| Function / GAI risk | Platform control | Status | Code | Test |
|---|---|---|---|---|
| GOVERN: documented decisions and accountability | ADRs, threat model, this mapping; every admin action attributed to a key label | Partial: organizational policies are Deployer | `docs/adr/`, `gateway/audit.py` | `test_gateway.py::test_audit_log_records_admin_actions_and_redacted_prompts` |
| MAP: context and risks | Threat model with STRIDE and OWASP LLM Top 10; sector profiles | Implemented (documentation) | `docs/threat-model.md`, `profiles/` | `test_deploy_config.py::test_env_example_and_profiles_only_use_known_settings` |
| MEASURE: performance and monitoring | Metrics, dashboard, load-test tool, measured gateway overhead; RAG eval gate in CI (recall@k, MRR, citation accuracy, answer-contains, ACL leaks) | Partial: retrieval evaluated with a stand-in embedder; model answer faithfulness not evaluated | `gateway/metrics.py`, `scripts/loadtest.py`, `scripts/rag_eval.py`, `docs/benchmarks.md` | `test_metrics.py`, `test_loadtest.py`, `test_retrieval.py::test_rag_eval_meets_thresholds_and_has_no_acl_leaks` |
| MANAGE: respond and contain | Key revocation, rate limits, document removal, audit verification | Implemented | `gateway/main.py` | `test_gateway.py::test_revoked_key_is_rejected`, `::test_rate_limit` |
| 600-1 Data privacy | Local inference; prompt logging off by default; redaction | Partial: pattern-based redaction only | `gateway/redact.py`, `gateway/logging_setup.py` | `test_redact.py` |
| 600-1 Information security | Authentication, rate limits, injection guard (ADR 0004), hash-chained audit | Partial (see threat model residual risks) | `gateway/auth.py`, `gateway/rag.py` | `test_injection_flags.py`, `test_gateway.py` |
| 600-1 Confabulation | Answers limited to sources, numbered citations, "I don't know" instruction, `cited` flag per source | Partial: citation faithfulness not evaluated | `gateway/rag.py::SYSTEM_PROMPT`, `::cited_numbers` | `test_documents.py::test_ask_retrieves_the_right_document_and_cites_it` |
| 600-1 Information integrity (provenance) | Each answer's retrieved and cited passages recorded in the audit log | Implemented | `gateway/main.py::ask` | `test_documents.py::test_questions_are_audited_with_sources_and_redaction` |
| 600-1 Value chain and component integration | Pluggable, instrumented backends; audited model pulls; model lock file, digest verification and policy; ML-BOM with model license, source and verification status | Partial: no model signature verification | `gateway/backends.py`, `gateway/supply_chain.py`, `scripts/mlbom.py` | `test_backends.py`; `test_supply_chain.py` |

## ISO/IEC 42001:2023 (selected Annex A controls)

The platform supports a few technical controls; an AI management system (clauses 4–10, policies, roles,
impact assessments) is organizational and out of scope. Control numbers should be checked against a licensed
copy of the standard.

| Control | Platform control | Status | Code | Test |
|---|---|---|---|---|
| A.6.2.6 AI system operation and monitoring | Prometheus metrics, Grafana dashboard, health endpoint with backend status | Implemented | `gateway/metrics.py`, `gateway/main.py::health` | `test_metrics.py`; `test_backends.py::test_default_backend_is_ollama` |
| A.6.2.7 AI system technical documentation | ADRs, threat model, controls mapping, benchmarks method | Implemented (documentation) | `docs/` | n/a |
| A.6.2.8 AI system recording of event logs | Hash-chained audit log | Implemented | `gateway/audit.py` | `test_gateway.py::test_audit_verify_detects_tampering` |
| A.7.5 Data provenance | Document additions/removals and the passages behind each answer are logged | Implemented | `gateway/main.py::_store_document`, `::ask` | `test_documents.py::test_questions_are_audited_with_sources_and_redaction` |
| A.6.2.4 AI system verification and validation | Automated tests in CI, plus a RAG eval gate over a golden set | Partial: retrieval is evaluated; a real model's output quality is not | `.github/workflows/ci.yml`, `scripts/rag_eval.py` | full suite; `test_retrieval.py::test_rag_eval_meets_thresholds_and_has_no_acl_leaks` |

## OWASP Top 10 for LLM Applications (2025)

Mapped risk by risk, with tests and residual risk, in the [threat model](threat-model.md#owasp-top-10-for-llm-applications-2025).
LLM08 (vector and embedding weaknesses) is addressed by permission-aware retrieval: access is decided per
caller before passages are loaded (`gateway/rag.py::access_for`, `gateway/rag.py::candidates`), with
leakage tests over answers, citations, injection flags, counts and prompts
(`test_acl.py::test_no_cross_group_leakage_in_answers_citations_flags_counts_or_prompts`).

## Known gaps (roadmap)

| Gap | Affects |
|---|---|
| SCIM provisioning; login flow (auth code + PKCE) in the admin page; token revocation/introspection | HIPAA (a)(2)(i), (d); SOC 2 CC6.2, CC6.3 |
| ACL sync from source systems (SharePoint, Drive, file shares); per-chunk ACLs | SOC 2 CC6.3; OWASP LLM08 |
| KMS key provider; hashing API keys at rest; encrypting titles and ACLs | HIPAA (a)(2)(iv); threat model "data at rest" |
| Model signature verification (provenance); hash-locked Python dependencies | SOC 2 CC6.8; OWASP LLM03; NIST 600-1 value chain |
| Signed audit checkpoints / automatic WORM anchoring | HIPAA (c); ADR 0003 tail limitation |
| Token budgets and `max_tokens` ceilings per key; shared rate-limit store | OWASP LLM10 |
| LLM-judged answer-faithfulness evals against the deployment's model | NIST MEASURE; OWASP LLM09 |
| Scheduled off-host backups and point-in-time recovery | SOC 2 CC7.5 |
