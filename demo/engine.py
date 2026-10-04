# Corey Mathie, 2026
"""
Browser demo engine (runs in Pyodide; also runs under CPython for the tests).

What is real: the gateway's own modules, imported unmodified from ../gateway/:
  store.py   API keys in SQLite (create, revoke, usage counters, key groups)
  auth.py    bearer-key check, revocation check and the per-key rate limiter
  identity.py  roles from groups, collection and document access decisions
  audit.py   SHA-256 hash-chained audit log, verify()
  redact.py  PII redaction
  logging_setup.log_prompt  redacted completion entries in the audit log
  rag.py     chunking, SQLite vector storage, access filtering before retrieval,
             retrieval, prompt building, citation parsing, injection flags
  retrieval.py BM25, vector ranking, Reciprocal Rank Fusion, lexical reranker
  backends.py  the pluggable backend interface (a demo backend is plugged in)

What is demo-only (this file):
  - The glue that main.py's FastAPI routes normally provide (mirrored closely).
  - DemoBackend, because there is no model in the browser:
      embeddings = deterministic hashed bag-of-words vectors (not a neural model)
      chat       = extractive answer: the best-matching sentences from the
                   retrieved passages, cited [n]; no text is generated.
  - Tamper helpers that edit the audit file the way an attacker would.
  - Personas. Token (OIDC) personas are built from claims with the gateway's own
    identity.principal_for_claims(); the JWT signature check (PyJWT) is server-only
    and does not run here. Key personas are real API keys with groups.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sys
import types
from collections import Counter
from pathlib import Path

try:  # demo/ next to gateway/ in the repo, or both copied into Pyodide's /home/pyodide
    import shims
except ImportError:  # pragma: no cover - imported as demo.engine under CPython
    from demo import shims

STUBBED = shims.install()

from gateway import audit, auth, backends, identity, rag, store  # noqa: E402
from gateway.config import settings  # noqa: E402
from gateway.logging_setup import log_prompt  # noqa: E402
from gateway.redact import redact  # noqa: E402

HTTPException = sys.modules["fastapi"].HTTPException

EMBED_DIM = 512
DEMO_MODEL = "demo-extractive (no LLM)"
DEMO_EMBED_MODEL = "demo-hashed-bow-512"
STOPWORDS = set(
    "a an and are as at be by can do does for from has have how i if in is it its may must of on or our "
    "should that the their there these this to was we what when where which who will with within you your "
    "per any all not no".split()
)


def terms(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9$]+", text.lower()):
        if w in STOPWORDS or len(w) < 2:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def hashed_embedding(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Signed feature hashing of terms with sublinear term frequency. Deterministic; not a neural embedding."""
    vec = [0.0] * dim
    for term, count in Counter(terms(text)).items():
        h = int.from_bytes(hashlib.blake2b(term.encode(), digest_size=8).digest(), "little")
        vec[h % dim] += (1.0 if (h >> 63) == 0 else -1.0) * (1.0 + math.log(count))
    return vec


def _sentences(text: str) -> list[str]:
    """Sentences worth quoting: no headings, and no fragments cut mid-word by chunk overlap."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for part in re.split(r"(?<=[.!?])\s+", line):
            part = part.strip(" -*")
            if len(part) > 20 and (part[0].isupper() or part[0].isdigit() or part[0] == "$"):
                out.append(part)
    return out


def extractive_answer(question: str, sources: list[tuple[int, str]], max_sentences: int = 3) -> str:
    """BM25 over the retrieved passages' sentences; returns the best sentences with [n] citations."""
    q = set(terms(question))
    cands, seen = [], set()
    for n, text in sources:
        for s in _sentences(text):
            # Like the system prompt tells a real model: instructions inside documents are not followed.
            if s not in seen and not rag.injection_signals(s):
                seen.add(s)
                cands.append((n, s, terms(s)))
    if not q or not cands:
        return "I don't know: the sources don't contain the answer."
    df = Counter(t for _, _, ts in cands for t in set(ts))
    avg = sum(len(ts) for _, _, ts in cands) / len(cands)
    scored = []
    for n, s, ts in cands:
        tf = Counter(ts)
        score = 0.0
        for t in q & tf.keys():
            idf = math.log(1 + (len(cands) - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(ts) / avg))
        scored.append((score, n, s))
    scored.sort(key=lambda x: -x[0])
    best = scored[0][0]
    if best <= 0:
        return "I don't know: the sources don't contain the answer."
    picked = [x for x in scored if x[0] >= 0.6 * best][:max_sentences]
    return " ".join(f"{s.rstrip('.')} [{n}]." for _, n, s in picked)


class DemoBackend:
    """Implements gateway.backends.Backend without a model."""

    name = "demo"
    supports_pull = False

    async def chat(self, p: backends.ChatParams) -> backends.ChatResult:
        content = p.messages[-1]["content"]
        m = re.match(r"Sources:\n\n(.*)\n\nQuestion: (.*)\Z", content, re.S)
        if m:  # a document question built by rag.build_messages
            blocks = re.split(r"\n\n(?=\[\d+\] )", m.group(1))
            sources = []
            for b in blocks:
                head, _, body = b.partition("\n")
                n = re.match(r"\[(\d+)\]", head)
                if n:
                    sources.append((int(n.group(1)), body))
            answer = extractive_answer(m.group(2), sources)
        else:
            answer = (
                "Demo mode: no language model runs in your browser. The gateway code authenticated this key, "
                "applied its rate limit, counted usage and wrote a redacted audit entry."
            )
        prompt_words = sum(len(str(x.get("content", "")).split()) for x in p.messages)
        return backends.ChatResult(answer, prompt_words, len(answer.split()), "chatcmpl-demo", p.model)

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        return [hashed_embedding(t) for t in texts]

    async def list_models(self) -> list[str]:
        return [DEMO_MODEL]

    async def health(self) -> bool:
        return True

    async def pull(self, model: str) -> str:
        raise backends.BackendNotSupported("no model downloads in the demo")


DEMO_ISSUER = "https://idp.demo.invalid"
GROUP_ROLES = {"staff": ["user"], "auditors": ["reader:policies"]}
PERSONAS = {
    "admin": {"name": "Admin", "kind": "api_key", "note": "bootstrap admin key: role admin"},
    "priya": {
        "name": "Priya (HR)",
        "kind": "oidc",
        "claims": {"sub": "priya", "groups": ["staff", "hr"]},
        "note": "SSO token, groups staff + hr",
    },
    "dana": {
        "name": "Dana (Engineering)",
        "kind": "oidc",
        "claims": {"sub": "dana", "groups": ["staff", "engineering"]},
        "note": "SSO token, groups staff + engineering",
    },
    "kiosk": {"name": "Lobby kiosk", "kind": "api_key", "label": "lobby-kiosk", "note": "API key, no groups"},
    "audrey": {
        "name": "Audrey (auditor)",
        "kind": "oidc",
        "claims": {"sub": "audrey", "groups": ["auditors"]},
        "note": "SSO token, group auditors: reader:policies only",
    },
}


def _mask(key: str) -> str:  # same as gateway/main.py
    return key[:12] + "…" if len(key) > 12 else key


def _request():
    return types.SimpleNamespace(state=types.SimpleNamespace())


class DemoEngine:
    def __init__(self, workdir: str = "/tmp/lldk-demo", rate_limit: int = 10):
        self.workdir = Path(workdir)
        self.rate_limit = rate_limit
        self._backup: str | None = None
        self.reset()

    # ---------- setup ----------

    def reset(self) -> dict:
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        settings.GATEWAY_DB_PATH = str(self.workdir / "gateway.db")
        settings.GATEWAY_LOG_DIR = str(self.workdir / "logs")
        settings.GATEWAY_RATE_LIMIT_PER_MIN = self.rate_limit
        settings.GATEWAY_LOG_PROMPTS = True
        settings.GATEWAY_REDACT_PROMPTS = True
        settings.GATEWAY_DEFAULT_MODEL = DEMO_MODEL
        settings.GATEWAY_EMBED_MODEL = DEMO_EMBED_MODEL
        settings.GATEWAY_ADMIN_BOOTSTRAP_KEY = ""
        settings.GATEWAY_OIDC_ISSUER = DEMO_ISSUER
        settings.GATEWAY_OIDC_GROUP_ROLES = GROUP_ROLES
        settings.GATEWAY_COLLECTION_DEFAULT_ACCESS = "open"
        settings.GATEWAY_RETRIEVAL_MODE = "hybrid"
        settings.GATEWAY_RERANKER = "none"
        backends.set_backend(DemoBackend())
        auth._calls.clear()
        self._backup = None
        store.init_db()
        rag.init_rag()
        self.admin_key = store.ensure_bootstrap_admin()
        audit.append("admin_bootstrap", {"label": "bootstrap admin"})
        self._persona_keys = {"admin": self.admin_key}
        return {"ok": True}

    def set_retrieval(self, mode: str, reranker: str = "none") -> dict:
        """Same switches as GATEWAY_RETRIEVAL_MODE / GATEWAY_RERANKER (cross_encoder is server-only)."""
        if mode not in {"vector", "bm25", "hybrid"} or reranker not in {"none", "lexical"}:
            return {"error": "mode: vector | bm25 | hybrid; reranker: none | lexical"}
        settings.GATEWAY_RETRIEVAL_MODE = mode
        settings.GATEWAY_RERANKER = reranker
        return rag.retrieval_config()

    def set_rate_limit(self, per_min: int) -> dict:
        self.rate_limit = max(1, min(int(per_min), 1000))
        settings.GATEWAY_RATE_LIMIT_PER_MIN = self.rate_limit
        return {"rate_limit_per_min": self.rate_limit}

    def info(self) -> dict:
        return {
            "python": sys.version.split()[0],
            "stubbed_packages": STUBBED,
            "rate_limit_per_min": settings.GATEWAY_RATE_LIMIT_PER_MIN,
            "backend": backends.get_backend().name,
            "embed_model": settings.GATEWAY_EMBED_MODEL,
        }

    # ---------- keys (mirrors /admin/keys) ----------

    def list_keys(self) -> list[dict]:
        return [
            {
                "key": k.key,
                "masked": _mask(k.key),
                "label": k.label,
                "is_admin": k.is_admin,
                "revoked": bool(k.revoked_at),
                "requests_total": k.requests_total,
                "tokens_total": k.tokens_total,
            }
            for k in store.list_keys()
        ]

    def create_key(self, label: str, is_admin: bool = False, groups: list[str] | None = None) -> dict:
        label = (label or "").strip()[:60] or "app"
        created = store.create_key(label=label, is_admin=is_admin, groups=groups or [])
        audit.append(
            "key_created",
            {
                "label": label,
                "is_admin": is_admin,
                "groups": created.groups,
                "key": _mask(created.key),
                "by": "bootstrap admin",
            },
        )
        return {"key": created.key, "label": label}

    # ---------- identity (mirrors /v1/me and the auth dependencies) ----------

    def personas(self) -> list[dict]:
        return [{"id": pid, "name": p["name"], "kind": p["kind"], "note": p["note"]} for pid, p in PERSONAS.items()]

    async def _principal(self, persona: str):
        spec = PERSONAS[persona]
        if spec["kind"] == "oidc":
            # Claims as a verified token would carry them; the role mapping below is the gateway's own code.
            return identity.principal_for_claims({"iss": DEMO_ISSUER, "aud": "llm-gateway", **spec["claims"]})
        if persona not in self._persona_keys:
            self._persona_keys[persona] = self.create_key(spec["label"])["key"]
        rec, err = await self._authenticate(self._persona_keys[persona])
        if err:
            raise RuntimeError(err["detail"])
        return rec

    async def whoami(self, persona: str, collection: str = "policies") -> dict:
        p = await self._principal(persona)
        access = rag.access_for(p, collection)
        visible = rag.list_documents(collection, access)
        return {
            **p.public(),
            "collections": [c["name"] for c in rag.list_collections(p)],
            "can_chat": p.is_user,
            "visible": [d["title"] for d in visible],
            # Shown on the demo page to explain the decision; the API never tells a caller what it can't see.
            "hidden_count": access.hidden,
            "decision": access.audit(),
        }

    def revoke_key(self, key: str) -> dict:
        active_admins = [k for k in store.list_keys() if k.is_admin and not k.revoked_at]
        if len(active_admins) == 1 and active_admins[0].key == key:
            return {"status": 409, "detail": "can't revoke the last active admin key; create another admin key first"}
        store.revoke_key(key)
        audit.append("key_revoked", {"key": _mask(key), "by": "bootstrap admin"})
        return {"status": 200, "detail": "revoked"}

    # ---------- chat request (mirrors POST /v1/chat/completions) ----------

    async def _authenticate(self, key: str):
        try:
            return await auth.require_key(_request(), f"Bearer {key}"), None
        except HTTPException as e:
            return None, {"status": e.status_code, "detail": e.detail}

    async def chat(self, key: str, prompt: str) -> dict:
        rec, err = await self._authenticate(key)
        if err:
            return err
        params = backends.ChatParams(model=DEMO_MODEL, messages=[{"role": "user", "content": prompt}])
        result = await backends.get_backend().chat(params)
        total = result.prompt_tokens + result.completion_tokens
        store.record_usage(rec.key, total)
        log_prompt(rec.label, DEMO_MODEL, json.dumps(params.messages), result.content, total)
        return {"status": 200, "content": result.content, "total_tokens": total}

    # ---------- redaction ----------

    def redact(self, text: str) -> dict:
        out, counts = redact(text)
        return {"text": out, "counts": counts}

    # ---------- audit (mirrors /admin/audit/*) ----------

    def audit_lines(self) -> list[dict]:
        path = audit._path()
        if not path.exists():
            return []
        rows = []
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            if line.strip():
                try:
                    rows.append({"line": i, "entry": json.loads(line)})
                except ValueError:
                    rows.append({"line": i, "entry": {"event": "(unparseable)", "raw": line[:200]}})
        return rows

    def verify(self) -> dict:
        return audit.verify()

    def tamper(self, line: int, mode: str = "edit") -> dict:
        """Edit the audit file like an attacker. mode: edit | edit_rehash | delete."""
        path = audit._path()
        text = path.read_text()
        if self._backup is None:
            self._backup = text
        lines = text.splitlines()
        if not 1 <= line <= len(lines):
            return {"error": f"line must be between 1 and {len(lines)}"}
        is_last = line == len(lines)
        if mode == "delete":
            del lines[line - 1]
        else:
            row = json.loads(lines[line - 1])
            row["payload"] = {**row["payload"], "label": "edited-after-the-fact"}
            if mode == "edit_rehash":  # a smarter attacker recomputes this entry's own hash
                core = {k: row[k] for k in ("ts", "event", "payload", "prev_hash")}
                row["entry_hash"] = audit._sha256(audit._canonical(core))
            lines[line - 1] = json.dumps(row, separators=(",", ":"))
        path.write_text("\n".join(lines) + ("\n" if lines else ""))
        return {"mode": mode, "line": line, "was_last_line": is_last, "verify": audit.verify()}

    def restore(self) -> dict:
        if self._backup is not None:
            audit._path().write_text(self._backup)
            self._backup = None
        return audit.verify()

    # ---------- documents (mirrors /v1/collections/...) ----------

    async def add_document(self, collection: str, title: str, text: str, acl: list[str] | None = None) -> dict:
        try:
            doc = await rag.add_document(collection, title, text, "bootstrap admin", _now(), acl=acl or [])
        except rag.RagError as e:
            return {"status": 400, "detail": str(e)}
        audit.append(
            "document_added",
            {
                "collection": collection,
                "doc_id": doc["id"],
                "title": title,
                "chunks": doc["chunks"],
                "acl": doc["acl"],
                "by": "bootstrap admin",
            },
        )
        return {"status": 201, **doc}

    def list_documents(self, collection: str) -> list[dict]:  # the admin's view, with access lists
        return rag.list_documents(collection, rag.unrestricted_access(collection), include_acl=True)

    def remove_document(self, collection: str, doc_id: str) -> dict:
        removed = rag.delete_document(collection, doc_id)
        if not removed:
            return {"status": 404}
        audit.append(
            "document_removed",
            {"collection": collection, "doc_id": doc_id, "title": removed["title"], "by": "bootstrap admin"},
        )
        return {"status": 200}

    async def ask(self, collection: str, question: str, key: str, top_k: int = 4) -> dict:
        rec, err = await self._authenticate(key)
        if err:
            return err
        return await self._answer(rec, collection, question, top_k)

    async def ask_as(self, persona: str, collection: str, question: str, top_k: int = 4) -> dict:
        return await self._answer(await self._principal(persona), collection, question, top_k)

    async def chat_as(self, persona: str, prompt: str) -> dict:
        """Raw chat needs the user role (auth.require_user); collection readers get 403."""
        p = await self._principal(persona)
        if not p.is_user:
            return {"status": 403, "detail": "the user role is required"}
        params = backends.ChatParams(model=DEMO_MODEL, messages=[{"role": "user", "content": prompt}])
        result = await backends.get_backend().chat(params)
        return {"status": 200, "content": result.content}

    async def _answer(self, rec, collection: str, question: str, top_k: int) -> dict:
        try:
            access = rag.access_for(rec, collection)
        except rag.RagError as e:
            return {"status": 400, "detail": str(e)}
        if not access.doc_ids:
            if access.hidden:
                audit.append(
                    "document_question_denied", {"key": rec.label, "collection": collection, "access": access.audit()}
                )
            return {"status": 404, "detail": f"collection {collection!r} has no documents yet"}
        sources = await rag.retrieve(collection, question, top_k, access)
        if not sources:
            return {"status": 404, "detail": f"collection {collection!r} has no documents yet"}
        params = backends.ChatParams(model=DEMO_MODEL, messages=rag.build_messages(question, sources), temperature=0.1)
        result = await backends.get_backend().chat(params)
        tokens = result.prompt_tokens + result.completion_tokens
        store.record_principal_usage(rec, tokens)
        cited = rag.cited_numbers(result.content)
        flags = {s.n: rag.injection_signals(s.text) for s in sources}
        entry = {
            "key": rec.label,
            "collection": collection,
            "model": DEMO_MODEL,
            "retrieval": rag.retrieval_config(),
            "access": access.audit(),
            "sources": [
                {
                    "doc_id": s.doc_id,
                    "chunk": s.chunk,
                    "score": round(s.score, 4),
                    "cited": s.n in cited,
                    "access": s.access,
                    **({"injection_flags": flags[s.n]} if flags[s.n] else {}),
                }
                for s in sources
            ],
            "tokens": tokens,
            "question": redact(question)[0],
            "answer": redact(result.content)[0],
        }
        audit.append("document_question", entry)
        return {
            "status": 200,
            "answer": result.content,
            "model": DEMO_MODEL,
            "sources": [
                {
                    "n": s.n,
                    "doc_id": s.doc_id,
                    "title": s.title,
                    "chunk": s.chunk,
                    "score": round(s.score, 4),
                    "cited": s.n in cited,
                    "injection_flags": flags[s.n],
                    "scores": s.scores,
                    "excerpt": s.text[:600],
                }
                for s in sources
            ],
            "system_prompt": rag.SYSTEM_PROMPT,
            "access": access.audit(),
            "retrieval": rag.retrieval_config(),
            "asked_as": rec.label,
        }


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


ENGINE: DemoEngine | None = None


async def call(method: str, args_json: str = "[]") -> str:
    """Single JSON entry point for the page's JavaScript."""
    global ENGINE
    if ENGINE is None:
        ENGINE = DemoEngine()
    fn = getattr(ENGINE, method)
    result = fn(*json.loads(args_json))
    if hasattr(result, "__await__"):
        result = await result
    return json.dumps(result)
