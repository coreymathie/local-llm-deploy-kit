# ADR 0003: Hash-chained audit log on the host; WORM storage for retention

## Status

Accepted, v0.3.0.

## Context

Regulated institutions need evidence of who did what: which keys exist, who created or revoked them, which
models were pulled, which documents were added, and what each answer relied on. An auditor or examiner will ask
whether that evidence could have been edited after the fact.

## Decision

- Write events to `audit.jsonl`. Each entry carries `prev_hash` (the previous entry's `entry_hash`) and its own
  `entry_hash = SHA-256(canonical JSON of ts, event, payload, prev_hash)`. The first entry chains from 64 zeros.
- `verify()` walks the file and returns the first line whose hash or link does not match.
- Admin actions and document questions are always logged; prompt/completion text only with
  `GATEWAY_LOG_PROMPTS=true`, and PII-redacted with `GATEWAY_REDACT_PROMPTS=true`. Full API keys never enter the
  log (masked to 12 characters).
- Retention and immutability are delegated to WORM storage (S3 Object Lock, immutable Blob, GCS Bucket Lock,
  WORM NAS) by an operator procedure in `docs/compliance.md`.

**Implementation and evidence.** Code: `gateway/audit.py`, callers in `gateway/main.py` and
`gateway/logging_setup.py`. Tests: `tests/test_gateway.py::test_audit_verify_detects_tampering`,
`::test_audit_log_records_admin_actions_and_redacted_prompts`;
`tests/test_demo_engine.py::test_tampering_is_detected_and_the_tail_limitation_is_reported` (documents the tail
limit).

## Consequences

**Positive**

- Any edit, insertion or deletion **before the newest entry** is detected, and `verify()` points at the line.
  No database, no external service.

**Negative**

- **The tail is not protected by the chain alone.** Someone with write access can rewrite or delete the most
  recent entries and recompute their hashes, and `verify()` will pass. The browser demo shows this case on
  purpose. Detection needs an anchor outside the attacker's reach: the archived copy in WORM storage, or the
  last hash recorded elsewhere. Automatic anchoring (signed checkpoints or shipping to an external log) is
  **not implemented** (roadmap).
- The hash chain proves integrity, not authenticity; entries are not signed with a key.
- Rotation is manual. The 0.3 docstring describes recording the archived file's last hash as the next file's
  anchor; the code does not do this automatically yet.
- `append()` serializes writers with a process-local lock and re-reads the file to find the tail. One gateway
  process only; multiple processes would need a shared writer.

## Alternatives considered

- **Write straight to WORM/object lock.** Strongest immutability, but cloud-dependent and awkward on an
  air-gapped laptop. Kept as the retention tier instead.
- **Database table with triggers.** Anyone with database admin rights can still edit it, and it is no easier to
  verify independently than a file.
- **Transparency log / Merkle tree with signed tree heads.** The right next step for tail protection; more
  machinery than v0.3 needed.
