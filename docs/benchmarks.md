# Benchmarks

Two kinds of numbers belong here, and they must not be mixed up:

1. **Model serving numbers** (time to first token, tokens/s under load) for a real model on real hardware.
   These depend almost entirely on the inference server, the model, the quantization and the GPU.
   **None have been published yet.** Use the method below and the results template to add them.
2. **Gateway overhead**: what the gateway itself adds (auth, rate limiting, SQLite usage accounting,
   SSE re-encoding, metrics) when the upstream answers instantly. These were measured and are below.

## Method

`scripts/loadtest.py` (async httpx) sends `--requests` chat completions per concurrency level through the
gateway's `/v1/chat/completions` and reports:

| Metric | Definition |
|---|---|
| Latency p50 / p95 / p99 | Request start to the last byte of the response (linear-interpolated percentiles) |
| TTFT p50 / p95 / p99 | Request start to the first streamed content chunk (streaming only) |
| RPS | Successful requests / wall time of the level |
| Error rate | Non-200 responses and transport errors / requests (429s count as errors) |
| Output tok/s (aggregate) | Sum of `completion_tokens` / wall time; tokens come from the server's `usage` via `stream_options.include_usage` |
| Decode tok/s per request | Median of `completion_tokens / (latency − TTFT)` |

```bash
# 1. Raise the per-key rate limit for the run, or 429s will dominate
GATEWAY_RATE_LIMIT_PER_MIN=1000000 uvicorn gateway.main:app --port 8080

# 2. Run three levels; one warm-up request (model load) is not counted
python scripts/loadtest.py --key sk-local-... --model qwen2.5:0.5b \
    --concurrency 1,8,32 --requests 64 --max-tokens 128 --json results.json
```

Report with every table: CPU/GPU model, RAM/VRAM, OS, inference server and version, model and
quantization, `--max-tokens`, prompt, requests per level, and whether client and server share a host.

### Results template (model serving)

| Hardware | Backend | Model | Concurrency | Requests | Error rate | TTFT p50 / p95 / p99 (ms) | Latency p50 / p95 / p99 (ms) | Output tok/s (aggregate) | Decode tok/s per request |
|---|---|---|---:|---:|---:|---|---|---:|---:|
| _not measured yet_ | | | 1 | | | | | | |
| | | | 8 | | | | | | |
| | | | 32 | | | | | | |

A CPU run with Ollama and `qwen2.5:0.5b` was planned for v0.5.0 but could not be done in the build
environment: its network policy blocked the Ollama model registry and Hugging Face, so no model weights
could be downloaded. No model numbers are claimed anywhere in this repository.

## Gateway overhead (measured)

**Setup.** `scripts/mock_openai_server.py` (an OpenAI-compatible stub that streams 64 fixed tokens
with no delay; it is not a model) as the upstream, the gateway with `BACKEND=openai_compatible`,
and `scripts/loadtest.py`, all on one host:

- Host: Linux VM (kernel 6.18), 2 vCPUs (Intel Xeon @ 2.80 GHz), 7 GiB RAM, no GPU
- Python 3.13, uvicorn 0.53 (one worker), FastAPI 0.142, httpx 0.28
- Streaming, `max_tokens=64`, 300 requests per level after 5 warm-up requests, 0 errors in all runs
- Client, gateway and stub share the 2 vCPUs, so these are conservative

| Path | Concurrency | RPS | Latency p50 / p95 / p99 (ms) | TTFT p50 / p95 (ms) |
|---|---:|---:|---|---|
| Client → stub (no gateway) | 1 | 260 | 3.5 / 5.6 / 6.3 | 1.9 / 3.3 |
| Client → gateway → stub | 1 | 102 | 9.2 / 13.6 / 16.4 | 6.0 / 9.6 |
| Client → stub (no gateway) | 8 | 306 | 25.5 / 33.2 / 37.1 | 17.9 / 24.7 |
| Client → gateway → stub | 8 | 112 | 68.2 / 96.1 / 104.4 | 50.3 / 75.3 |
| Client → stub (no gateway) | 32 | 364 | 86.5 / 98.2 / 108.1 | 65.4 / 79.5 |
| Client → gateway → stub | 32 | 106 | 292.6 / 330.6 / 336.2 | 272.5 / 310.0 |

A second gateway run gave 92 / 113 / 112 RPS at concurrency 1 / 8 / 32 (p50 10.1 / 67.5 / 288.7 ms).

**Reading it.** At concurrency 1 the gateway adds roughly 6 ms at p50 to a 64-token streamed request.
A single uvicorn worker on this 2-vCPU host saturates at about 100–110 requests/s, after which extra
concurrency turns into queueing. These numbers bound the gateway only; whether the gateway or the
inference server is the limit for a given deployment has to be measured with a real model (method
above). Scaling the gateway past one process needs a shared rate-limit store; see Failure modes in
the README.

**Bottleneck found and fixed during this measurement.** Up to v0.4 the gateway built a new
`httpx.AsyncClient` for every upstream call. Creating a client builds an SSL context and loads the CA
bundle, which took about 48 ms of blocking CPU on this host. Measured with the same setup
(200 requests per level):

| Version | Concurrency 1: RPS, p50 | Concurrency 8: RPS, p50 | Concurrency 32: RPS, p50 |
|---|---|---|---|
| Client per request (0.4 behavior) | 16.7 RPS, 57.9 ms | 16.1 RPS, 495 ms | 17.0 RPS, 1877 ms |
| Pooled client per event loop (0.5) | 102 RPS, 9.2 ms | 112 RPS, 68.2 ms | 106 RPS, 293 ms |

The fix is in `gateway/backends.py` (`_client()`), with a regression test
(`test_upstream_http_client_is_pooled_not_rebuilt_per_request`). In 0.5 the gateway also stops logging
every upstream request at INFO.

**Reproduce.**

```bash
MOCK_TOKENS=64 uvicorn scripts.mock_openai_server:app --port 8001 &
BACKEND=openai_compatible OPENAI_COMPAT_BASE_URL=http://127.0.0.1:8001/v1 GATEWAY_DEFAULT_MODEL=mock-model \
  GATEWAY_RATE_LIMIT_PER_MIN=1000000 GATEWAY_ADMIN_BOOTSTRAP_KEY=sk-local-bench-admin \
  uvicorn gateway.main:app --port 8080 &
python scripts/loadtest.py --key sk-local-bench-admin --model mock-model --concurrency 1,8,32 --requests 300 --max-tokens 64 --warmup 5
python scripts/loadtest.py --url http://127.0.0.1:8001 --key x --model mock-model --concurrency 1,8,32 --requests 300 --max-tokens 64 --warmup 5  # baseline
```

## Retrieval quality (measured, golden set)

`python scripts/rag_eval.py` (runs in about 1.5 s on the build host; CI runs it with `--check`). Same
caveats as [ADR 0006](adr/0006-hybrid-retrieval-and-rag-evals.md): demo hashed bag-of-words embedder (not
a neural model), extractive answerer (no LLM), a small golden set written by one author. A regression
baseline for the retrieval pipeline, not a statement about answer quality in production.

Golden set: 12 fictional documents (2 restricted), 43 questions, k=4. Embedder: demo hashed bag-of-words (512-d), not a neural model. Answers: demo extractive (no LLM).

| Configuration | recall@1 | recall@4 | MRR | citation accuracy | answer contains | ACL leaks |
|---|---:|---:|---:|---:|---:|---:|
| `vector+none` | 0.907 | 0.977 | 0.942 | 0.907 | 0.954 | 0 |
| `bm25+none` | 1.000 | 1.000 | 1.000 | 0.907 | 0.977 | 0 |
| `hybrid+none` | 0.954 | 1.000 | 0.977 | 0.884 | 0.977 | 0 |
| `hybrid+lexical` | 1.000 | 1.000 | 1.000 | 0.907 | 0.954 | 0 |