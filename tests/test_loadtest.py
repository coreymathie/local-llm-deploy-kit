# Corey Mathie, 2026
"""scripts/loadtest.py and scripts/mock_openai_server.py, run in-process (no network, no model)."""

import asyncio
import json

import httpx
import numpy as np
from fastapi.testclient import TestClient

from gateway import backends, main
from gateway.config import settings
from scripts import loadtest, mock_openai_server

from .conftest import new_key


def test_percentile_matches_numpy():
    xs = [0.9, 0.1, 0.5, 0.3, 0.7, 1.3, 0.2]
    for p in (50, 95, 99):
        assert abs(loadtest.percentile(xs, p) - float(np.percentile(xs, p))) < 1e-12
    assert loadtest.percentile([], 50) is None and loadtest.percentile([2.0], 99) == 2.0


def test_summarize_counts_errors_and_tokens():
    samples = [
        loadtest.Sample(True, 200, latency=1.0, ttft=0.2, completion_tokens=40),
        loadtest.Sample(True, 200, latency=2.0, ttft=0.4, completion_tokens=80),
        loadtest.Sample(False, 429, latency=0.01, error="rate limit"),
    ]
    out = loadtest.summarize(samples, wall=2.0, concurrency=2)
    assert out["requests"] == 3 and out["errors"] == 1 and out["error_breakdown"] == {"429": 1}
    assert abs(out["error_rate"] - 1 / 3) < 1e-9
    assert out["output_tok_s"] == 60.0  # 120 tokens / 2 s wall
    assert out["decode_tok_s_median"] == 50.0  # 40/0.8 and 80/1.6
    table = loadtest.markdown([out])
    assert table.startswith("| Concurrency") and "| 2 | 3 | 33.3% |" in table


def test_mock_server_speaks_openai():
    c = TestClient(mock_openai_server.app)
    assert c.get("/v1/models").json()["data"][0]["id"] == "mock-model"
    r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "a b c"}], "max_tokens": 3})
    assert r.json()["usage"] == {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}
    body = {"messages": [], "max_tokens": 2, "stream": True, "stream_options": {"include_usage": True}}
    lines = [ln[6:] for ln in c.post("/v1/chat/completions", json=body).text.splitlines() if ln.startswith("data: ")]
    assert lines[-1] == "[DONE]" and json.loads(lines[-2])["usage"]["completion_tokens"] == 2
    emb = c.post("/v1/embeddings", json={"input": ["x", "y"]}).json()["data"]
    assert [d["index"] for d in emb] == [0, 1] and len(emb[0]["embedding"]) == 16


def test_loadtest_end_to_end_through_gateway_and_openai_compatible_backend(client, monkeypatch):
    """loadtest -> gateway (in-process) -> BACKEND=openai_compatible -> mock server (in-process)."""
    monkeypatch.setattr(settings, "BACKEND", "openai_compatible")
    monkeypatch.setattr(settings, "OPENAI_COMPAT_BASE_URL", "http://mock/v1")
    monkeypatch.setattr(settings, "GATEWAY_RATE_LIMIT_PER_MIN", 10_000)
    monkeypatch.setenv("MOCK_TOKENS", "16")
    upstream = httpx.ASGITransport(app=mock_openai_server.app)
    monkeypatch.setattr(
        backends.httpx, "AsyncClient", lambda **kw: httpx.AsyncClient(transport=upstream, headers=kw.get("headers"))
    )
    key = new_key(client, label="bench")

    results = asyncio.run(
        loadtest.run(
            "http://gateway",
            key,
            levels=[1, 4],
            requests=8,
            max_tokens=16,
            transport=httpx.ASGITransport(app=main.app),
        )
    )
    assert [r["concurrency"] for r in results] == [1, 4]
    for r in results:
        assert r["requests"] == 8 and r["errors"] == 0
        assert r["output_tokens"] == 8 * 16  # exact counts from the usage chunk, not chunk counting
        assert r["ttft_p50"] is not None and r["ttft_p50"] <= r["latency_p50"]

    bad = asyncio.run(
        loadtest.run("http://gateway", "sk-wrong", [2], 4, warmup=0, transport=httpx.ASGITransport(app=main.app))
    )
    assert bad[0]["error_rate"] == 1.0 and bad[0]["error_breakdown"] == {"401": 4}


def test_cli_requires_a_key(capsys):
    assert loadtest.main(["--key", ""]) == 2
    assert "GATEWAY_KEY" in capsys.readouterr().err
