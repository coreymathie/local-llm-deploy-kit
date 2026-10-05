# Corey Mathie, 2026
"""Prometheus /metrics: route-template and key-name labels, tokens, rejections, protection, Grafana queries."""

import json
import re
from pathlib import Path

import httpx

from gateway import backends, main, metrics
from gateway.config import settings

from .conftest import fake_ollama, new_key

DASHBOARD = Path(__file__).resolve().parent.parent / "deploy" / "grafana-dashboard.json"


def sample(name: str, **labels) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def chat(client, key, **extra):
    body = {"messages": [{"role": "user", "content": "hi"}], **extra}
    return client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})


def test_metrics_endpoint_exposes_prometheus_text(client):
    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "# TYPE gateway_http_requests_total counter" in r.text
    assert f'gateway_info{{backend="ollama",version="{main.__version__}"}} 1.0' in r.text


def test_requests_are_counted_by_route_template_and_key_name(client):
    key = new_key(client, label="metrics-app")
    before = sample("gateway_http_requests_total", method="POST", route="/v1/chat/completions", status="200",
                    key="metrics-app")  # fmt: skip
    assert chat(client, key).status_code == 200
    after = sample("gateway_http_requests_total", method="POST", route="/v1/chat/completions", status="200",
                   key="metrics-app")  # fmt: skip
    assert after == before + 1

    client.get("/v1/collections/payroll-2026/documents", headers={"Authorization": f"Bearer {key}"})
    text = client.get("/metrics").text
    assert key not in text  # the secret never becomes a label value
    assert 'route="/v1/collections/{collection}/documents"' in text and "payroll-2026" not in text  # templated

    # Latency histogram observed for the route
    assert sample("gateway_http_request_duration_seconds_count", method="POST", route="/v1/chat/completions") >= 1


def test_tokens_are_counted_per_key_and_model(client):
    key = new_key(client, label="token-app")
    chat(client, key)
    assert sample("gateway_llm_tokens_total", key="token-app", model="llama3.1:8b", type="prompt") == 7
    assert sample("gateway_llm_tokens_total", key="token-app", model="llama3.1:8b", type="completion") == 5
    assert sample("gateway_llm_request_duration_seconds_count", operation="chat", backend="ollama",
                  model="llama3.1:8b") >= 1  # fmt: skip


def test_rejections_show_up_by_status_with_key_name(client, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_RATE_LIMIT_PER_MIN", 1)
    key = new_key(client, label="noisy-app")
    chat(client, key)
    assert chat(client, key).status_code == 429
    assert sample("gateway_http_requests_total", method="POST", route="/v1/chat/completions", status="429",
                  key="noisy-app") == 1  # fmt: skip
    before = sample("gateway_http_requests_total", method="POST", route="/v1/chat/completions", status="401",
                    key="none")  # fmt: skip
    chat(client, "sk-local-not-a-key")
    assert sample("gateway_http_requests_total", method="POST", route="/v1/chat/completions", status="401",
                  key="none") == before + 1  # fmt: skip


def test_unknown_paths_collapse_to_one_label(client):
    client.get("/wp-admin/../../etc/passwd-12345")
    text = client.get("/metrics").text
    assert "passwd-12345" not in text and 'route="unmatched"' in text


def test_backend_errors_and_ttft_are_recorded(client, monkeypatch):
    key = new_key(client, label="ttft-app")

    def streaming(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat" and json.loads(request.content).get("stream"):
            lines = [{"message": {"content": "hi"}}, {"done": True, "eval_count": 1, "prompt_eval_count": 1}]
            return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())
        return fake_ollama(request)

    transport = httpx.MockTransport(streaming)
    monkeypatch.setattr(backends.httpx, "AsyncClient", lambda **kw: httpx.AsyncClient(transport=transport))
    before = sample("gateway_llm_time_to_first_token_seconds_count", backend="ollama", model="llama3.1:8b")
    chat(client, key, stream=True)
    assert sample("gateway_llm_time_to_first_token_seconds_count", backend="ollama", model="llama3.1:8b") == before + 1

    def down(request):
        raise httpx.ConnectError("refused")

    down_transport = httpx.MockTransport(down)
    monkeypatch.setattr(backends.httpx, "AsyncClient", lambda **kw: httpx.AsyncClient(transport=down_transport))
    before = sample("gateway_backend_errors_total", operation="chat", backend="ollama", reason="ConnectError")
    assert chat(client, key).status_code == 502
    assert sample("gateway_backend_errors_total", operation="chat", backend="ollama", reason="ConnectError") == (
        before + 1
    )


def test_metrics_token_and_disable_switch(client, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_METRICS_TOKEN", "scrape-secret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"}).status_code == 200
    monkeypatch.setattr(settings, "GATEWAY_METRICS_ENABLED", False)
    assert client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"}).status_code == 404


def test_grafana_dashboard_only_queries_metrics_the_gateway_exports(client):
    dash = json.loads(DASHBOARD.read_text())
    exprs = [t["expr"] for p in dash["panels"] for t in p["targets"]]
    used = {m for e in exprs for m in re.findall(r"\b(gateway_[a-z_]+)\b", e)}
    exported = set()
    for line in client.get("/metrics").text.splitlines():
        if line.startswith("# TYPE "):
            name, kind = line.split()[2:4]
            exported.add(name)
            if kind == "histogram":
                exported |= {f"{name}_bucket", f"{name}_sum", f"{name}_count"}
            if kind == "counter":
                exported.add(f"{name}_total" if not name.endswith("_total") else name)
    assert used and used <= exported, used - exported
