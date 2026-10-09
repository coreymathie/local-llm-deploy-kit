# ADR 0008: Pin model digests, verify what is served, and publish an ML-BOM

## Status

Accepted, v0.6.0.

## Context

A model is code-like supply: a re-pulled tag, a swapped weight file or a typo-squatted name changes behavior
without any code change (OWASP LLM03, NIST AI 600-1 value chain, SOC 2 CC6.8). Through v0.5 model pulls were
admin-only and audited, but nothing checked *which* bytes were being served.

## Decision

1. **Lock file** (`GATEWAY_MODEL_LOCK_FILE`, `models.lock.example.json`): model name, backend, SHA-256 digest
   (Ollama's manifest digest) or pinned weight files with their SHA-256, plus source, license and purpose. A
   malformed lock file stops startup.
2. **Verification** (`gateway/supply_chain.py::verify`): Ollama's `/api/tags` digest must equal the pin; for
   OpenAI-compatible servers (which report names only) the pinned weight files are hashed from a path the
   gateway can read. Statuses: verified, mismatch, missing, unpinned, unverifiable, error. Runs at startup
   (bounded to 10 s), after every model pull, on `POST /admin/models/verify`, and lazily for Ollama when the
   last result is older than `GATEWAY_MODEL_VERIFY_INTERVAL_SECONDS`. Every run is audited
   (`model_verification`).
3. **Policy** (`GATEWAY_MODEL_POLICY`): `off`; `warn` (default: serve, log once per run, count in
   `gateway_model_policy_decisions_total`, record the status on document answers); `enforce` (serve only pinned
   models whose last verification is `verified`, 403 otherwise, including when the backend cannot be checked).
   Applied to chat, embeddings, document questions and ingestion. The healthcare and finance profiles set
   `enforce`.
4. **ML-BOM** (`scripts/mlbom.py`): CycloneDX 1.6 JSON with `machine-learning-model` components (hashes, nested
   weight-file components, license, source, task, verification status) and the Python packages with purls,
   validated against the CycloneDX 1.6 schema in tests.
5. **Pinning is trust on first use** (`scripts/pin_models.py`), stated as such: digests must be compared with the
   publisher's before they are relied on.

**Implementation and evidence.** `gateway/supply_chain.py`, `gateway/backends.py::OllamaBackend`
(`model_inventory`), `gateway/main.py` (`_model_allowed`, `/admin/models/*`), `scripts/pin_models.py`,
`scripts/mlbom.py`. Tests: `tests/test_supply_chain.py`. Operating procedure:
[operations.md](../operations.md#model-supply-chain).

## Consequences

**Positive**

- A swapped or re-pulled model is refused (enforce) or flagged (warn) within one verification interval;
  auditors receive a BOM listing exactly which models and packages are in use.

**Negative**

- Not provenance: a digest proves the bytes match the pin, not who produced them. Signature verification
  (Sigstore model signing, OCI signatures) is **roadmap**.
- Time-of-check/time-of-use: between verifications the backend could serve something else; Ollama is re-checked
  every interval, weight files only at startup, after pulls and on demand.
- Ollama's digest covers the manifest (which names the weight layer digests), not a hash the gateway computes
  over the weights itself.

## Alternatives considered

- **Trust the registry tag.** No pin at all, which is the v0.5 state this decision replaces: a re-pulled tag
  changes behavior silently.
