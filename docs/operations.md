# Operations: encryption at rest, keys, backup and restore

How to run the gateway's data safely: what state exists, how it is encrypted, how keys rotate, how to
back it up and restore it, and how to set recovery objectives. Every procedure here maps to a script
and a test; gaps are listed at the end. Related: [threat model](threat-model.md),
[controls](controls.md), [compliance notes](compliance.md).

## State

| What | Where | Holds | Encrypted by the gateway |
|---|---|---|---|
| Database | `GATEWAY_DB_PATH` (SQLite) | API keys and usage, token-user usage, documents, passages, vectors, access lists | Passages and vectors when `GATEWAY_ENCRYPT_AT_REST=true`. Not: titles, sizes, access lists, uploader labels, API keys (plaintext) |
| Audit log | `$GATEWAY_LOG_DIR/audit.jsonl` | Hash-chained events | Free-text fields (prompt, response, question, answer) when `GATEWAY_AUDIT_ENCRYPT_TEXT=true`; metadata stays readable |
| Keyring | `GATEWAY_ENCRYPTION_KEY_FILE` (or `GATEWAY_ENCRYPTION_KEY`) | Key-encryption keys (KEKs) | It *is* the key material: protect it with OS permissions (written as mode 0600), a secrets manager, or a KMS |
| Configuration | `.env` / environment | Settings, possibly the metrics token and upstream API key | No |

Use full-disk or volume encryption as well: it covers what the gateway does not (titles, API keys,
temporary files, the OS).

## Encryption at rest

Envelope encryption with AES-256-GCM (`gateway/crypto.py`, using `cryptography`):

- Each document gets a random 256-bit data key (DEK). Its passages' text and vectors are sealed with the
  DEK; the associated data names the document, passage number and field, so swapping ciphertexts
  between rows fails authentication (`test_encryption.py::test_ciphertexts_are_bound_to_their_row_and_key`).
- The DEK is stored wrapped by the active KEK, with the KEK's id. Decryption happens after the access
  filter, so a hidden document's DEK is never unwrapped for a caller who can't read it
  (`test_encryption.py::test_hidden_documents_keys_are_never_unwrapped`).
- A wrong or missing key, or altered ciphertext, makes `/ask` fail closed with a 500 that names the
  keyring as the problem; nothing is returned in plaintext by fallback.
- Audit free-text fields are sealed per entry before hashing, so `GET /admin/audit/verify` works
  without keys; admins see opened fields in `/admin/audit/recent` while the key is available
  (`test_encryption.py::test_audit_text_fields_are_sealed_and_the_chain_verifies_without_keys`).

Measured cost on the build host with `python scripts/bench_storage.py --docs 500` (2 vCPUs, shared,
Intel Xeon 2.80 GHz; synthetic collection of 500 documents, 3,883 passages, 16.1 MB database; demo
embedder; medians; two runs, which varied by up to about 30%): loading and decrypting every passage for
one question took 61.5–83.5 ms versus 21.8–33.5 ms in plaintext, so decryption added about 40–50 ms; a
full hybrid retrieval took 199–248 ms encrypted versus 182–240 ms plaintext. At that size most of the
retrieval time is BM25 tokenization in Python, not decryption. Re-measure on your hardware and data.

### Turning it on

```bash
python scripts/keys.py generate --keyring /etc/local-llm-deploy-kit/keyring.json --activate
# .env: GATEWAY_ENCRYPT_AT_REST=true, GATEWAY_ENCRYPTION_KEY_FILE=/etc/local-llm-deploy-kit/keyring.json
python scripts/keys.py encrypt-existing   # documents stored before encryption was enabled; then VACUUM runs
python scripts/keys.py status
```

`encrypt-existing` rewrites the database file (`VACUUM`) so freed pages don't keep the old plaintext,
but **backups, snapshots and replicas taken before encryption still contain plaintext**: replace them
on your retention schedule. The gateway refuses to start if encryption is on and the key can't be
loaded (`test_encryption.py::test_misconfigured_encryption_fails_at_startup`).

### Key rotation

1. `python scripts/keys.py generate --keyring PATH --activate` (new documents use the new KEK after the
   gateway restarts).
2. `python scripts/keys.py rewrap`: every DEK is unwrapped with its old KEK and wrapped with the active
   one. Passages are not re-encrypted, so rotation is quick and doesn't touch the bulk of the data.
   Audited as `encryption_keys_rewrapped`.
3. `python scripts/keys.py status`: confirm no document uses the old key.
4. `python scripts/keys.py retire --keyring PATH --id OLD`: refuses while the key is active, still wraps a
   document, or sealed audit fields reference it (those can't be re-sealed without breaking the hash
   chain; archive the old key with the audit archive, or pass `--force`).

Keep retired keys wherever old backups are kept: a backup is readable only with the KEKs listed in its
manifest. Tested end to end in `test_encryption.py::test_encrypting_existing_documents_and_rotating_the_key`.

Rotating the DEKs themselves (re-encrypting passages) is not implemented; re-ingesting a document gives
it a new DEK.

### KMS

Key providers implement two calls, `wrap(dek) -> (kek_id, wrapped)` and `unwrap(kek_id, wrapped) -> dek`
(`gateway/crypto.py::KeyProvider`), and are installed with `gateway/crypto.py::set_provider`. A KMS
provider maps them to AWS KMS `Encrypt`/`Decrypt`, Azure Key Vault `wrapKey`/`unwrapKey`, Google Cloud
KMS `encrypt`/`decrypt`, or HashiCorp Vault Transit `encrypt`/`decrypt`, so the KEK never leaves the
KMS and every unwrap is logged there. **No KMS provider ships in this repository**, and none has been
tested; the local keyring is the only implemented provider. With a KMS, one unwrap per document per
question becomes one KMS call: cache DEKs in memory for a short time or the KMS becomes the bottleneck.

### Deleting data

Deleting a document deletes its passages, vectors and wrapped DEK. Older backups still contain them and
remain readable while their KEK exists; destroying a KEK makes every backup that needs it unreadable
(crypto-shredding at backup granularity, not per document).

## Backup

```bash
python scripts/backup.py --out /backups        # -> /backups/lldk-backup-<UTC timestamp>/
```

- The database is copied with SQLite's online backup API: consistent while the gateway runs.
- The audit log is copied and the copy's chain verified; the manifest records entries and last hash.
- `manifest.json` lists SHA-256 and size per file, the SQLite integrity check, and the KEK ids the backup
  needs. **Keys are never included**; store the keyring on a different system with different access.
- API keys are stored in plaintext in the database; protect backups like the live database.

Ship backups off the host (object storage with versioning or object lock). The audit log's WORM
archive (see [compliance.md](compliance.md)) is a separate, append-only copy; keep doing both.

## Restore

Stop the gateway, then:

```bash
python scripts/restore.py /backups/lldk-backup-... --check-only   # verify only
python scripts/restore.py /backups/lldk-backup-...                # refuses to overwrite live files
python scripts/restore.py /backups/lldk-backup-... --force        # live files are moved aside, not deleted
```

Before touching anything, restore checks that every file matches its checksum, the SQLite integrity
check passes, the audit chain verifies (an attacker who edits the log *and* the manifest is still
caught), and the configured keyring holds every KEK the backup needs. After copying, it checks
integrity and the chain again. Tested as a full drill (backup while running, delete everything,
restore, start a new gateway, ask a question through an ACL, keep appending to the audit chain) in
`test_backup_restore.py::test_backup_restore_drill_brings_back_documents_keys_acls_and_the_audit_chain`,
plus refusal cases in `test_backup_restore.py::test_restore_refuses_altered_or_incomplete_backups_and_leaves_live_files_alone`
and `test_backup_restore.py::test_restore_needs_the_keys_and_does_not_overwrite_without_force`.

Measured on the build host for the 16.1 MB synthetic database above (`scripts/bench_storage.py`, two
runs): `backup` took 111–117 ms and `restore` 114–119 ms. That is only the copy-and-verify step, not a
recovery time.

## Recovery objectives (RTO / RPO)

This repository doesn't promise recovery times; they depend on your hosts, storage and people. How to
set and measure them:

- **RPO (data you can lose)** is at most the interval between successful, off-host backups, plus the
  delay before audit archives reach WORM storage. Hourly backups mean up to an hour of new documents,
  keys and audit entries. For less, use storage-level replication or snapshots (deployer choice; not
  provided or tested here).
- **RTO (time to serve again)** = provision or repair the host + install the kit (or start the container)
  + fetch the backup and the keyring + `restore.py` + start the gateway + smoke test. Measure it with a
  restore drill on the schedule your program requires (quarterly is common) and record the result as
  audit evidence. The restore step itself is small next to provisioning and model downloads: pre-stage
  model files (see the air-gap bundle) if the model server must be rebuilt too.
- The model server is stateless apart from model files; its recovery is reinstalling it and the pinned
  models.

## Failure modes

| Situation | Behavior |
|---|---|
| Encryption on, key missing or invalid at startup | Gateway refuses to start with a message naming the setting |
| Key removed while running, wrong key, or ciphertext altered | `/ask` on that collection returns 500 "could not be decrypted"; logged; nothing returned in plaintext |
| Keyring lost | Encrypted passages and sealed audit fields are unrecoverable. Back up the keyring separately |
| Backup altered or incomplete | `restore.py` refuses before changing anything |
| Restore target exists | Refused without `--force`; with it, existing files are renamed `*.pre-restore-<timestamp>` |

## Not implemented

KMS providers; per-document DEK rotation without re-ingest; encryption of titles, access lists and API
keys (API keys are not hashed at rest); automatic scheduled backups (use cron or your scheduler);
point-in-time recovery.
