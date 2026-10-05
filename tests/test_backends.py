# Corey Mathie, 2026
"""
Pluggable inference backends, against mocked HTTP:
- BACKEND=ollama (default): streaming, include_usage, models, embeddings, error mapping
- BACKEND=openai_compatible (vLLM / SGLang / TGI / NIM): same gateway API, upstream auth, streaming usage,
  embeddings order, models, pull refused, errors surfaced before a stream starts
"""

import json

import httpx
import pydantic
import pytest

from gateway import audit, backends, store
from gateway.config import Settings, settings

from .conftest import ADMIN, fake_ollama, new_key

UPSTREAM = "http://vllm.test:8000/v1"


def _route(monkeypatch, handler):
    """Send every upstream call from gateway/backends.py to `handler`."""
    transport = httpx.MockTransport(handler)

    def async_client(**kwargs):
        kwargs.pop("transport", None)
        return httpx.AsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(backends.httpx, "AsyncClient", async_client)


def _sse_events(text: str) -> list:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            data = line[6:]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


# ---------- Ollama (default) ----------


def ollama_streaming(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/chat" and json.loads(request.content).get("stream"):
        lines = [
            {"message": {"content": "Hel"}, "done": False},
            {"message": {"content": "lo"}, "done": False},
            {"message": {"content": ""}, "done": True, "prompt_eval_count": 9, "eval_count": 2},
        ]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())
    return fake_ollama(request)


def test_default_backend_is_ollama(client):
    assert Settings().BACKEND == "ollama"
    h = client.get("/health").json()
    assert h == {"status": "ok", "backend": "ollama", "backend_ok": True, "ollama": True}


def test_ollama_streaming_with_usage_chunk(client, monkeypatch):
    _route(monkeypatch, ollama_streaming)
    key = new_key(client, label="stream-app")
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    r = client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(r.text)
    assert [e["choices"][0]["delta"]["content"] for e in events[:2]] == ["Hel", "lo"]
    assert events[2]["choices"] == [] and events[2]["usage"]["completion_tokens"] == 2
    assert events[-1] == "[DONE]"
    rec = next(k for k in store.list_keys() if k.key == key)
    assert rec.tokens_total == 11
    completion = next(e for e in audit.recent() if e["event"] == "completion")
    assert completion["payload"]["response"] == "Hello"


def test_ollama_streaming_without_include_usage_is_unchanged(client, monkeypatch):
    _route(monkeypatch, ollama_streaming)
    key = new_key(client)
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    r = client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})
    events = _sse_events(r.text)
    assert all("usage" not in e for e in events if e != "[DONE]") and len(events) == 3


def test_ollama_models_and_upstream_errors(client, monkeypatch):
    key = {"Authorization": f"Bearer {new_key(client)}"}
    assert client.get("/v1/models", headers=key).json()["data"] == [{"id": "llama3.1:8b", "object": "model"}]

    def broken(request):
        if request.url.path == "/api/chat":
            return httpx.Response(404, text='{"error":"model not found"}')
        raise httpx.ConnectError("refused")

    _route(monkeypatch, broken)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", json=body, headers=key)
    assert r.status_code == 404 and "model not found" in r.text  # upstream status passed through, as in 0.4
    assert client.get("/v1/models", headers=key).status_code == 502
    assert client.get("/health").json()["backend_ok"] is False


def test_ollama_stream_error_is_reported_before_streaming(client, monkeypatch):
    _route(monkeypatch, lambda request: httpx.Response(500, text="out of memory"))
    key = new_key(client)
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    r = client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 500 and "out of memory" in r.text


# ---------- OpenAI-compatible (vLLM, SGLang, TGI, NIM) ----------


@pytest.fixture
def vllm(client, monkeypatch):
    """Switch the gateway to BACKEND=openai_compatible and record what reaches the upstream server."""
    monkeypatch.setattr(settings, "BACKEND", "openai_compatible")
    monkeypatch.setattr(settings, "OPENAI_COMPAT_BASE_URL", UPSTREAM)
    monkeypatch.setattr(settings, "OPENAI_COMPAT_API_KEY", "upstream-secret")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "meta-llama/Llama-3.1-8B-Instruct"}]})
        if path == "/v1/embeddings":
            body = json.loads(request.content)
            from .conftest import fake_embedding

            rows = [{"index": i, "embedding": fake_embedding(t)} for i, t in enumerate(body["input"])]
            return httpx.Response(200, json={"data": list(reversed(rows))})  # out of order on purpose
        if path == "/v1/chat/completions":
            body = json.loads(request.content)
            if body.get("stream"):
                chunks = [
                    {"id": "c1", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                    {"id": "c1", "choices": [{"index": 0, "delta": {"content": "Twenty "}}]},
                    {"id": "c1", "choices": [{"index": 0, "delta": {"content": "days [1]."}}]},
                    {"id": "c1", "choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 4}},
                ]
                text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-vllm-1",
                    "model": body["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Twenty days [1]."}}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34},
                },
            )
        return httpx.Response(404)

    _route(monkeypatch, handler)
    return seen


def test_openai_compatible_chat_keeps_gateway_contract(client, vllm):
    key = new_key(client, label="vllm-app")
    r = client.post(
        "/v1/chat/completions",
        json={"model": "meta-llama/Llama-3.1-8B-Instruct", "messages": [{"role": "user", "content": "hi"}],
              "max_tokens": 64, "temperature": 0.2},
        headers={"Authorization": f"Bearer {key}"},
    )  # fmt: skip
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion" and data["id"] == "chatcmpl-vllm-1"
    assert data["choices"][0]["message"]["content"] == "Twenty days [1]."
    assert data["usage"] == {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34}

    upstream = vllm[-1]
    assert str(upstream.url) == f"{UPSTREAM}/chat/completions"
    assert upstream.headers["authorization"] == "Bearer upstream-secret"  # the gateway key never goes upstream
    sent = json.loads(upstream.content)
    assert sent["max_tokens"] == 64 and sent["temperature"] == 0.2 and "options" not in sent


def test_openai_compatible_streaming(client, vllm):
    key = new_key(client)
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": True, "stream_options": {"include_usage": True}}
    r = client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})
    events = _sse_events(r.text)
    assert [e["choices"][0]["delta"]["content"] for e in events[:2]] == ["Twenty ", "days [1]."]
    assert events[2]["usage"] == {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34}
    assert json.loads(vllm[-1].content)["stream_options"] == {"include_usage": True}
    assert next(k for k in store.list_keys() if k.key == key).tokens_total == 34


def test_openai_compatible_models_embeddings_and_health(client, vllm):
    key = {"Authorization": f"Bearer {new_key(client)}"}
    assert client.get("/v1/models", headers=key).json()["data"] == [
        {"id": "meta-llama/Llama-3.1-8B-Instruct", "object": "model"}
    ]
    r = client.post("/v1/embeddings", json={"input": ["alpha beta", "gamma"]}, headers=key)
    vecs = [d["embedding"] for d in r.json()["data"]]
    import numpy as np

    from .conftest import fake_embedding

    expected = np.asarray(fake_embedding("alpha beta"), dtype=np.float32)
    assert np.allclose(vecs[0], expected / np.linalg.norm(expected), atol=1e-5)  # order restored by index
    assert client.get("/health").json() == {"status": "ok", "backend": "openai_compatible", "backend_ok": True}


def test_openai_compatible_document_qa_end_to_end(client, vllm):
    text = "Paid time off.\n\nFull-time employees receive 20 vacation days per year."
    assert (
        client.post(
            "/v1/collections/handbook/documents/text", json={"title": "pto.md", "text": text}, headers=ADMIN
        ).status_code
        == 201
    )
    key = {"Authorization": f"Bearer {new_key(client)}"}
    out = client.post("/v1/collections/handbook/ask", json={"question": "vacation days?"}, headers=key).json()
    assert out["answer"] == "Twenty days [1]." and out["sources"][0]["cited"] is True
    assert {str(r.url).rsplit("/", 1)[-1] for r in vllm} >= {"embeddings", "completions"}


def test_openai_compatible_refuses_model_pull(client, vllm):
    r = client.post("/v1/pull/some-model", headers=ADMIN)
    assert r.status_code == 501
    assert "model_pull_started" not in [e["event"] for e in audit.recent()]


def test_openai_compatible_stream_error_before_first_token(client, vllm, monkeypatch):
    _route(monkeypatch, lambda request: httpx.Response(503, text="server overloaded"))
    key = new_key(client)
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    r = client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 503 and "overloaded" in r.text


def test_no_upstream_key_means_no_authorization_header(client, vllm, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_COMPAT_API_KEY", "")
    client.get("/v1/models", headers={"Authorization": f"Bearer {new_key(client)}"})
    assert "authorization" not in vllm[-1].headers


def test_unknown_backend_is_rejected_at_startup():
    with pytest.raises(pydantic.ValidationError):
        Settings(BACKEND="bedrock")


def test_custom_backend_can_be_installed(client):
    class Echo:
        name = "echo"

        async def chat(self, p):
            return backends.ChatResult(content=p.messages[-1]["content"][::-1], prompt_tokens=1, completion_tokens=1)

        async def health(self):
            return True

    backends.set_backend(Echo())
    try:
        key = {"Authorization": f"Bearer {new_key(client)}"}
        r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "abc"}]}, headers=key)
        assert r.json()["choices"][0]["message"]["content"] == "cba"
    finally:
        backends.set_backend(None)


def test_upstream_http_client_is_pooled_not_rebuilt_per_request(client, monkeypatch):
    """Building an httpx client costs tens of ms (SSL context); the gateway must reuse one per event loop."""
    created = []
    transport = httpx.MockTransport(fake_ollama)

    def factory(**kwargs):
        created.append(kwargs)
        return httpx.AsyncClient(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"})

    monkeypatch.setattr(backends.httpx, "AsyncClient", factory)
    key = {"Authorization": f"Bearer {new_key(client)}"}
    for _ in range(3):
        client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}, headers=key)
    client.get("/v1/models", headers=key)
    client.post("/v1/embeddings", json={"input": "x"}, headers=key)
    assert len(created) == 1
