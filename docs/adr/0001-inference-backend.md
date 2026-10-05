# ADR 0001: Ollama by default, OpenAI-compatible servers (vLLM and others) as a pluggable backend

Status: Accepted, v0.5.0

## Context

The gateway exists so teams can use a local model without sending data to a public API. Two very
different deployment shapes ask for it:

- **A laptop, workstation or small on-prem box**, often without a datacenter GPU. Ollama fits: one
  installer per OS, quantized GGUF models that run on CPU, Apple Silicon or consumer GPUs, model
  downloads through its own API (`/api/pull`), and it serves several models from one process.
- **A shared GPU server** with many concurrent users. Engines built for throughput (vLLM, SGLang,
  Hugging Face TGI, NVIDIA NIM) batch concurrent requests on the GPU. They all expose the OpenAI wire
  format (`/v1/chat/completions`, `/v1/models`, and for embedding models `/v1/embeddings`).

Up to v0.4 the gateway called Ollama's native API directly from its routes, so the second shape
wasn't possible without a fork.

## Decision

- Add a `Backend` interface in `gateway/backends.py` with `chat`, `stream_chat`, `embed`,
  `list_models`, `health` and `pull`. Routes and RAG call the interface, never a URL.
- `BACKEND=ollama` (default) keeps v0.4 behavior byte-for-byte at the API: same request mapping
  (`options.temperature`, `options.num_predict`), same response ids, same error contract.
- `BACKEND=openai_compatible` talks to `OPENAI_COMPAT_BASE_URL` (including `/v1`) with an optional
  `OPENAI_COMPAT_API_KEY` sent upstream as a bearer token. Streaming asks for
  `stream_options.include_usage` so token accounting comes from the server, not from counting chunks.
- The gateway's own API does not change with the backend. Client keys never go upstream.
- `get_backend()` wraps every backend in `Instrumented`, so metrics and GenAI spans are identical for
  both.
- Model pulls stay Ollama-only; with another backend `/v1/pull` returns **501** because the inference
  server owns its model lifecycle.
- One pooled HTTP client per event loop for upstream calls (see `docs/benchmarks.md`: creating a client
  per request capped throughput at about 17 requests/s on the test host).

## Consequences

- Good: the same keys, audit, redaction, RAG and metrics in front of a laptop or a GPU server. A
  custom backend is a class with six methods (`set_backend()` is used by tests and the browser demo).
- Good: upstream failures surface before a stream starts. The gateway pulls the first streamed event
  before answering, so an unreachable or failing backend returns 502/4xx instead of a 200 stream that
  breaks half-way.
- Cost: chat and embeddings go to the same base URL. vLLM serves one model per process, so document
  Q&A on vLLM needs an embedding endpoint at that URL (a router in front of two vLLM processes, or
  keep `BACKEND=ollama`). A separate `EMBEDDINGS_BASE_URL` is **not implemented** (roadmap).
- Cost: compatibility is tested against mocked HTTP that follows the OpenAI wire format, not against
  live vLLM/SGLang/TGI/NIM servers. TGI's embeddings come from a separate service (TEI).
- The Docker Compose `vllm` profile is an example for an NVIDIA host; it is validated with
  `docker compose config` and a test, not run on a GPU in CI.

## Alternatives considered

- **Only Ollama.** Simplest, but rules out shared GPU serving.
- **Only an OpenAI-compatible client, and point it at Ollama's `/v1`.** Ollama does offer an
  OpenAI-compatible endpoint, but the native API gives model pulls and is what v0.4 users already
  run; keeping it avoids a behavior change for them.
- **LiteLLM or another proxy library.** Broad provider coverage, but a large dependency for two wire
  formats, and most of its providers are hosted APIs this project exists to avoid.

## Where it lives

- Code: `gateway/backends.py`, `gateway/main.py` (routes), `gateway/config.py`, `docker-compose.yml` (`vllm` profile)
- Tests: `tests/test_backends.py` (both backends: chat, streaming with usage, embeddings order, models,
  upstream auth header, 501 pull, error mapping before streaming, client pooling),
  `tests/test_deploy_config.py`, `tests/test_loadtest.py` (gateway → OpenAI-compatible mock end to end)
