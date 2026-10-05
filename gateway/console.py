# Corey Mathie, 2026
"""
Read models and runtime policy for the product console (demo/ served at /console).

Shared by the FastAPI routes in main.py (live mode) and by demo/engine.py (the same console running
in the browser through Pyodide), so both modes compute overview numbers, access matrices and policy
validation with the same code. Imports nothing server-only: no FastAPI, no HTTP client.

- overview()         counters from the key store, the audit log and the document store
- audit_rows()       audit entries with their line numbers (the console's log explorer)
- access_matrix()    which caller can read which document of a collection, and why
- current_policy() / validate_policy() / apply_policy()
                     the runtime-editable subset of settings (roles from IdP groups, default collection
                     access, retrieval, rate limit, model policy), validated with the Settings field types
                     and identity.check_role. Applied in memory only: a restart reads the environment again.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from . import audit, identity, rag, store
from .config import Settings, settings

# The settings an admin may change at runtime from the console. Everything else (secrets, URLs, paths,
# encryption, OIDC issuer/audience) stays environment-only.
POLICY_FIELDS: dict[str, str] = {
    "GATEWAY_OIDC_GROUP_ROLES": "IdP group -> gateway roles (admin, user, reader:<collection>, reader:*)",
    "GATEWAY_COLLECTION_DEFAULT_ACCESS": "collections without an ACL: open (user role may read) or restricted",
    "GATEWAY_RETRIEVAL_MODE": "vector | bm25 | hybrid",
    "GATEWAY_RERANKER": "none | lexical | cross_encoder (needs GATEWAY_CROSS_ENCODER_MODEL)",
    "GATEWAY_RRF_K": "Reciprocal Rank Fusion constant (1-1000)",
    "GATEWAY_RATE_LIMIT_PER_MIN": "requests per caller per minute (1-100000)",
    "GATEWAY_MODEL_POLICY": "off | warn | enforce (model lock file)",
}
_RANGES = {"GATEWAY_RRF_K": (1, 1000), "GATEWAY_RATE_LIMIT_PER_MIN": (1, 100_000)}


class PolicyError(ValueError):
    def __init__(self, errors: list[dict]):
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))
        self.errors = errors


def current_policy() -> dict:
    return {name: getattr(settings, name) for name in POLICY_FIELDS}


def validate_policy(doc: Any) -> dict:
    """Return the normalized policy, or raise PolicyError listing every problem (field + message)."""
    if not isinstance(doc, dict):
        raise PolicyError([{"field": "(document)", "message": "the policy must be a JSON object"}])
    errors: list[dict] = []
    out: dict[str, Any] = {}
    for name in doc:
        if name not in POLICY_FIELDS:
            errors.append({"field": name, "message": "not a runtime-editable setting"})
    for name in POLICY_FIELDS:
        if name not in doc:
            out[name] = getattr(settings, name)  # omitted fields keep their current value
            continue
        try:
            value = TypeAdapter(Settings.model_fields[name].annotation).validate_python(doc[name])
        except ValidationError as e:
            msg = e.errors()[0].get("msg", "invalid value")
            errors.append({"field": name, "message": msg})
            continue
        if name in _RANGES and not _RANGES[name][0] <= value <= _RANGES[name][1]:
            lo, hi = _RANGES[name]
            errors.append({"field": name, "message": f"must be between {lo} and {hi}"})
            continue
        if name == "GATEWAY_OIDC_GROUP_ROLES":
            for group, roles in value.items():
                if not group.strip():
                    errors.append({"field": name, "message": "group names can't be empty"})
                for role in roles:
                    try:
                        identity.check_role(role)
                    except ValueError as e:
                        errors.append({"field": f"{name}.{group}", "message": str(e)})
        if name == "GATEWAY_RERANKER" and value == "cross_encoder" and not settings.GATEWAY_CROSS_ENCODER_MODEL:
            errors.append({"field": name, "message": "cross_encoder needs GATEWAY_CROSS_ENCODER_MODEL (environment)"})
        out[name] = value
    if errors:
        raise PolicyError(errors)
    return out


def apply_policy(doc: Any) -> tuple[dict, dict]:
    """Validate, then set the values on the live settings object. Returns (before, after) of changed fields."""
    new = validate_policy(doc)
    before = current_policy()
    changed = {k: v for k, v in new.items() if before[k] != v}
    for name, value in changed.items():
        setattr(settings, name, value)
    return {k: before[k] for k in changed}, changed


# ---------- Audit log as rows ----------


def audit_rows(limit: int | None = None) -> list[dict]:
    """Entries oldest first, each with its 1-based line number; unparseable lines are kept as such."""
    path = audit._path()
    if not path.exists():
        return []
    rows = []
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = {"event": "(unparseable)", "payload": {"raw": line[:200]}}
        rows.append({"line": i, **entry})
    return rows[-limit:] if limit else rows


def _actor(event: str, payload: dict) -> str:
    """Who acted: the caller for questions and completions, the admin for admin actions."""
    if event.startswith("document_question") or event == "completion":
        return str(payload.get("key") or "")
    return str(payload.get("by") or payload.get("requested_by") or payload.get("label") or "")


def summarize_entry(row: dict) -> dict:
    """One line per audit entry for the log explorer: actor, access decision, cited documents."""
    p = row.get("payload") or {}
    out = {
        "line": row.get("line"),
        "ts": row.get("ts", ""),
        "event": row.get("event", ""),
        "actor": _actor(row.get("event", ""), p),
        "collection": p.get("collection", ""),
        "decision": (p.get("access") or {}).get("decision", ""),
        "basis": (p.get("access") or {}).get("basis", ""),
        "cited": [s["doc_id"] for s in p.get("sources", []) if s.get("cited")],
        "flags": sorted({f for s in p.get("sources", []) for f in s.get("injection_flags", [])}),
    }
    return out


# ---------- Overview ----------


def _bucket(ts: str, size_s: int) -> int | None:
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return int(t.timestamp()) // size_s * size_s


def activity(rows: list[dict], buckets: int = 12) -> dict:
    """Requests per time bucket from the audit log: questions (allowed / denied) and logged completions.

    The bucket size adapts to the span of the log (a minute up to a day), so a fresh demo and a
    month-old server both get a readable chart.
    """
    kinds = {"document_question": "questions", "document_question_denied": "denied", "completion": "completions"}
    times = [r["ts"] for r in rows if r.get("event") in kinds and r.get("ts")]
    if not times:
        return {"bucket_seconds": 60, "series": []}
    first, last = _bucket(times[0], 1), _bucket(times[-1], 1)
    span = max(60, (last or 0) - (first or 0))
    size = next((s for s in (60, 300, 900, 3600, 4 * 3600, 86400) if span / s <= buckets), 86400)
    counts: dict[int, Counter] = {}
    for r in rows:
        kind = kinds.get(r.get("event", ""))
        if not kind:
            continue
        b = _bucket(r.get("ts", ""), size)
        if b is not None:
            counts.setdefault(b, Counter())[kind] += 1
    end = max(counts)
    start = max(min(counts), end - (buckets - 1) * size)
    start = min(start, end - 5 * size)  # at least six buckets, so one busy minute isn't a lone bar
    series = []
    for b in range(start, end + size, size):
        c = counts.get(b, Counter())
        series.append(
            {
                "t": datetime.fromtimestamp(b, UTC).isoformat(),
                "questions": c["questions"],
                "denied": c["denied"],
                "completions": c["completions"],
            }
        )
    return {"bucket_seconds": size, "series": series}


def overview() -> dict:
    keys = store.list_keys()
    users = store.list_principal_usage()
    collections = rag.list_collections(None)
    rows = audit_rows(limit=20_000)
    events = Counter(r.get("event", "") for r in rows)
    decisions = Counter(
        (r.get("payload") or {}).get("access", {}).get("decision", "")
        for r in rows
        if r.get("event") in ("document_question", "document_question_denied")
    )
    flagged = sum(
        1
        for r in rows
        if r.get("event") == "document_question"
        and any(s.get("injection_flags") for s in (r.get("payload") or {}).get("sources", []))
    )
    active = [k for k in keys if not k.revoked_at]
    return {
        "requests": {
            "total": sum(k.requests_total for k in keys) + sum(u["requests_total"] for u in users),
            "tokens": sum(k.tokens_total for k in keys) + sum(u["tokens_total"] for u in users),
            "questions": events["document_question"],
            "denied": events["document_question_denied"],
        },
        "identities": {
            "keys_active": len(active),
            "keys_revoked": len(keys) - len(active),
            "admin_keys": sum(k.is_admin for k in active),
            "token_users": len(users),
        },
        "documents": {
            "collections": len(collections),
            "documents": sum(c["documents"] for c in collections),
            "passages": sum(c["chunks"] for c in collections),
        },
        "audit": {**audit.verify(), "events": dict(events.most_common())},
        "access_decisions": {"allow": decisions["allow"], "deny": decisions["deny"]},
        "injection_flagged_answers": flagged,
        "activity": activity(rows),
        "retrieval": rag.retrieval_config(),
        "rate_limit_per_min": settings.GATEWAY_RATE_LIMIT_PER_MIN,
        "model_policy": settings.GATEWAY_MODEL_POLICY,
        "collection_default_access": settings.GATEWAY_COLLECTION_DEFAULT_ACCESS,
        "oidc": {
            "enabled": settings.GATEWAY_OIDC_ENABLED,
            "groups_claim": settings.GATEWAY_OIDC_GROUPS_CLAIM,
            "group_roles": settings.GATEWAY_OIDC_GROUP_ROLES,
        },
        "logging": {"prompts": settings.GATEWAY_LOG_PROMPTS, "redacted": settings.GATEWAY_REDACT_PROMPTS},
    }


# ---------- Access matrix ----------


def matrix_principals() -> list[identity.Principal]:
    """Every active API key, plus one representative member of each IdP group that maps to a role."""
    out = [identity.principal_for_key(k) for k in store.list_keys() if not k.revoked_at]
    for group in sorted(settings.GATEWAY_OIDC_GROUP_ROLES):
        groups = frozenset({group})
        out.append(
            identity.Principal(
                kind="oidc_group",
                subject=f"group:{group}",
                label=f"group:{group} member",
                roles=identity.roles_for_groups(groups),
                groups=groups,
            )
        )
    return out


def access_matrix(collection: str, principals: list[identity.Principal] | None = None) -> dict:
    """Which caller can read which document, with the rule that decided it. Uses rag.access_for()."""
    rag.check_collection(collection)
    docs = rag.list_documents(collection, rag.unrestricted_access(collection), include_acl=True)
    rows = []
    for p in principals if principals is not None else matrix_principals():
        a = rag.access_for(p, collection)
        rows.append(
            {
                **p.public(),
                "collection": {"allowed": a.allowed, "basis": a.basis},
                "documents": {d["id"]: a.doc_basis.get(d["id"]) for d in docs},
                "visible": len(a.doc_basis),
                "hidden": a.hidden,
            }
        )
    return {
        "collection": collection,
        "collection_acl": rag.collection_acl(collection),
        "default_access": settings.GATEWAY_COLLECTION_DEFAULT_ACCESS,
        "documents": [{"id": d["id"], "title": d["title"], "acl": d["acl"]} for d in docs],
        "rows": rows,
    }
