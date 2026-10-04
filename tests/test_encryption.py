# Corey Mathie, 2026
"""Envelope encryption at rest: sealed passages and vectors, per-row binding, key rotation, audit fields."""

import base64
import json
import sqlite3
import stat
from pathlib import Path

import numpy as np
import pytest

from gateway import audit, crypto, rag
from gateway.config import settings
from scripts import keys as keys_cli

from .conftest import ADMIN, fake_embedding, new_key

SECRET = "The vault combination is PELICAN-4471."
KEY_A = base64.b64encode(b"A" * 32).decode()


@pytest.fixture
def encrypted(client, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_ENCRYPT_AT_REST", True)
    monkeypatch.setattr(settings, "GATEWAY_ENCRYPTION_KEY", KEY_A)
    monkeypatch.setattr(settings, "GATEWAY_ENCRYPTION_KEY_FILE", "")
    crypto.set_provider(None)
    yield client
    crypto.set_provider(None)


def _add(client, title, text, collection="vault"):
    r = client.post(f"/v1/collections/{collection}/documents/text", json={"title": title, "text": text}, headers=ADMIN)
    assert r.status_code == 201
    return r.json()["id"]


def _raw(sql, *args):
    with sqlite3.connect(settings.GATEWAY_DB_PATH) as c:
        return c.execute(sql, args).fetchall()


def _ask(client, q="What is the vault combination?", collection="vault"):
    return client.post(
        f"/v1/collections/{collection}/ask",
        json={"question": q},
        headers={"Authorization": f"Bearer {new_key(client)}"},
    )


def test_passages_and_vectors_are_sealed_on_disk_and_readable_through_the_api(encrypted):
    doc = _add(encrypted, "vault.md", "Facilities.\n\n" + SECRET)
    text, emb = _raw("SELECT text, embedding FROM chunks WHERE doc_id = ?", doc)[0]
    assert isinstance(text, bytes) and b"PELICAN" not in text
    plain_vec = np.asarray(fake_embedding("Facilities.\n\n" + SECRET), dtype=np.float32)
    plain_vec /= np.linalg.norm(plain_vec)
    assert plain_vec.tobytes() not in emb and len(emb) == len(plain_vec.tobytes()) + 12 + 16  # nonce + GCM tag
    kek_id, wrapped = _raw("SELECT kek_id, wrapped_dek FROM documents WHERE id = ?", doc)[0]
    assert kek_id == "env" and len(wrapped) == 12 + 32 + 16
    db_bytes = open(settings.GATEWAY_DB_PATH, "rb").read()
    assert b"PELICAN" not in db_bytes
    r = _ask(encrypted)
    assert r.status_code == 200 and "PELICAN-4471" in r.json()["sources"][0]["excerpt"]


def test_ciphertexts_are_bound_to_their_row_and_key(encrypted):
    a = _add(encrypted, "a.md", "Alpha passage about the vault.")
    b = _add(encrypted, "b.md", "Bravo passage about the garden.")
    with sqlite3.connect(settings.GATEWAY_DB_PATH) as c:  # an attacker swaps two sealed passages
        ta = c.execute("SELECT text FROM chunks WHERE doc_id = ?", (a,)).fetchone()[0]
        tb = c.execute("SELECT text FROM chunks WHERE doc_id = ?", (b,)).fetchone()[0]
        c.execute("UPDATE chunks SET text = ? WHERE doc_id = ?", (tb, a))
        c.execute("UPDATE chunks SET text = ? WHERE doc_id = ?", (ta, b))
    with pytest.raises(crypto.CryptoError, match="failed authentication"):
        rag.candidates("vault", rag.unrestricted_access("vault"))
    crypto.set_provider(crypto.LocalKeyring({"env": b"B" * 32}, "env"))  # wrong key, same id
    with pytest.raises(crypto.CryptoError):
        rag.candidates("vault", rag.unrestricted_access("vault"))
    r = _ask(encrypted)
    assert r.status_code == 500 and "could not be decrypted" in r.json()["detail"]  # fails closed, clearly


def test_hidden_documents_keys_are_never_unwrapped(encrypted):
    open_doc = _add(encrypted, "open.md", "Garden hours are 9 to 5.")
    hidden = _add(encrypted, "hidden.md", SECRET)
    encrypted.put(
        f"/v1/collections/vault/documents/{hidden}/acl", json={"principals": ["group:facilities"]}, headers=ADMIN
    )
    real = crypto.provider()
    unwrapped = []

    class Spy:
        active_id = real.active_id

        def wrap(self, dek):
            return real.wrap(dek)

        def unwrap(self, kek_id, wrapped):
            unwrapped.append(wrapped)
            return real.unwrap(kek_id, wrapped)

    crypto.set_provider(Spy())
    assert _ask(encrypted).status_code == 200
    (open_wrapped,) = _raw("SELECT wrapped_dek FROM documents WHERE id = ?", open_doc)[0]
    assert unwrapped == [open_wrapped]


def test_encrypting_existing_documents_and_rotating_the_key(client, tmp_path, monkeypatch):
    plain = _add(client, "old.md", "Legacy note. " + SECRET)  # stored before encryption was enabled
    assert _raw("SELECT kek_id FROM documents WHERE id = ?", plain)[0][0] is None

    ring = tmp_path / "keyring.json"
    assert keys_cli.main(["generate", "--keyring", str(ring), "--id", "k1"]) == 0
    assert stat.S_IMODE(ring.stat().st_mode) == 0o600
    assert "k1" in json.loads(ring.read_text())["keys"] and json.loads(ring.read_text())["active"] == "k1"
    monkeypatch.setattr(settings, "GATEWAY_ENCRYPT_AT_REST", True)
    monkeypatch.setattr(settings, "GATEWAY_ENCRYPTION_KEY_FILE", str(ring))
    crypto.set_provider(None)
    assert keys_cli.main(["encrypt-existing"]) == 0
    assert rag.encryption_status() == {"plaintext_documents": 0, "documents_by_key": {"k1": 1}}
    assert b"PELICAN" not in _raw("SELECT text FROM chunks WHERE doc_id = ?", plain)[0][0]
    assert b"PELICAN" not in Path(settings.GATEWAY_DB_PATH).read_bytes()  # no plaintext left in free pages
    _add(client, "new.md", "New note about the garden.")

    assert keys_cli.main(["generate", "--keyring", str(ring), "--id", "k2", "--activate"]) == 0
    assert keys_cli.main(["retire", "--keyring", str(ring), "--id", "k2"]) == 1  # active key
    assert keys_cli.main(["retire", "--keyring", str(ring), "--id", "k1"]) == 1  # still in use
    crypto.set_provider(None)
    before = _raw("SELECT text FROM chunks ORDER BY id")
    assert keys_cli.main(["rewrap"]) == 0
    assert rag.encryption_status()["documents_by_key"] == {"k2": 2}
    assert _raw("SELECT text FROM chunks ORDER BY id") == before  # passages untouched; only DEKs re-wrapped
    assert keys_cli.main(["retire", "--keyring", str(ring), "--id", "k1"]) == 0
    crypto.set_provider(None)
    assert any("PELICAN" in s["excerpt"] for s in _ask(client).json()["sources"])  # readable under k2 only
    events = [e["event"] for e in audit.recent(100)]
    assert "documents_encrypted" in events and "encryption_keys_rewrapped" in events


def test_audit_text_fields_are_sealed_and_the_chain_verifies_without_keys(client, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_AUDIT_ENCRYPT_TEXT", True)
    monkeypatch.setattr(settings, "GATEWAY_REDACT_PROMPTS", False)
    monkeypatch.setattr(settings, "GATEWAY_ENCRYPTION_KEY", KEY_A)
    crypto.set_provider(None)
    key = new_key(client, label="intake")
    body = {"messages": [{"role": "user", "content": "Patient code word OSPREY-12"}]}
    assert client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}).status_code == 200
    raw = audit._path().read_text()
    assert "OSPREY" not in raw and '"enc":"aes-256-gcm"' in raw
    entry = next(e for e in client.get("/admin/audit/recent", headers=ADMIN).json() if e["event"] == "completion")
    assert "OSPREY-12" in entry["payload"]["prompt"]  # admins see it, decrypted for display

    monkeypatch.setattr(settings, "GATEWAY_ENCRYPTION_KEY", "")  # the key is gone
    crypto.set_provider(None)
    assert audit.verify()["ok"] is True  # integrity is checkable by anyone, without keys
    entry = next(e for e in audit.recent(20) if e["event"] == "completion")
    assert entry["payload"]["prompt"] == "[encrypted]"
    assert keys_cli.audit_key_ids() == {"env": 2}  # prompt and response


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"GATEWAY_ENCRYPT_AT_REST": True}, "no encryption key configured"),
        ({"GATEWAY_ENCRYPT_AT_REST": True, "GATEWAY_ENCRYPTION_KEY": base64.b64encode(b"short").decode()}, "32 bytes"),
        ({"GATEWAY_AUDIT_ENCRYPT_TEXT": True, "GATEWAY_ENCRYPTION_KEY": "not base64!"}, "not valid base64"),
    ],
)
def test_misconfigured_encryption_fails_at_startup(monkeypatch, updates, message):
    for name, value in {"GATEWAY_ENCRYPTION_KEY": "", "GATEWAY_ENCRYPTION_KEY_FILE": "", **updates}.items():
        monkeypatch.setattr(settings, name, value)
    crypto.set_provider(None)
    with pytest.raises(ValueError, match=message):
        crypto.validate_settings()
    crypto.set_provider(None)


def test_keyring_file_validation(tmp_path):
    ring = tmp_path / "ring.json"
    ring.write_text(json.dumps({"active": "missing", "keys": {"k1": KEY_A}}))
    with pytest.raises(ValueError, match="active key"):
        crypto.LocalKeyring.from_file(ring)
    k = crypto.LocalKeyring({"k1": b"x" * 32}, "k1")
    kid, wrapped = k.wrap(b"d" * 32)
    assert kid == "k1" and k.unwrap("k1", wrapped) == b"d" * 32
    with pytest.raises(crypto.CryptoError, match="not in the keyring"):
        k.unwrap("k0", wrapped)
