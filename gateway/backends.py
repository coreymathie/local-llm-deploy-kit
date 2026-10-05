# Corey Mathie, 2026
"""
Pluggable inference backends.

The gateway's public API (OpenAI-compatible chat, embeddings, models, document
Q&A) stays the same; only the server it forwards to changes.

- BACKEND=ollama (default): Ollama's native API (/api/chat, /api/embed,
  /api/tags, /api/pull).
- BACKEND=openai_compatible: any server exposing /v1/chat/completions,
  /v1/embeddings and /v1/models, such as vLLM, SGLang, Hugging Face TGI
  (Messages API) or NVIDIA NIM. Set OPENAI_COMPAT_BASE_URL (including the /v1
  suffix) and, if the server requires one, OPENAI_COMPAT_API_KEY.

Each backend returns plain dataclasses so routes, metrics and tracing do not
depend on the upstream wire format. `get_backend()` wraps the configured
backend in `Instrumented`, which records Prometheus metrics (if
prometheus_client is installed) and OpenTelemetry GenAI spans (if
opentelemetry is installed) around every call.
"""

from __future__ import annotations

import asyncio
import json
import time
import weakref
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from httpx import Limits, Timeout

from .config import settings
from .telemetry import genai_span

try:
    from . import metrics as _metrics
except ImportError:  # prometheus_client isn't installed (e.g. the browser demo): metrics become no-ops
    _metrics = None


class BackendHTTPError(Exception):
    """The upstream server answered with a non-2xx status. Routes pass the status through."""

    def __init__(self, status_code: int, text: str):
        super().__init__(f"{status_code}: {text[:200]}")
        self.status_code = status_code
        self.text = text


class BackendNotSupported(Exception):
    """The operation exists on one backend but not on the configured one (e.g. model pulls)."""


@dataclass
class ChatResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    id: str = ""
    model: str = ""


@dataclass
class StreamEvent:
    """One streamed piece of text, or (done=True) the final usage counts."""

    content: str = ""
    done: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class ChatParams:
    model: str
    messages: list[dict]
    temperature: float = 0.7
    max_tokens: int | None = None
    extra: dict = field(default_factory=dict)


class Backend(Protocol):
    name: str

    async def chat(self, p: ChatParams) -> ChatResult: ...

    def stream_chat(self, p: ChatParams) -> AsyncIterator[StreamEvent]: ...

    async def embed(self, texts: list[str], model: str) -> list[list[float]]: ...

    async def list_models(self) -> list[str]: ...

    async def health(self) -> bool: ...

    async def pull(self, model: str) -> str: ...


# One pooled AsyncClient per event loop. Building a client creates an SSL context and loads the CA
# bundle (tens of milliseconds of blocking CPU), so a client per request caps throughput; see
# docs/benchmarks.md. Keyed by loop (and by the client factory, so tests can swap transports).
_clients: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_STREAM_TIMEOUT = Timeout(None, connect=10.0)


def _client() -> httpx.AsyncClient:
    per_loop = _clients.setdefault(asyncio.get_running_loop(), {})
    factory = httpx.AsyncClient
    client = per_loop.get(factory)
    if client is None or client.is_closed:
        client = factory(
            timeout=Timeout(300.0, connect=10.0),
            limits=Limits(max_connections=256, max_keepalive_connections=64),
        )
        per_loop[factory] = client
    return client


async def aclose_clients() -> None:
    """Close this event loop's pooled clients (called on gateway shutdown)."""
    try:
        per_loop = _clients.pop(asyncio.get_running_loop(), {})
    except KeyError:
        return
    for client in per_loop.values():
        await client.aclose()


def _check(r) -> None:
    if r.status_code != 200:
        raise BackendHTTPError(r.status_code, r.text)


# ---------- Ollama ----------


class OllamaBackend:
    name = "ollama"
    supports_pull = True

    def __init__(self, host: str | None = None):
        self._host = host

    @property
    def host(self) -> str:
        return (self._host or settings.OLLAMA_HOST).rstrip("/")

    def _payload(self, p: ChatParams, stream: bool) -> dict:
        payload = {
            "model": p.model,
            "messages": p.messages,
            "stream": stream,
            "options": {"temperature": p.temperature},
        }
        if p.max_tokens:
            payload["options"]["num_predict"] = p.max_tokens
        return payload

    async def chat(self, p: ChatParams) -> ChatResult:
        r = await _client().post(f"{self.host}/api/chat", json=self._payload(p, stream=False), timeout=300.0)
        _check(r)
        data = r.json()
        return ChatResult(
            content=data["message"]["content"],
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            id=f"chatcmpl-{data.get('created_at', '')}",
            model=p.model,
        )

    async def stream_chat(self, p: ChatParams) -> AsyncIterator[StreamEvent]:
        async with _client().stream(
            "POST", f"{self.host}/api/chat", json=self._payload(p, stream=True), timeout=_STREAM_TIMEOUT
        ) as r:
            if r.status_code != 200:
                await r.aread()
                raise BackendHTTPError(r.status_code, r.text)
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                piece = chunk.get("message", {}).get("content")
                if piece:
                    yield StreamEvent(content=piece)
                if chunk.get("done"):
                    yield StreamEvent(
                        done=True,
                        prompt_tokens=chunk.get("prompt_eval_count", 0),
                        completion_tokens=chunk.get("eval_count", 0),
                    )

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        r = await _client().post(f"{self.host}/api/embed", json={"model": model, "input": texts}, timeout=120.0)
        r.raise_for_status()
        return r.json()["embeddings"]

    async def list_models(self) -> list[str]:
        r = await _client().get(f"{self.host}/api/tags", timeout=10.0)
        _check(r)
        return [m["name"] for m in r.json().get("models", [])]

    async def model_inventory(self) -> dict[str, dict]:
        """Served models with the manifest digest and details Ollama reports (used by supply_chain.py)."""
        r = await _client().get(f"{self.host}/api/tags", timeout=10.0)
        _check(r)
        return {
            m["name"]: {"digest": m.get("digest", ""), "details": m.get("details") or {}, "size": m.get("size")}
            for m in r.json().get("models", [])
        }

    async def health(self) -> bool:
        try:
            r = await _client().get(f"{self.host}/api/tags", timeout=3.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def pull(self, model: str) -> str:
        r = await _client().post(f"{self.host}/api/pull", json={"name": model, "stream": False}, timeout=None)
        return "completed" if r.status_code == 200 else f"failed ({r.status_code})"


# ---------- OpenAI-compatible (vLLM, SGLang, TGI, NVIDIA NIM, ...) ----------


class OpenAICompatibleBackend:
    name = "openai_compatible"
    supports_pull = False

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self._base_url = base_url
        self._api_key = api_key

    @property
    def base_url(self) -> str:
        return (self._base_url or settings.OPENAI_COMPAT_BASE_URL).rstrip("/")

    @property
    def headers(self) -> dict:
        key = self._api_key if self._api_key is not None else settings.OPENAI_COMPAT_API_KEY
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _payload(self, p: ChatParams, stream: bool) -> dict:
        payload = {"model": p.model, "messages": p.messages, "temperature": p.temperature, "stream": stream}
        if p.max_tokens:
            payload["max_tokens"] = p.max_tokens
        if stream:
            # vLLM, SGLang and NIM send a final chunk with token usage when asked.
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def chat(self, p: ChatParams) -> ChatResult:
        r = await _client().post(
            f"{self.base_url}/chat/completions",
            json=self._payload(p, stream=False),
            headers=self.headers,
            timeout=300.0,
        )
        _check(r)
        data = r.json()
        usage = data.get("usage") or {}
        return ChatResult(
            content=data["choices"][0]["message"].get("content") or "",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            id=data.get("id", ""),
            model=data.get("model", p.model),
        )

    async def stream_chat(self, p: ChatParams) -> AsyncIterator[StreamEvent]:
        prompt_tokens = completion_tokens = 0
        async with _client().stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=self._payload(p, stream=True),
            headers=self.headers,
            timeout=_STREAM_TIMEOUT,
        ) as r:
            if r.status_code != 200:
                await r.aread()
                raise BackendHTTPError(r.status_code, r.text)
            async for line in r.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    prompt_tokens = chunk["usage"].get("prompt_tokens", 0)
                    completion_tokens = chunk["usage"].get("completion_tokens", 0)
                for choice in chunk.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        yield StreamEvent(content=piece)
        yield StreamEvent(done=True, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        r = await _client().post(
            f"{self.base_url}/embeddings", json={"model": model, "input": texts}, headers=self.headers, timeout=120.0
        )
        r.raise_for_status()
        rows = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in rows]

    async def list_models(self) -> list[str]:
        r = await _client().get(f"{self.base_url}/models", headers=self.headers, timeout=10.0)
        _check(r)
        return [m["id"] for m in r.json().get("data", [])]

    async def health(self) -> bool:
        try:
            r = await _client().get(f"{self.base_url}/models", headers=self.headers, timeout=3.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def pull(self, model: str) -> str:
        raise BackendNotSupported(
            "model downloads are managed by the inference server (e.g. vLLM --model); the gateway can't pull"
        )


# ---------- Instrumentation ----------


def _reason(e: Exception) -> str:
    if isinstance(e, BackendHTTPError):
        return f"http_{e.status_code}"
    if isinstance(e, BackendNotSupported):
        return "not_supported"
    return type(e).__name__


class Instrumented:
    """Wraps any backend with latency/error metrics and GenAI spans. Never sees or records prompt text."""

    def __init__(self, inner: Backend):
        self.inner = inner
        self.name = inner.name
        self.supports_pull = getattr(inner, "supports_pull", False)

    def _observe(self, operation: str, model: str, start: float, error: Exception | None = None) -> None:
        if _metrics is None:
            return
        _metrics.LLM_LATENCY.labels(operation, self.name, model).observe(time.perf_counter() - start)
        if error is not None:
            _metrics.BACKEND_ERRORS.labels(operation, self.name, _reason(error)).inc()

    async def chat(self, p: ChatParams) -> ChatResult:
        start = time.perf_counter()
        with genai_span("chat", self.name, p.model, p.temperature, p.max_tokens) as span:
            try:
                result = await self.inner.chat(p)
            except Exception as e:
                self._observe("chat", p.model, start, e)
                raise
            span.usage(result.prompt_tokens, result.completion_tokens, result.model or p.model)
        self._observe("chat", p.model, start)
        return result

    async def stream_chat(self, p: ChatParams) -> AsyncIterator[StreamEvent]:
        start = time.perf_counter()
        first = True
        with genai_span("chat", self.name, p.model, p.temperature, p.max_tokens, current=False) as span:
            try:
                async for event in self.inner.stream_chat(p):
                    if first and event.content:
                        first = False
                        if _metrics is not None:
                            _metrics.LLM_TTFT.labels(self.name, p.model).observe(time.perf_counter() - start)
                    if event.done:
                        span.usage(event.prompt_tokens, event.completion_tokens, p.model)
                    yield event
            except Exception as e:
                self._observe("chat_stream", p.model, start, e)
                raise
        self._observe("chat_stream", p.model, start)

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        start = time.perf_counter()
        with genai_span("embeddings", self.name, model):
            try:
                rows = await self.inner.embed(texts, model)
            except Exception as e:
                self._observe("embeddings", model, start, e)
                raise
        self._observe("embeddings", model, start)
        return rows

    async def list_models(self) -> list[str]:
        return await self.inner.list_models()

    async def model_inventory(self) -> dict[str, dict]:
        """name -> {"digest", "details"}; backends without digests report names only."""
        inventory = getattr(self.inner, "model_inventory", None)
        if inventory is not None:
            return await inventory()
        return {name: {} for name in await self.inner.list_models()}

    async def health(self) -> bool:
        return await self.inner.health()

    async def pull(self, model: str) -> str:
        return await self.inner.pull(model)


# ---------- Selection ----------

BACKENDS = {"ollama": OllamaBackend, "openai_compatible": OpenAICompatibleBackend}
_override: Backend | None = None


def set_backend(backend: Backend | None) -> None:
    """Install a custom backend (tests, the browser demo). None restores the configured one."""
    global _override
    _override = backend


def get_backend() -> Instrumented:
    if _override is not None:
        return Instrumented(_override)
    try:
        return Instrumented(BACKENDS[settings.BACKEND]())
    except KeyError:
        raise ValueError(f"BACKEND must be one of {sorted(BACKENDS)}, got {settings.BACKEND!r}") from None
