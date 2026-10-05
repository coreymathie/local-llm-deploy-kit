# Corey Mathie, 2026
"""
Optional OpenTelemetry spans following the GenAI semantic conventions.

If `opentelemetry-api` isn't installed, every helper here is a no-op. If it is
installed but no SDK/exporter is configured, the API hands out non-recording
spans, which also cost almost nothing. To export, run the gateway under the
standard OTel SDK configuration, e.g.

    pip install opentelemetry-distro opentelemetry-exporter-otlp
    OTEL_SERVICE_NAME=llm-gateway OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 \
        opentelemetry-instrument uvicorn gateway.main:app

Attributes (https://opentelemetry.io/docs/specs/semconv/gen-ai/):
gen_ai.operation.name, gen_ai.provider.name, gen_ai.request.model,
gen_ai.request.temperature, gen_ai.request.max_tokens, gen_ai.response.model,
gen_ai.usage.input_tokens, gen_ai.usage.output_tokens, error.type.
Prompt and completion text are never put on spans.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

try:  # pragma: no cover - exercised by whichever environment the tests run in
    from opentelemetry import context as _context
    from opentelemetry import trace as _trace
    from opentelemetry.trace import SpanKind, Status, StatusCode

    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _trace = None
    OTEL_AVAILABLE = False


class GenAISpan:
    """Thin wrapper so callers don't need to know whether OTel is present."""

    def __init__(self, span: Any = None):
        self._span = span

    def set(self, key: str, value: Any) -> None:
        if self._span is not None and value is not None:
            self._span.set_attribute(key, value)

    def usage(self, input_tokens: int | None, output_tokens: int | None, response_model: str | None = None) -> None:
        self.set("gen_ai.usage.input_tokens", input_tokens)
        self.set("gen_ai.usage.output_tokens", output_tokens)
        self.set("gen_ai.response.model", response_model)


@contextmanager
def genai_span(
    operation: str,
    provider: str,
    model: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    current: bool = True,
):
    """`with genai_span("chat", "ollama", "llama3.1:8b") as s: ... s.usage(7, 5)`

    current=False starts the span without making it the active context; use it
    around async generators (streaming), where the context can't be safely
    attached and detached across yields.
    """
    if not OTEL_AVAILABLE or _trace is None:
        yield GenAISpan()
        return
    tracer = _trace.get_tracer("private-llm-platform.gateway")
    span = tracer.start_span(f"{operation} {model}", kind=SpanKind.CLIENT)
    token = _context.attach(_trace.set_span_in_context(span)) if current else None
    wrapper = GenAISpan(span)
    wrapper.set("gen_ai.operation.name", operation)
    wrapper.set("gen_ai.provider.name", provider)
    wrapper.set("gen_ai.request.model", model)
    wrapper.set("gen_ai.request.temperature", temperature)
    wrapper.set("gen_ai.request.max_tokens", max_tokens)
    try:
        yield wrapper
    except Exception as e:
        span.set_attribute("error.type", type(e).__name__)
        span.set_status(Status(StatusCode.ERROR))
        raise
    finally:
        if token is not None:
            _context.detach(token)
        span.end()
