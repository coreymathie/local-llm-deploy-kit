# Corey Mathie, 2026
"""Model supply chain: lock file, digest verification (Ollama manifests, weight files), policy, ML-BOM."""

import asyncio
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import audit, backends, main, supply_chain
from gateway.config import settings
from scripts import mlbom, pin_models

from .conftest import ADMIN, fake_ollama, new_key

D_LLAMA = "a" * 64
D_EMBED = "b" * 64
D_QWEN = "c" * 64
CHAT = {"messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def ollama(client, monkeypatch, tmp_path):
    """Ollama that reports digests (as /api/tags does), with a lock file pinning two of its three models."""
    served = {
        "llama3.1:8b": {
            "digest": D_LLAMA,
            "details": {"family": "llama", "parameter_size": "8.0B", "quantization_level": "Q4_K_M"},
        },
        "nomic-embed-text:latest": {"digest": D_EMBED, "details": {"family": "nomic-bert"}},
        "qwen2.5:14b": {"digest": D_QWEN, "details": {}},
    }
    calls = {"tags": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            calls["tags"] += 1
            return httpx.Response(200, json={"models": [{"name": n, **m} for n, m in served.items()]})
        return fake_ollama(request)

    transport = httpx.MockTransport(handler)

    def async_client(**kwargs):
        kwargs.pop("transport", None)
        return httpx.AsyncClient(transport=transport, **kwargs)

    for module in (main, backends):
        monkeypatch.setattr(module.httpx, "AsyncClient", async_client)
    lock = tmp_path / "models.lock.json"
    lock.write_text(
        json.dumps(
            {
                "version": 1,
                "models": [
                    {"name": "llama3.1:8b", "digest": "sha256:" + D_LLAMA, "license": "Llama 3.1 Community License"},
                    {"name": "nomic-embed-text:latest", "digest": D_EMBED, "purpose": "embedding"},
                ],
            }
        )
    )
    monkeypatch.setattr(settings, "GATEWAY_MODEL_LOCK_FILE", str(lock))
    supply_chain.reset()
    yield {"served": served, "calls": calls, "lock": lock}
    supply_chain.reset()


def _chat(client, model, key):
    return client.post(
        "/v1/chat/completions", json={**CHAT, "model": model}, headers={"Authorization": f"Bearer {key}"}
    )


def test_enforce_serves_only_pinned_models_whose_digest_matches(client, ollama, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_MODEL_POLICY", "enforce")
    key = new_key(client)
    assert _chat(client, "llama3.1:8b", key).status_code == 200
    r = _chat(client, "qwen2.5:14b", key)  # served, but not pinned
    assert r.status_code == 403 and "unpinned" in r.json()["detail"]

    ollama["served"]["llama3.1:8b"]["digest"] = "d" * 64  # someone re-pulled or swapped the model
    v = client.post("/admin/models/verify", headers=ADMIN).json()
    assert v["ok"] is False and v["models"]["llama3.1:8b"]["status"] == "mismatch"
    assert v["models"]["llama3.1:8b"]["expected"] == D_LLAMA and v["models"]["llama3.1:8b"]["actual"] == "d" * 64
    r = _chat(client, "llama3.1:8b", key)
    assert r.status_code == 403 and "mismatch" in r.json()["detail"]
    entry = next(
        e for e in audit.recent(50) if e["event"] == "model_verification" and e["payload"]["trigger"] == "admin"
    )
    assert entry["payload"]["problems"] == {"llama3.1:8b": "mismatch"} and entry["payload"]["by"] == "bootstrap admin"
    assert (
        'gateway_model_policy_decisions_total{action="refused",model="llama3.1:8b",status="mismatch"}'
        in client.get("/metrics").text
    )


def test_enforce_covers_embeddings_ingestion_and_document_questions(client, ollama, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_MODEL_POLICY", "enforce")
    key = {"Authorization": f"Bearer {new_key(client)}"}
    # GATEWAY_EMBED_MODEL is "nomic-embed-text"; Ollama serves it as "nomic-embed-text:latest" (pinned).
    doc = {"title": "a.md", "text": "Hotel cap is $210 per night."}
    assert client.post("/v1/collections/p/documents/text", json=doc, headers=ADMIN).status_code == 201
    assert client.post("/v1/embeddings", json={"input": "x"}, headers=key).status_code == 200
    assert client.post("/v1/embeddings", json={"input": "x", "model": "qwen2.5:14b"}, headers=key).status_code == 403
    assert client.post("/v1/collections/p/ask", json={"question": "hotel cap?"}, headers=key).status_code == 200
    r = client.post("/v1/collections/p/ask", json={"question": "hotel cap?", "model": "qwen2.5:14b"}, headers=key)
    assert r.status_code == 403
    ollama["served"]["nomic-embed-text:latest"]["digest"] = "e" * 64
    client.post("/admin/models/verify", headers=ADMIN)
    assert client.post("/v1/collections/p/documents/text", json=doc, headers=ADMIN).status_code == 403


def test_warn_serves_but_flags_and_records_the_status(client, ollama, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_MODEL_POLICY", "warn")
    key = new_key(client)
    assert _chat(client, "qwen2.5:14b", key).status_code == 200
    client.post("/v1/collections/p/documents/text", json={"title": "a.md", "text": "Hotel cap."}, headers=ADMIN)
    client.post(
        "/v1/collections/p/ask",
        json={"question": "cap?", "model": "qwen2.5:14b"},
        headers={"Authorization": f"Bearer {key}"},
    )
    q = next(e for e in audit.recent(20) if e["event"] == "document_question")
    assert q["payload"]["model_verification"] == "unpinned"
    assert 'action="warned",model="qwen2.5:14b",status="unpinned"' in client.get("/metrics").text


def test_digests_are_rechecked_after_the_interval_and_after_pulls(client, ollama, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_MODEL_POLICY", "enforce")
    key = new_key(client)
    monkeypatch.setattr(settings, "GATEWAY_MODEL_VERIFY_INTERVAL_SECONDS", 3600)
    _chat(client, "llama3.1:8b", key)
    before = ollama["calls"]["tags"]
    _chat(client, "llama3.1:8b", key)
    assert ollama["calls"]["tags"] == before  # cached
    monkeypatch.setattr(settings, "GATEWAY_MODEL_VERIFY_INTERVAL_SECONDS", 0)
    ollama["served"]["llama3.1:8b"]["digest"] = "f" * 64  # swapped behind the gateway's back
    assert _chat(client, "llama3.1:8b", key).status_code == 403  # caught by the lazy re-check
    monkeypatch.setattr(settings, "GATEWAY_MODEL_VERIFY_INTERVAL_SECONDS", 3600)
    ollama["served"]["llama3.1:8b"]["digest"] = D_LLAMA
    assert client.post("/v1/pull/llama3.1:8b", headers=ADMIN).status_code == 202  # pull -> re-verify
    assert _chat(client, "llama3.1:8b", key).status_code == 200
    triggers = [e["payload"]["trigger"] for e in audit.recent(100) if e["event"] == "model_verification"]
    assert "model_pull" in triggers


def test_startup_verification_is_audited_and_admin_endpoints_are_admin_only(ollama, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_MODEL_POLICY", "warn")
    with TestClient(main.app) as c:
        entry = next(e for e in audit.recent(50) if e["event"] == "model_verification")
        assert entry["payload"]["trigger"] == "startup" and entry["payload"]["counts"] == {"verified": 2, "unpinned": 1}
        key = new_key(c)
        assert c.get("/admin/models/verification", headers={"Authorization": f"Bearer {key}"}).status_code == 403
        status = c.get("/admin/models/verification", headers=ADMIN).json()
        assert status["models"]["llama3.1:8b"]["info"]["quantization_level"] == "Q4_K_M"


def test_backend_down_at_startup_does_not_block_and_enforce_fails_closed(ollama, monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    transport = httpx.MockTransport(down)
    monkeypatch.setattr(
        backends.httpx,
        "AsyncClient",
        lambda **kw: httpx.AsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
    )
    monkeypatch.setattr(settings, "GATEWAY_MODEL_POLICY", "enforce")
    with TestClient(main.app) as c:
        entry = next(e for e in audit.recent(50) if e["event"] == "model_verification")
        assert entry["payload"]["ok"] is False and "backend unavailable" in entry["payload"]["error"]
        r = _chat(c, "llama3.1:8b", "sk-local-admin")
        assert r.status_code == 403 and "(error)" in r.json()["detail"]


def test_malformed_lock_file_stops_startup(ollama, tmp_path, monkeypatch):
    for bad, message in (
        ({"version": 2, "models": []}, "version"),
        ({"version": 1, "models": [{"name": "x", "digest": "sha256:123"}]}, "SHA-256"),
        ({"version": 1, "models": [{"name": "x"}, {"name": "x"}]}, "twice"),
        ({"version": 1, "models": [{"name": "x", "backend": "tgi"}]}, "unknown backend"),
        ({"version": 1, "models": [{"name": "x", "files": [{"path": "/m"}]}]}, "path and a sha256"),
    ):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(bad))
        with pytest.raises(supply_chain.LockError, match=message):
            supply_chain.load_lock(p)
    monkeypatch.setattr(settings, "GATEWAY_MODEL_LOCK_FILE", str(p))
    with pytest.raises(supply_chain.LockError), TestClient(main.app):
        pass


class FileBackend:
    """An OpenAI-compatible server only reports names; weights are verified from files."""

    name = "openai_compatible"

    def __init__(self, names):
        self.names = names

    async def list_models(self):
        return self.names


def test_openai_compatible_models_are_verified_by_hashing_pinned_weight_files(tmp_path):
    weights = tmp_path / "model-00001-of-00001.safetensors"
    weights.write_bytes(b"\x00weights" * 1000)
    good = hashlib.sha256(weights.read_bytes()).hexdigest()
    pins = [
        supply_chain.Pin("org/model-a", "openai_compatible", files=[{"path": str(weights), "sha256": good}]),
        supply_chain.Pin("org/model-b", "openai_compatible"),
        supply_chain.Pin("org/model-c", "openai_compatible", files=[{"path": str(tmp_path / "nope"), "sha256": good}]),
        supply_chain.Pin("org/gone", "openai_compatible", files=[{"path": str(weights), "sha256": good}]),
    ]
    backend = FileBackend(["org/model-a", "org/model-b", "org/model-c", "org/extra"])
    r = asyncio.run(supply_chain.verify(backend, pins))
    status = {n: m["status"] for n, m in r["models"].items()}
    assert status == {
        "org/model-a": "verified",
        "org/model-b": "unverifiable",
        "org/model-c": "error",
        "org/gone": "missing",
        "org/extra": "unpinned",
    }
    weights.write_bytes(b"\x00tampered" * 1000)
    assert asyncio.run(supply_chain.verify(backend, pins[:1]))["models"]["org/model-a"]["status"] == "mismatch"


def test_ml_bom_is_valid_cyclonedx_1_6_with_models_and_dependencies(ollama, tmp_path):
    validation = pytest.importorskip("cyclonedx.validation.json")
    from cyclonedx.schema import SchemaVersion

    weights = tmp_path / "w.safetensors"
    weights.write_bytes(b"w")
    lock = json.loads(ollama["lock"].read_text())
    lock["models"].append(
        {
            "name": "org/model-a",
            "backend": "openai_compatible",
            "files": [{"path": str(weights), "sha256": hashlib.sha256(b"w").hexdigest()}],
            "source": "https://example.test/org/model-a",
            "license": "Apache-2.0",
            "purpose": "text-generation",
        }
    )
    ollama["lock"].write_text(json.dumps(lock))
    verification = asyncio.run(supply_chain.verify(backends.get_backend()))
    bom = mlbom.build(ollama["lock"], mlbom.ROOT / "requirements.txt", verification, version="0.6.0")
    assert validation.JsonStrictValidator(SchemaVersion.V1_6).validate_str(json.dumps(bom)) is None

    models = {c["name"]: c for c in bom["components"] if c["type"] == "machine-learning-model"}
    assert models["llama3.1:8b"]["hashes"] == [{"alg": "SHA-256", "content": D_LLAMA}]
    props = {p["name"]: p["value"] for p in models["llama3.1:8b"]["properties"]}
    assert props["lldk:verification"] == "verified" and props["lldk:ollama:quantization_level"] == "Q4_K_M"
    assert models["org/model-a"]["components"][0]["hashes"][0]["content"] == hashlib.sha256(b"w").hexdigest()
    libs = {c["name"].lower(): c for c in bom["components"] if c["type"] == "library"}
    assert libs["fastapi"]["purl"].startswith("pkg:pypi/fastapi@") and "pyjwt" in libs and "cryptography" in libs
    top = bom["dependencies"][0]
    assert top["ref"] == "local-llm-deploy-kit" and "model:ollama:llama3.1:8b" in top["dependsOn"]
    refs = {c["bom-ref"] for c in bom["components"]}
    assert all(d in refs for dep in bom["dependencies"][1:] for d in dep["dependsOn"])  # no dangling edges


def test_pin_models_records_served_digests_and_keeps_metadata(ollama, tmp_path):
    out = tmp_path / "pinned.json"
    out.write_text(json.dumps({"version": 1, "models": [{"name": "llama3.1:8b", "digest": "9" * 64, "license": "L"}]}))
    asyncio.run(pin_models.pin(out, [], update=False))
    doc = {m["name"]: m for m in json.loads(out.read_text())["models"]}
    assert doc["llama3.1:8b"] == {
        "name": "llama3.1:8b",
        "backend": "ollama",
        "license": "L",
        "digest": "sha256:" + "9" * 64,
    }
    assert doc["qwen2.5:14b"]["digest"] == "sha256:" + D_QWEN
    asyncio.run(pin_models.pin(out, [], update=True))
    assert json.loads(out.read_text())["models"][0]["digest"] == "sha256:" + D_LLAMA
    assert supply_chain.load_lock(out)  # the result is a valid lock file
