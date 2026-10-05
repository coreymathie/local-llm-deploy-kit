# Corey Mathie, 2026
"""OpenTelemetry GenAI spans: attributes per the semantic conventions, no prompt text, no-op without OTel."""

import httpx
import pytest

from gateway import backends, telemetry

from .conftest import new_key

sdk = pytest.importorskip("opentelemetry.sdk.trace", reason="opentelemetry-sdk not installed")
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

_EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def _provider():
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    trace.set_tracer_provider(provider)  # global, once per process
    yield


@pytest.fixture
def spans():
    _EXPORTER.clear()
    return _EXPORTER


def test_chat_emits_genai_span(client, spans):
    key = new_key(client)
    secret_prompt = "patient MRN 0012345 needs a follow-up"
    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": secret_prompt}], "max_tokens": 50, "temperature": 0.3},
        headers={"Authorization": f"Bearer {key}"},
    )
    span = next(s for s in spans.get_finished_spans() if s.name == "chat llama3.1:8b")
    a = dict(span.attributes)
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.provider.name"] == "ollama"
    assert a["gen_ai.request.model"] == "llama3.1:8b"
    assert a["gen_ai.request.max_tokens"] == 50 and a["gen_ai.request.temperature"] == 0.3
    assert a["gen_ai.usage.input_tokens"] == 7 and a["gen_ai.usage.output_tokens"] == 5
    assert span.kind == trace.SpanKind.CLIENT
    assert all(secret_prompt not in str(v) for v in a.values())  # content never goes on spans


def test_embeddings_span_and_error_span(client, spans, monkeypatch):
    key = {"Authorization": f"Bearer {new_key(client)}"}
    client.post("/v1/embeddings", json={"input": "hello"}, headers=key)
    emb = next(s for s in spans.get_finished_spans() if s.name == "embeddings nomic-embed-text")
    assert emb.attributes["gen_ai.operation.name"] == "embeddings"

    def down(request):
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(down)
    monkeypatch.setattr(backends.httpx, "AsyncClient", lambda **kw: httpx.AsyncClient(transport=transport))
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}, headers=key)
    failed = [s for s in spans.get_finished_spans() if s.name == "chat llama3.1:8b"][-1]
    assert failed.attributes["error.type"] == "ConnectError"
    assert failed.status.status_code == trace.StatusCode.ERROR


def test_helper_is_a_noop_without_opentelemetry(monkeypatch, spans):
    monkeypatch.setattr(telemetry, "OTEL_AVAILABLE", False)
    with telemetry.genai_span("chat", "ollama", "m") as s:
        s.usage(1, 2, "m")  # must not raise
    assert spans.get_finished_spans() == ()
