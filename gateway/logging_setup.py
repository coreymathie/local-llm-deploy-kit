# Corey Mathie, 2026
from __future__ import annotations

import logging

from . import audit
from .config import settings
from .redact import redact


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def log_prompt(key_label: str, model: str, prompt: str, response: str, tokens: int) -> None:
    """Record a prompt/response pair in the tamper-evident audit log, if enabled."""
    if not settings.GATEWAY_LOG_PROMPTS:
        return
    payload = {"key": key_label, "model": model, "tokens": tokens}
    if settings.GATEWAY_REDACT_PROMPTS:
        payload["prompt"], p_counts = redact(prompt)
        payload["response"], r_counts = redact(response)
        payload["pii_counts"] = {k: p_counts.get(k, 0) + r_counts.get(k, 0) for k in {*p_counts, *r_counts}}
    else:
        payload["prompt"], payload["response"] = prompt, response
    audit.append("completion", payload)
