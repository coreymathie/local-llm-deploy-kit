# Corey Mathie, 2026
"""
Prometheus metrics for the gateway, exposed at GET /metrics.

Labels never carry secrets: the `key` label is the API key's human label
("hr-bot"), never the key value. Unmatched paths are collapsed into one
"unmatched" route so scanners can't explode label cardinality. Rejected
requests are visible by status: 401 (missing/invalid/revoked key), 403
(admin only) and 429 (rate limited, labelled with the key that hit it).
"""

from __future__ import annotations

import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

REGISTRY = CollectorRegistry(auto_describe=True)

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)

HTTP_REQUESTS = Counter(
    "gateway_http_requests_total",
    "HTTP requests handled by the gateway",
    ["method", "route", "status", "key"],
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "gateway_http_request_duration_seconds",
    "End-to-end request latency, including the full body of streamed responses",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "gateway_llm_tokens_total",
    "Tokens reported by the inference backend",
    ["key", "model", "type"],  # type: prompt | completion
    registry=REGISTRY,
)
LLM_LATENCY = Histogram(
    "gateway_llm_request_duration_seconds",
    "Time spent waiting on the inference backend",
    ["operation", "backend", "model"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
LLM_TTFT = Histogram(
    "gateway_llm_time_to_first_token_seconds",
    "Time from request to the first streamed token",
    ["backend", "model"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
BACKEND_ERRORS = Counter(
    "gateway_backend_errors_total",
    "Failed calls to the inference backend",
    ["operation", "backend", "reason"],
    registry=REGISTRY,
)
INFO = Gauge("gateway_info", "Gateway build and backend", ["version", "backend"], registry=REGISTRY)
MODEL_POLICY = Counter(
    "gateway_model_policy_decisions_total",
    "Model policy checks that found a model not verified against the lock file",
    ["model", "status", "action"],  # action: warned | refused
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def record_tokens(key: str, model: str, prompt: int, completion: int) -> None:
    if prompt:
        LLM_TOKENS.labels(key, model, "prompt").inc(prompt)
    if completion:
        LLM_TOKENS.labels(key, model, "completion").inc(completion)


class MetricsMiddleware:
    """Pure ASGI middleware: counts and times every HTTP request by route template and key label."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def _send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            key = (scope.get("state") or {}).get("key_label", "none")
            HTTP_REQUESTS.labels(scope["method"], route, str(status), key).inc()
            HTTP_LATENCY.labels(scope["method"], route).observe(time.perf_counter() - start)
