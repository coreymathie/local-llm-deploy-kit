# Corey Mathie, 2026
"""Backup and restore drill: online SQLite backup + audit log + manifest; verified restore; refusal cases."""

import base64
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway import audit, crypto, main
from gateway.config import settings
from scripts import backup as backup_cli
from scripts import restore as restore_cli

from .conftest import ADMIN, new_key

KEY = base64.b64encode(b"K" * 32).decode()


@pytest.fixture
def populated(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "GATEWAY_ENCRYPT_AT_REST", True)
    monkeypatch.setattr(settings, "GATEWAY_ENCRYPTION_KEY", KEY)
    crypto.set_provider(None)
    r = client.post(
        "/v1/collections/ops/documents/text",
        json={
            "title": "dr.md",
            "text": "Disaster recovery.\n\nThe standby site is in building C.",
            "acl": ["group:ops"],
        },
        headers=ADMIN,
    )
    assert r.status_code == 201
    key = client.post("/admin/keys", json={"label": "ops-bot", "groups": ["ops"]}, headers=ADMIN).json()["key"]
    yield client, key, tmp_path / "backups"
    crypto.set_provider(None)


def _ask(client, key):
    return client.post(
        "/v1/collections/ops/ask",
        json={"question": "Where is the standby site?"},
        headers={"Authorization": f"Bearer {key}"},
    )


def test_backup_restore_drill_brings_back_documents_keys_acls_and_the_audit_chain(populated):
    client, key, out = populated
    assert _ask(client, key).status_code == 200
    db, log_dir = Path(settings.GATEWAY_DB_PATH), Path(settings.GATEWAY_LOG_DIR)
    entries_before = audit.verify()["entries"]

    assert backup_cli.main(["--out", str(out)]) == 0  # while the gateway (TestClient) is running
    (target,) = out.iterdir()
    manifest = json.loads((target / "manifest.json").read_text())
    assert set(manifest["files"]) == {"gateway.db", "audit.jsonl"}
    assert (
        manifest["sqlite_integrity"] == "ok"
        and manifest["audit"]["ok"]
        and manifest["audit"]["entries"] == entries_before
    )
    assert manifest["encryption"] == {"keys_needed": ["env"], "keyring_included": False}
    assert KEY not in json.dumps(manifest) and KEY.encode() not in (target / "gateway.db").read_bytes()

    db.unlink()  # disaster
    shutil.rmtree(log_dir)
    assert restore_cli.main([str(target)]) == 0
    with TestClient(main.app) as fresh:  # a new gateway process on the restored files
        r = _ask(fresh, key)
        assert r.status_code == 200 and "building C" in r.json()["sources"][0]["excerpt"]
        other = new_key(fresh, label="no-groups")
        assert _ask(fresh, other).status_code == 404  # the document's ACL came back too
        v = fresh.get("/admin/audit/verify", headers=ADMIN).json()
        assert v["ok"] and v["entries"] > entries_before  # the restored chain keeps growing and verifying


def test_restore_refuses_altered_or_incomplete_backups_and_leaves_live_files_alone(populated):
    _client, _, out = populated
    target = backup_cli.backup(out, Path(settings.GATEWAY_DB_PATH), Path(settings.GATEWAY_LOG_DIR))
    live = Path(settings.GATEWAY_DB_PATH).read_bytes()

    tampered = out / "tampered"
    shutil.copytree(target, tampered)
    with (tampered / "audit.jsonl").open("a") as f:
        f.write('{"forged": true}\n')
    with pytest.raises(restore_cli.RestoreError, match="checksum"):
        restore_cli.check_backup(tampered)

    rehashed = out / "rehashed"  # the attacker also updates the manifest: the audit chain still catches it
    shutil.copytree(tampered, rehashed)
    m = json.loads((rehashed / "manifest.json").read_text())
    m["files"]["audit.jsonl"]["sha256"] = backup_cli.sha256_file(rehashed / "audit.jsonl")
    (rehashed / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(restore_cli.RestoreError, match="audit chain is broken"):
        restore_cli.check_backup(rehashed)

    (target / "manifest.json").rename(target / "manifest.bak")
    with pytest.raises(restore_cli.RestoreError, match="no manifest"):
        restore_cli.check_backup(target)
    assert Path(settings.GATEWAY_DB_PATH).read_bytes() == live


def test_restore_needs_the_keys_and_does_not_overwrite_without_force(populated, monkeypatch):
    _client, _, out = populated
    target = backup_cli.backup(out, Path(settings.GATEWAY_DB_PATH), Path(settings.GATEWAY_LOG_DIR))
    db, log_dir = Path(settings.GATEWAY_DB_PATH), Path(settings.GATEWAY_LOG_DIR)

    with pytest.raises(restore_cli.RestoreError, match="refusing to overwrite"):
        restore_cli.restore(target, db, log_dir)
    monkeypatch.setattr(settings, "GATEWAY_ENCRYPTION_KEY", base64.b64encode(b"Z" * 32).decode())
    crypto.set_provider(crypto.LocalKeyring({"new-key": b"Z" * 32}, "new-key"))
    with pytest.raises(restore_cli.RestoreError, match=r"lacks keys \['env'\]"):
        restore_cli.restore(target, db, log_dir, force=True)
    crypto.set_provider(None)
    monkeypatch.setattr(settings, "GATEWAY_ENCRYPTION_KEY", KEY)

    out_info = restore_cli.restore(target, db, log_dir, force=True)
    assert len(out_info["moved_aside"]) == 2 and all(Path(p).exists() for p in out_info["moved_aside"])
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restore_cli.main([str(target), "--check-only"]) == 0


def test_storage_benchmark_script_runs_and_restores_settings():
    from scripts import bench_storage

    before = settings.model_dump()
    out = bench_storage.measure(3)
    assert out["plaintext"]["passages"] == out["encrypted"]["passages"] > 0
    assert {"backup_ms", "restore_ms"} <= set(out["encrypted"])
    assert settings.model_dump() == before
