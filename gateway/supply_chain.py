# Corey Mathie, 2026
"""
Model supply chain: pinned model digests, verification against what the backend actually serves, and a
policy for unpinned or mismatched models.

Lock file (GATEWAY_MODEL_LOCK_FILE, JSON; see models.lock.example.json):

    {"version": 1, "models": [
      {"name": "llama3.1:8b", "backend": "ollama", "digest": "sha256:<64 hex>",
       "source": "https://ollama.com/library/llama3.1", "license": "Llama 3.1 Community License"},
      {"name": "Qwen/Qwen2.5-7B-Instruct", "backend": "openai_compatible",
       "files": [{"path": "/models/qwen2.5-7b/model-00001-of-00004.safetensors", "sha256": "<64 hex>"}]}
    ]}

Verification:
  ollama              the digest Ollama reports in /api/tags for that name must equal the pinned digest
                      (a re-pull that changes the model changes the digest)
  openai_compatible   the server only reports names, so the pinned weight files (a path the gateway can
                      read, e.g. a shared volume) are hashed with SHA-256; no files pinned = unverifiable

Statuses per model: verified, mismatch, missing (pinned, not served), unpinned (served, not pinned),
unverifiable (pinned without anything to check), error.

Policy (GATEWAY_MODEL_POLICY), applied to chat, embeddings, document Q&A and ingestion:
  off      no checks
  warn     requests proceed; problems are logged and counted, and the result carries the status
  enforce  only models pinned in the lock file whose last verification is "verified" are served (403)

Ollama digests are re-checked lazily every GATEWAY_MODEL_VERIFY_INTERVAL_SECONDS (one /api/tags call);
file hashes only at startup, after a model pull, and on POST /admin/models/verify (they can be GBs).
This verifies *integrity against a pin you chose*, not the model's provenance: pin digests you obtained
from a trusted source (`scripts/pin_models.py` pins what is served now: trust on first use).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from .backends import BackendHTTPError
from .config import settings

try:
    from . import metrics as _metrics
except ImportError:  # prometheus_client not installed
    _metrics = None

log = logging.getLogger("gateway.supply_chain")

HEX64 = re.compile(r"^[0-9a-f]{64}$")
STATUSES = ("verified", "mismatch", "missing", "unpinned", "unverifiable", "error")


class LockError(ValueError):
    """The lock file is malformed."""


class ModelPolicyError(Exception):
    """The policy refuses this model (HTTP 403)."""


@dataclass
class Pin:
    name: str
    backend: str = "ollama"
    digest: str = ""  # sha256 hex (an optional "sha256:" prefix is accepted in the file)
    files: list[dict] = field(default_factory=list)  # [{"path", "sha256"}]
    source: str = ""
    license: str = ""
    purpose: str = ""


@dataclass
class ModelStatus:
    name: str
    status: str
    detail: str = ""
    expected: str = ""
    actual: str = ""
    pinned: bool = False
    backend: str = ""
    info: dict = field(default_factory=dict)  # e.g. Ollama's family / parameter size / quantization


def _norm_digest(value: str) -> str:
    value = (value or "").strip().lower()
    return value.split(":", 1)[1] if value.startswith("sha256:") else value


def load_lock(path: str | Path | None = None) -> list[Pin]:
    path = path or settings.GATEWAY_MODEL_LOCK_FILE
    if not path:
        return []
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise LockError(f"can't read model lock file {path}: {e}") from e
    return parse_lock(doc)


def parse_lock(doc) -> list[Pin]:
    """Validate a lock document (already parsed JSON). Raises LockError on the first problem."""
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("models"), list):
        raise LockError('model lock file needs {"version": 1, "models": [...]}')
    pins, seen = [], set()
    for m in doc["models"]:
        if not isinstance(m, dict):
            raise LockError("every entry under models must be an object")
        pin = Pin(**{k: v for k, v in m.items() if k in Pin.__dataclass_fields__})
        if not pin.name or pin.name in seen:
            raise LockError(f"model {pin.name!r} is missing a name or listed twice")
        seen.add(pin.name)
        if pin.backend not in ("ollama", "openai_compatible"):
            raise LockError(f"{pin.name}: unknown backend {pin.backend!r}")
        pin.digest = _norm_digest(pin.digest)
        if pin.digest and not HEX64.match(pin.digest):
            raise LockError(f"{pin.name}: digest must be a SHA-256 hex string")
        if not isinstance(pin.files, list):
            raise LockError(f"{pin.name}: files must be a list")
        for f in pin.files:
            if not isinstance(f, dict) or not f.get("path") or not HEX64.match(_norm_digest(f.get("sha256", ""))):
                raise LockError(f"{pin.name}: every pinned file needs a path and a sha256")
        if not pin.digest and not pin.files:
            log.warning("model %s is pinned without a digest or files: it can't be verified", pin.name)
        pins.append(pin)
    return pins


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


async def served_models(backend) -> dict[str, dict]:
    """name -> {"digest", "details"} for what the backend serves now (digests only from Ollama)."""
    inventory = getattr(backend, "model_inventory", None)
    if inventory is not None:
        return await inventory()
    return {name: {} for name in await backend.list_models()}


async def verify(backend, pins: list[Pin] | None = None) -> dict:
    pins = load_lock() if pins is None else pins
    by_name = {p.name: p for p in pins}
    results: dict[str, ModelStatus] = {}
    try:
        served = await served_models(backend)
    except (httpx.HTTPError, BackendHTTPError, OSError, ValueError, KeyError) as e:  # reported, not raised
        return _remember(
            {
                "ok": False,
                "checked_at": time.time(),
                "error": f"backend unavailable: {e.__class__.__name__}",
                "models": {},
                "backend": backend.name,
            }
        )
    for pin in pins:
        if pin.backend != backend.name:
            continue
        st = ModelStatus(pin.name, "unverifiable", pinned=True, backend=pin.backend, expected=pin.digest)
        if pin.name not in served:
            st.status, st.detail = "missing", "pinned but not served by the backend"
        elif pin.backend == "ollama" and pin.digest:
            st.actual = _norm_digest(served[pin.name].get("digest", ""))
            st.info = served[pin.name].get("details") or {}
            st.status = "verified" if st.actual == pin.digest else "mismatch"
            st.detail = "" if st.status == "verified" else "served digest differs from the pin"
        elif pin.files:
            st.status, st.detail = await asyncio.to_thread(_verify_files, pin)
        else:
            st.detail = "no digest or files pinned"
        results[pin.name] = st
    for name, meta in served.items():
        if name not in by_name:
            results[name] = ModelStatus(
                name,
                "unpinned",
                "served but not in the lock file",
                actual=_norm_digest(meta.get("digest", "")),
                backend=backend.name,
                info=meta.get("details") or {},
            )
    out = {
        "ok": all(r.status == "verified" for r in results.values() if r.pinned),
        "checked_at": time.time(),
        "backend": backend.name,
        "lock_file": settings.GATEWAY_MODEL_LOCK_FILE,
        "policy": settings.GATEWAY_MODEL_POLICY,
        "models": {n: asdict(r) for n, r in sorted(results.items())},
    }
    return _remember(out)


def _verify_files(pin: Pin) -> tuple[str, str]:
    for f in pin.files:
        try:
            actual = sha256_file(f["path"])
        except OSError as e:
            return "error", f"can't read {f['path']}: {e.__class__.__name__}"
        if actual != _norm_digest(f["sha256"]):
            return "mismatch", f"{f['path']} differs from its pinned sha256"
    return "verified", f"{len(pin.files)} file(s) match"


_last: dict | None = None


def _remember(result: dict) -> dict:
    global _last
    _last = result
    return result


def last_result() -> dict | None:
    return _last


def reset() -> None:
    global _last
    _last = None


async def check_model(name: str, backend) -> str | None:
    """Apply GATEWAY_MODEL_POLICY to one model. Returns its status (None when the policy is off).

    Raises ModelPolicyError under enforce. Re-verifies (cheaply, for Ollama) when the last result is stale.
    """
    policy = settings.GATEWAY_MODEL_POLICY
    if policy == "off":
        return None
    result = _last
    stale = result is None or time.time() - result["checked_at"] > settings.GATEWAY_MODEL_VERIFY_INTERVAL_SECONDS
    if stale and (result is None or backend.name == "ollama"):
        result = await verify(backend)
    models = result.get("models", {})
    entry = models.get(name) or models.get(f"{name}:latest")  # Ollama: "llama3.1" means "llama3.1:latest"
    status = "error" if result.get("error") else (entry or {}).get("status", "unpinned")
    if status != "verified":
        action = "refused" if policy == "enforce" else "warned"
        if _metrics is not None:
            _metrics.MODEL_POLICY.labels(name, status, action).inc()
        if policy == "enforce":
            raise ModelPolicyError(f"model {name!r} is not allowed by the model policy ({status})")
        key = (name, status, result["checked_at"])
        if key not in _warned:  # once per model and verification run, not once per request
            _warned.add(key)
            log.warning("model policy: %s is %s (GATEWAY_MODEL_POLICY=warn)", name, status)
    return status


_warned: set = set()
