# Corey Mathie, 2026
"""
Load-test the gateway's OpenAI-compatible chat endpoint.

    python scripts/loadtest.py --key sk-local-... --model qwen2.5:0.5b --concurrency 1,4,8 --requests 32

For each concurrency level it sends `--requests` chat completions through the
gateway (streaming by default, with stream_options.include_usage so token
counts come from the inference server) and reports:

- latency p50 / p95 / p99 (request start to the last byte)
- time to first token (TTFT) p50 / p95 / p99 (streaming only)
- requests per second and error rate
- aggregate output tokens/s (all completion tokens / wall time for the level)
- median per-request decode tokens/s (completion tokens / time after the first token)

Raise GATEWAY_RATE_LIMIT_PER_MIN on the gateway first, or 429s will show up as errors.
Results print as a Markdown table (paste into docs/benchmarks.md); --json writes raw numbers.
Only needs httpx.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
from dataclasses import dataclass

import httpx

DEFAULT_PROMPT = "Write three sentences about why audit logs matter for regulated industries."


@dataclass
class Sample:
    ok: bool
    status: int
    latency: float
    ttft: float | None = None
    completion_tokens: int = 0
    error: str = ""


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (same as numpy's default). None for an empty list."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


async def one_request(
    client: httpx.AsyncClient, model: str | None, prompt: str, max_tokens: int, stream: bool
) -> Sample:
    body: dict = {"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "stream": stream}
    if model:
        body["model"] = model
    if stream:
        body["stream_options"] = {"include_usage": True}
    start = time.perf_counter()
    try:
        if not stream:
            r = await client.post("/v1/chat/completions", json=body)
            latency = time.perf_counter() - start
            if r.status_code != 200:
                return Sample(False, r.status_code, latency, error=r.text[:200])
            tokens = (r.json().get("usage") or {}).get("completion_tokens", 0)
            return Sample(True, 200, latency, completion_tokens=tokens)

        ttft = None
        tokens = 0
        chunks = 0
        async with client.stream("POST", "/v1/chat/completions", json=body) as r:
            if r.status_code != 200:
                await r.aread()
                return Sample(False, r.status_code, time.perf_counter() - start, error=r.text[:200])
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    tokens = chunk["usage"].get("completion_tokens", 0)
                if any((c.get("delta") or {}).get("content") for c in chunk.get("choices") or []):
                    chunks += 1
                    if ttft is None:
                        ttft = time.perf_counter() - start
        latency = time.perf_counter() - start
        return Sample(True, 200, latency, ttft=ttft, completion_tokens=tokens or chunks)
    except httpx.HTTPError as e:
        return Sample(False, 0, time.perf_counter() - start, error=type(e).__name__)


async def run_level(
    client: httpx.AsyncClient,
    concurrency: int,
    total: int,
    model: str | None,
    prompt: str,
    max_tokens: int,
    stream: bool,
) -> dict:
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(total):
        queue.put_nowait(i)
    samples: list[Sample] = []

    async def worker():
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            samples.append(await one_request(client, model, prompt, max_tokens, stream))

    start = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    wall = time.perf_counter() - start
    return summarize(samples, wall, concurrency)


def summarize(samples: list[Sample], wall: float, concurrency: int) -> dict:
    ok = [s for s in samples if s.ok]
    lat = [s.latency for s in ok]
    ttft = [s.ttft for s in ok if s.ttft is not None]
    decode = [
        s.completion_tokens / (s.latency - s.ttft)
        for s in ok
        if s.ttft is not None and s.completion_tokens > 1 and s.latency > s.ttft
    ]
    errors: dict[str, int] = {}
    for s in samples:
        if not s.ok:
            label = str(s.status) if s.status else s.error
            errors[label] = errors.get(label, 0) + 1
    return {
        "concurrency": concurrency,
        "requests": len(samples),
        "errors": len(samples) - len(ok),
        "error_rate": (len(samples) - len(ok)) / len(samples) if samples else 0.0,
        "error_breakdown": errors,
        "wall_s": wall,
        "rps": len(ok) / wall if wall else 0.0,
        "latency_p50": percentile(lat, 50),
        "latency_p95": percentile(lat, 95),
        "latency_p99": percentile(lat, 99),
        "ttft_p50": percentile(ttft, 50),
        "ttft_p95": percentile(ttft, 95),
        "ttft_p99": percentile(ttft, 99),
        "output_tokens": sum(s.completion_tokens for s in ok),
        "output_tok_s": sum(s.completion_tokens for s in ok) / wall if wall else 0.0,
        "decode_tok_s_median": percentile(decode, 50),
    }


def _fmt(v: float | None, unit: str = "") -> str:
    if v is None:
        return "n/a"
    if unit == "ms":
        return f"{v * 1000:.1f}"
    return f"{v:.2f}"


def markdown(results: list[dict]) -> str:
    head = (
        "| Concurrency | Requests | Error rate | RPS | Latency p50 / p95 / p99 (ms) | TTFT p50 / p95 / p99 (ms) "
        "| Output tok/s (aggregate) | Decode tok/s per request (median) |\n"
        "|---:|---:|---:|---:|---|---|---:|---:|\n"
    )
    rows = []
    for r in results:
        lat = " / ".join(_fmt(r[f"latency_p{p}"], "ms") for p in (50, 95, 99))
        ttft = " / ".join(_fmt(r[f"ttft_p{p}"], "ms") for p in (50, 95, 99))
        rows.append(
            f"| {r['concurrency']} | {r['requests']} | {r['error_rate'] * 100:.1f}% | {r['rps']:.2f} | {lat} | {ttft} "
            f"| {_fmt(r['output_tok_s'])} | {_fmt(r['decode_tok_s_median'])} |"
        )
    return head + "\n".join(rows)


async def run(
    url: str,
    key: str,
    levels: list[int],
    requests: int,
    model: str | None = None,
    prompt: str = DEFAULT_PROMPT,
    max_tokens: int = 128,
    stream: bool = True,
    warmup: int = 1,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict]:
    limits = httpx.Limits(max_connections=max(levels) + 4, max_keepalive_connections=max(levels) + 4)
    async with httpx.AsyncClient(
        base_url=url,
        headers={"Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(600.0, connect=10.0),
        limits=limits,
        transport=transport,
    ) as client:
        for _ in range(warmup):  # loads the model into memory; not counted
            await one_request(client, model, prompt, max_tokens, stream)
        return [await run_level(client, c, requests, model, prompt, max_tokens, stream) for c in levels]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--key", default=os.environ.get("GATEWAY_KEY", ""), help="any gateway API key")
    ap.add_argument("--model", default=None, help="defaults to the gateway's GATEWAY_DEFAULT_MODEL")
    ap.add_argument("--concurrency", default="1,8,32", help="comma-separated levels, e.g. 1,4,8")
    ap.add_argument("--requests", type=int, default=32, help="requests per level")
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--no-stream", action="store_true", help="non-streaming requests (no TTFT)")
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--json", type=str, default="", help="also write raw results to this file")
    args = ap.parse_args(argv)
    if not args.key:
        print("pass --key or set GATEWAY_KEY", file=sys.stderr)
        return 2
    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    results = asyncio.run(
        run(
            args.url,
            args.key,
            levels,
            args.requests,
            args.model,
            args.prompt,
            args.max_tokens,
            not args.no_stream,
            args.warmup,
        )
    )
    meta = {
        "url": args.url,
        "model": args.model or "(gateway default)",
        "stream": not args.no_stream,
        "max_tokens": args.max_tokens,
        "requests_per_level": args.requests,
        "client_host": f"{platform.system()} {platform.machine()}, {os.cpu_count()} logical CPUs",
        "python": platform.python_version(),
    }
    print(f"\n{json.dumps(meta)}\n")
    print(markdown(results))
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"meta": meta, "results": results}, f, indent=2)
    return 1 if any(r["errors"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
