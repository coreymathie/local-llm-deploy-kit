# ADR 0007: Envelope encryption for passages, vectors and audit text; keys outside backups

## Status

Accepted, v0.6.0.

## Context

Documents, their passages and their embedding vectors sat in plaintext in SQLite (vectors can be partially
inverted back to text). Prompts in the audit log were plaintext unless redacted. HIPAA 164.312(a)(2)(iv) and
most security reviews ask for encryption at rest under keys the operator controls and can rotate, and for
backups that are useless without those keys.

## Decision

1. **Envelope encryption** (`gateway/crypto.py`): a random AES-256-GCM data key per document seals its passages'
   text and vectors; associated data binds each ciphertext to document, passage and field. The data key is
   stored wrapped by a key-encryption key (KEK) and the KEK's id.
2. **Pluggable key provider** (`wrap`/`unwrap`). Shipped: a local keyring file (mode 0600) or one base64 key in
   the environment. KMS providers fit the same interface; none is shipped or tested.
3. **Rotation re-wraps, it does not re-encrypt** (`scripts/keys.py rewrap`); keys are retired only when nothing
   references them.
4. **Decrypt after the access filter**, so hidden documents' keys are never unwrapped (ADR 0005).
5. **Audit text**: free-text fields are sealed per entry *before* hashing, so the chain verifies without keys and
   archives can be checked by people who must not read prompts.
6. **Backups never contain keys**; the manifest lists the KEK ids needed, and restore checks them first.
7. **Fail closed**: a missing key at startup stops the gateway; a decryption failure returns a 500.

**Implementation and evidence.** `gateway/crypto.py`, `gateway/rag.py` (`_seal_row`, `_open_rows`,
`encrypt_existing`, `rewrap_keys`), `gateway/audit.py`, `scripts/keys.py`, `scripts/backup.py`,
`scripts/restore.py`. Tests: `tests/test_encryption.py`, `tests/test_backup_restore.py`. Procedures:
[operations.md](../operations.md).

## Consequences

**Positive**

- A copied database file or backup reveals no passage or vector without the keyring; per-row binding catches
  swapped ciphertexts; rotation is inexpensive.

**Negative**

- Measured cost: about 40–50 ms more per question to decrypt 3,883 passages on a 2-vCPU host
  (`scripts/bench_storage.py`, docs/operations.md).
- Not covered: titles, access lists, sizes and uploader labels stay readable (needed to list and authorize
  without keys); API keys are stored in plaintext; data in memory is plaintext; backups made before enabling
  encryption keep plaintext.
- Audit fields sealed under a KEK keep needing that KEK as long as the audit log is retained.

## Alternatives considered

- **SQLCipher (whole-file encryption).** Covers everything including titles and API keys, but needs a patched
  SQLite build in every install path (and does not run in the browser demo). Full-disk or volume encryption
  gives similar whole-file coverage with no code change; recommended alongside this decision.
- **One key for all rows.** Simpler, but rotation would mean re-encrypting everything, and a single key
  compromise exposes every row with no per-document shredding.
