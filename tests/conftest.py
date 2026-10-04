# Corey Mathie, 2026
"""Shared fixtures: an isolated gateway database and a mocked Ollama (chat, pull, tags, embed)."""

import hashlib
import json
import re
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import auth, backends, main
from gateway.config import settings

ADMIN = {"Authorization": "Bearer sk-local-admin"}


def fake_ollama(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}]})
    if request.url.path == "/api/chat":
        body = json.loads(request.content)
        reply = "My SSN is 123-45-6789" if "ssn" in body["messages"][-1]["content"].lower() else "Hello there"
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": reply},
                "prompt_eval_count": 7,
                "eval_count": 5,
                "created_at": "t",
            },
        )
    if request.url.path == "/api/pull":
        return httpx.Response(200, json={"status": "success"})
    if request.url.path == "/api/embed":
        body = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [fake_embedding(t) for t in body["input"]]})
    return httpx.Response(404)


def fake_embedding(text: str, dim: int = 256) -> list[float]:
    """Bag-of-words hashing: texts that share words land close together, like a real embedding model."""
    vec = [0.0] * dim
    for word in re.findall(r"[a-z0-9$]+", text.lower()):
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % dim] += 1.0
    return vec


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_DB_PATH", str(tmp_path / "gw.db"))
    monkeypatch.setattr(settings, "GATEWAY_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "GATEWAY_ADMIN_BOOTSTRAP_KEY", "sk-local-admin")
    monkeypatch.setattr(settings, "GATEWAY_LOG_PROMPTS", True)
    monkeypatch.setattr(settings, "GATEWAY_REDACT_PROMPTS", True)
    monkeypatch.setattr(settings, "GATEWAY_RATE_LIMIT_PER_MIN", 60)
    auth._calls.clear()

    transport = httpx.MockTransport(fake_ollama)

    def async_client(**kwargs):
        kwargs.pop("transport", None)
        return httpx.AsyncClient(transport=transport, **kwargs)

    fake_httpx = SimpleNamespace(AsyncClient=async_client, HTTPError=httpx.HTTPError)
    monkeypatch.setattr(main, "httpx", fake_httpx)
    monkeypatch.setattr(backends, "httpx", fake_httpx)  # all upstream HTTP goes through gateway/backends.py
    monkeypatch.setattr(settings, "BACKEND", "ollama")
    backends.set_backend(None)
    with TestClient(main.app) as c:
        yield c


def new_key(client, label="app", is_admin=False) -> str:
    r = client.post("/admin/keys", json={"label": label, "is_admin": is_admin}, headers=ADMIN)
    assert r.status_code == 200
    return r.json()["key"]
