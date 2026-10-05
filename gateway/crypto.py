# Corey Mathie, 2026
"""
Envelope encryption at rest (AES-256-GCM via `cryptography`).

- Each document gets its own random 256-bit data key (DEK). Its passages' text and embedding vectors
  are sealed with the DEK; the associated data binds every ciphertext to its document, passage number
  and field, so ciphertexts can't be swapped between rows undetected.
- The DEK is stored only wrapped (encrypted) by a key-encryption key (KEK) from a key provider, with the
  KEK's id. Rotating the KEK re-wraps DEKs; the passages themselves are not re-encrypted.
- Deleting a document deletes its wrapped DEK with its passages (crypto-shredding of that row's keys;
  copies in older backups remain decryptable while the KEK is kept).
- Optionally (GATEWAY_AUDIT_ENCRYPT_TEXT) the free-text fields of audit entries (prompt, response,
  question, answer) are sealed the same way, per entry. The hash chain covers the ciphertext, so the
  chain verifies without any key.

Key providers implement `wrap(dek) -> (kek_id, wrapped)` and `unwrap(kek_id, wrapped) -> dek`.
`LocalKeyring` (shipped) reads KEKs from a JSON keyring file or one base64 key in an environment
variable. A KMS provider (AWS KMS Encrypt/Decrypt, Azure Key Vault wrapKey/unwrapKey, GCP KMS, HashiCorp
Vault Transit) implements the same two calls so the KEK never leaves the KMS; see docs/operations.md.
No KMS provider is implemented here.

Not encrypted by this module: document titles, sizes, access lists and uploader labels (needed for
listing and access decisions without keys), API keys and usage counters, and audit entries' metadata.

`cryptography` is imported lazily, so the browser demo (encryption off) runs without it.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Protocol

from .config import settings

KEY_BYTES = 32
NONCE_BYTES = 12
KEY_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SENSITIVE_AUDIT_FIELDS = ("prompt", "response", "question", "answer")


class CryptoError(Exception):
    """A key is missing or a ciphertext failed authentication."""


class KeyProvider(Protocol):
    active_id: str

    def wrap(self, dek: bytes) -> tuple[str, bytes]: ...

    def unwrap(self, kek_id: str, wrapped: bytes) -> bytes: ...


def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key)


def seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + _aesgcm(key).encrypt(nonce, plaintext, aad)


def unseal(key: bytes, blob: bytes, aad: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag

    try:
        return _aesgcm(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad)
    except (InvalidTag, ValueError) as e:
        raise CryptoError("ciphertext failed authentication (wrong key, or the data was altered)") from e


def new_key() -> bytes:
    return os.urandom(KEY_BYTES)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _decode_key(value: str, name: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except ValueError as e:
        raise ValueError(f"{name} is not valid base64") from e
    if len(key) != KEY_BYTES:
        raise ValueError(f"{name} must decode to {KEY_BYTES} bytes (AES-256), got {len(key)}")
    return key


class LocalKeyring:
    """KEKs held by the gateway process. Keyring file format:

        {"active": "2026-10", "keys": {"2026-04": "<base64 32 bytes>", "2026-10": "<base64 32 bytes>"}}

    Keep retired keys in the file for as long as backups or audit entries sealed with them must stay
    readable.
    """

    def __init__(self, keys: dict[str, bytes], active: str):
        if active not in keys:
            raise ValueError(f"active key {active!r} is not in the keyring")
        for kid in keys:
            if not KEY_ID_RE.match(kid):
                raise ValueError(f"invalid key id {kid!r}")
        self.keys = keys
        self.active_id = active

    @classmethod
    def from_file(cls, path: str | Path) -> LocalKeyring:
        doc = json.loads(Path(path).read_text())
        keys = {kid: _decode_key(v, f"key {kid!r}") for kid, v in doc.get("keys", {}).items()}
        return cls(keys, doc.get("active", ""))

    @classmethod
    def from_env(cls, value: str) -> LocalKeyring:
        return cls({"env": _decode_key(value, "GATEWAY_ENCRYPTION_KEY")}, "env")

    def _aad(self, kek_id: str) -> bytes:
        return b"lldk-dek|" + kek_id.encode()

    def wrap(self, dek: bytes) -> tuple[str, bytes]:
        return self.active_id, seal(self.keys[self.active_id], dek, self._aad(self.active_id))

    def unwrap(self, kek_id: str, wrapped: bytes) -> bytes:
        if kek_id not in self.keys:
            raise CryptoError(f"key {kek_id!r} is not in the keyring (was it retired too early?)")
        return unseal(self.keys[kek_id], wrapped, self._aad(kek_id))


def write_keyring(path: str | Path, keys: dict[str, bytes], active: str) -> None:
    """Write a keyring file readable only by its owner."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"active": active, "keys": {k: b64(v) for k, v in keys.items()}}, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)


_provider: KeyProvider | None = None
_provider_source: tuple | None = None


def provider() -> KeyProvider:
    """The configured key provider (cached; reloaded when the settings change)."""
    global _provider, _provider_source
    source = (settings.GATEWAY_ENCRYPTION_KEY_FILE, settings.GATEWAY_ENCRYPTION_KEY)
    if _provider is None or _provider_source != source:
        if settings.GATEWAY_ENCRYPTION_KEY_FILE:
            _provider = LocalKeyring.from_file(settings.GATEWAY_ENCRYPTION_KEY_FILE)
        elif settings.GATEWAY_ENCRYPTION_KEY:
            _provider = LocalKeyring.from_env(settings.GATEWAY_ENCRYPTION_KEY)
        else:
            raise CryptoError("no encryption key configured (GATEWAY_ENCRYPTION_KEY_FILE or GATEWAY_ENCRYPTION_KEY)")
        _provider_source = source
    return _provider


def set_provider(p: KeyProvider | None) -> None:
    """Plug in another provider (a KMS client, or tests)."""
    global _provider, _provider_source
    _provider = p
    _provider_source = (settings.GATEWAY_ENCRYPTION_KEY_FILE, settings.GATEWAY_ENCRYPTION_KEY) if p else None


def enabled() -> bool:
    return settings.GATEWAY_ENCRYPT_AT_REST


def validate_settings() -> None:
    if settings.GATEWAY_ENCRYPT_AT_REST or settings.GATEWAY_AUDIT_ENCRYPT_TEXT:
        try:
            provider()
        except (CryptoError, OSError, ValueError) as e:
            raise ValueError(f"encryption is enabled but the key can't be loaded: {e}") from e


# ---------- Document passages ----------


def passage_aad(doc_id: str, idx: int, part: str) -> bytes:
    return f"lldk-chunk|{doc_id}|{idx}|{part}".encode()


# ---------- Audit fields ----------


def seal_field(value: str) -> dict:
    dek = new_key()
    kek_id, wrapped = provider().wrap(dek)
    return {
        "enc": "aes-256-gcm",
        "kek": kek_id,
        "dek": b64(wrapped),
        "ct": b64(seal(dek, value.encode(), b"lldk-audit")),
    }


def open_field(obj: dict) -> str:
    dek = provider().unwrap(obj["kek"], base64.b64decode(obj["dek"]))
    return unseal(dek, base64.b64decode(obj["ct"]), b"lldk-audit").decode()


def is_sealed(value) -> bool:
    return isinstance(value, dict) and value.get("enc") == "aes-256-gcm" and {"kek", "dek", "ct"} <= set(value)


def protect_payload(payload: dict) -> dict:
    """Seal the free-text fields of an audit payload when GATEWAY_AUDIT_ENCRYPT_TEXT is on."""
    if not settings.GATEWAY_AUDIT_ENCRYPT_TEXT:
        return payload
    return {k: seal_field(v) if k in SENSITIVE_AUDIT_FIELDS and isinstance(v, str) else v for k, v in payload.items()}


def reveal_payload(payload: dict) -> dict:
    """Open sealed fields for an admin view; fields whose key is unavailable show as "[encrypted]"."""
    out = {}
    for k, v in payload.items():
        if is_sealed(v):
            try:
                v = open_field(v)
            except (CryptoError, OSError, ValueError, KeyError):
                v = "[encrypted]"
        out[k] = v
    return out
