# Corey Mathie, 2026
"""
Gateway tests against a mocked Ollama: auth, admin, chat, usage accounting,
rate limiting, background model pulls, and the tamper-evident audit log.
"""

import json

from gateway import audit, store
from gateway.config import settings

from .conftest import ADMIN, new_key


def test_requires_bearer_key(client):
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/v1/chat/completions", json={"messages": []}, headers=bad).status_code == 401


def test_non_admin_cannot_manage_keys(client):
    key = new_key(client)
    r = client.get("/admin/keys", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403


def test_chat_is_openai_compatible_and_counts_usage(client):
    key = new_key(client)
    r = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "Hello there"
    assert data["usage"] == {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
    rec = next(k for k in store.list_keys() if k.key == key)
    assert rec.requests_total == 1 and rec.tokens_total == 12


def test_revoked_key_is_rejected(client):
    key = new_key(client)
    client.delete(f"/admin/keys/{key}", headers=ADMIN)
    r = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 401


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_RATE_LIMIT_PER_MIN", 2)
    key = new_key(client)
    h = {"Authorization": f"Bearer {key}"}
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert [client.post("/v1/chat/completions", json=body, headers=h).status_code for _ in range(3)] == [200, 200, 429]


def test_audit_log_records_admin_actions_and_redacted_prompts(client):
    key = new_key(client, label="clinic-app")
    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "what is my ssn? email me at pat@example.com"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    events = [e["event"] for e in client.get("/admin/audit/recent", headers=ADMIN).json()]
    assert "key_created" in events and "completion" in events

    raw = (audit._path()).read_text()
    assert "123-45-6789" not in raw and "pat@example.com" not in raw
    assert key not in raw  # full keys never land in the audit log

    v = client.get("/admin/audit/verify", headers=ADMIN).json()
    assert v["ok"] is True and v["entries"] >= 3


def test_audit_verify_detects_tampering(client):
    new_key(client, label="a")
    new_key(client, label="b")
    path = audit._path()
    lines = path.read_text().splitlines()
    row = json.loads(lines[1])
    row["payload"]["label"] = "edited"
    lines[1] = json.dumps(row, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    v = client.get("/admin/audit/verify", headers=ADMIN).json()
    assert v == {"ok": False, "entries": 2, "bad_line": 2}


def test_model_pull_runs_in_background_and_is_audited(client):
    r = client.post("/v1/pull/qwen2.5:14b", headers=ADMIN)
    assert r.status_code == 202
    events = [e["event"] for e in client.get("/admin/audit/recent", headers=ADMIN).json()]
    assert "model_pull_started" in events and "model_pull_finished" in events


def test_bootstrap_admin_created_once(client):
    assert store.ensure_bootstrap_admin() is None  # already created at startup


def test_cannot_revoke_last_admin(client):
    r = client.delete("/admin/keys/sk-local-admin", headers=ADMIN)
    assert r.status_code == 409
    second = new_key(client, label="ops", is_admin=True)
    assert client.delete("/admin/keys/sk-local-admin", headers={"Authorization": f"Bearer {second}"}).status_code == 200
