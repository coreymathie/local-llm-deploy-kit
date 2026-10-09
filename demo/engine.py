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
  console.py   overview counters, audit rows, access matrix, runtime policy validation
  supply_chain.py  lock-file parsing, verification statuses and the off/warn/enforce policy
  scripts/mlbom.py, scripts/rag_eval.py  ML-BOM model components and the RAG eval (when fetched)

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
  - SimulatedModelServer: a stand-in Ollama model inventory for the Models screen.
    Its digests are SHA-256 of a fixed string, not real model digests; verification,
    pinning and the policy decision run the gateway's supply_chain.py against it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import sys
import time
import types
from collections import Counter
from pathlib import Path

try:  # demo/ next to gateway/ in the repo, or both copied into Pyodide's /home/pyodide
    import shims
except ImportError:  # pragma: no cover - imported as demo.engine under CPython
    from demo import shims

STUBBED = shims.install()
if sys.platform == "emscripten":  # keep gateway warnings (e.g. model policy) out of the browser console
    logging.getLogger("gateway").addHandler(logging.NullHandler())

import numpy as np  # noqa: E402

from gateway import audit, auth, backends, console, identity, rag, retrieval, store, supply_chain  # noqa: E402
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


def terms(text: str, digits: bool = False) -> list[str]:
    """Search terms. digits=True keeps single digits ("3" in "level 3") for picking answer sentences."""
    out = []
    for w in re.findall(r"[a-z0-9$]+", text.lower()):
        if w in STOPWORDS or (len(w) < 2 and not (digits and w.isdigit())):
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


NO_ANSWER = "I don't know: none of the documents you can access answer that."
# "How long", "how much", "how often": the question's form, not its subject. Ignored when picking answer
# sentences, so "How long are records kept?" doesn't quote a sentence that says passwords are 14 characters long.
QUESTION_FORM = {"long", "much", "many", "often", "fast", "soon"}


def extractive_answer(question: str, sources: list[tuple[int, str]], max_sentences: int = 3) -> str:
    """BM25 over the retrieved passages' sentences; returns the best sentences with [n] citations.

    Sentences come from the best sentence's source or the top-ranked passage, unless another source scores
    nearly as well (85%), so an answer doesn't stitch an unrelated policy onto the right one.
    """
    q = set(terms(question, digits=True)) - QUESTION_FORM
    cands, seen = [], set()
    for n, text in sources:
        for s in _sentences(text):
            # Like the system prompt tells a real model: instructions inside documents are not followed.
            if s not in seen and not rag.injection_signals(s):
                seen.add(s)
                cands.append((n, s, terms(s, digits=True)))
    if not q or not cands:
        return NO_ANSWER
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
    best, top_n = scored[0][0], scored[0][1]
    if best <= 0:
        return NO_ANSWER
    picked = [x for x in scored if x[0] >= 0.6 * best and (x[1] in (top_n, 1) or x[0] >= 0.85 * best)][:max_sentences]
    numbers = {t for t in q if t.isdigit()}
    if numbers & set(terms(scored[0][2], digits=True)):  # "level 3": keep to sentences about level 3
        picked = [x for x in picked if numbers & set(terms(x[2], digits=True))]
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


class SimulatedModelServer:
    """A stand-in for Ollama's model inventory (/api/tags): names, digests, details. Simulated.

    `name` is "ollama" so supply_chain.verify() checks the lock file's Ollama pins against it.
    """

    name = "ollama"
    supports_pull = False

    def __init__(self):
        self.revisions = {"llama3.1:8b": 1, "nomic-embed-text:latest": 1}
        self.details = {
            "llama3.1:8b": {"family": "llama", "parameter_size": "8.0B", "quantization_level": "Q4_K_M"},
            "nomic-embed-text:latest": {"family": "nomic-bert", "parameter_size": "137M", "quantization_level": "F16"},
        }

    def digest(self, name: str) -> str:
        return hashlib.sha256(f"simulated:{name}:rev{self.revisions[name]}".encode()).hexdigest()

    async def model_inventory(self) -> dict:
        return {n: {"digest": self.digest(n), "details": self.details[n]} for n in self.revisions}

    async def list_models(self) -> list[str]:
        return list(self.revisions)


# Source, license and purpose for the simulated models, as in models.lock.example.json.
MODEL_METADATA = {
    "llama3.1:8b": {
        "source": "https://ollama.com/library/llama3.1",
        "license": "Llama 3.1 Community License",
        "purpose": "text-generation",
    },
    "nomic-embed-text:latest": {
        "source": "https://ollama.com/library/nomic-embed-text",
        "license": "Apache-2.0",
        "purpose": "embedding",
    },
}


DEMO_ISSUER = "https://idp.demo.invalid"
# Sign-in groups to roles. Staff get the user role; collection access lists (demo/data/library.json) then decide
# which collections they read. Internal audit reads every collection, read-only and without free-form chat;
# a document's own access list still applies to auditors.
GROUP_ROLES = {"staff": ["user"], "auditors": ["reader:*"]}

# Suggested questions on the assistant screen: (question, the document that must answer it). A None document
# marks a question that demonstrates a refusal; its note says so in the UI.
PERSONAS = {
    "admin": {"name": "Admin", "kind": "api_key", "note": "bootstrap admin key: role admin"},
    "priya": {
        "name": "Priya Shah (Human resources)",
        "kind": "oidc",
        "department": "Human resources",
        "claims": {"sub": "priya", "groups": ["staff", "hr"]},
        "note": "HR business partner · SSO token, groups staff + hr",
        "suggested": [
            ("What is the level 3 salary band?", "restricted-hr-compensation-bands.md"),
            ("How many days of PTO do I get?", "paid-time-off-and-leave.md"),
            ("What is the 401k match?", "benefits-overview.md"),
            ("Can I accept a gift from a member?", "code-of-conduct.md"),
        ],
    },
    "dana": {
        "name": "Dana Ortiz (IT and digital banking)",
        "kind": "oidc",
        "department": "IT and digital banking",
        "claims": {"sub": "dana", "groups": ["staff", "engineering"]},
        "note": "Digital banking engineer · SSO token, groups staff + engineering",
        "suggested": [
            (
                "How fast must the payments on-call engineer acknowledge a page?",
                "restricted-payments-oncall-runbook.md",
            ),
            ("When is the production change freeze?", "change-management-policy.md"),
            ("How long is a vendor account valid?", "vendor-access-standard.md"),
            ("How long must passwords be?", "password-and-mfa-standard.md"),
        ],
    },
    "marcus": {
        "name": "Marcus Bell (Compliance and BSA)",
        "kind": "oidc",
        "department": "Compliance and BSA",
        "claims": {"sub": "marcus", "groups": ["staff", "compliance"]},
        "note": "BSA officer · SSO token, groups staff + compliance",
        "suggested": [
            ("How long does the BSA officer have to decide on a filing?", "restricted-bsa-aml-escalation.md"),
            ("When must a confirmed OFAC match be reported?", "restricted-bsa-aml-escalation.md"),
            (
                "How long can we delay a suspicious transaction for an older member?",
                "elder-financial-abuse-reporting.md",
            ),
            ("How fast must a complaint be acknowledged?", "sample-complaint-handling-policy.md"),
        ],
    },
    "kiosk": {
        "name": "Branch lobby kiosk",
        "kind": "api_key",
        "label": "lobby-kiosk",
        "groups": ["public"],
        "note": "Member-facing kiosk · API key, group public: Member information only",
        "suggested": [
            ("What time do branches close on Saturday?", "branch-hours-and-holidays.md"),
            ("How much is a stop payment?", "fee-schedule.md"),
            ("What rate is a new car loan?", "auto-loan-rate-sheet.md"),
            ("How do I become a member?", "how-to-become-a-member.md"),
        ],
        "popular": [
            ("What is the overdraft fee?", "fee-schedule.md"),
            ("How much can I deposit with mobile deposit?", "mobile-deposit.md"),
            ("What does a 12-month certificate earn?", "savings-and-certificate-rates.md"),
            ("Are branches open on Thanksgiving?", "branch-hours-and-holidays.md"),
        ],
    },
    "audrey": {
        "name": "Audrey Kim (Internal audit)",
        "kind": "oidc",
        "department": "Internal audit",
        "claims": {"sub": "audrey", "groups": ["auditors"]},
        "note": "Internal auditor · SSO token, group auditors: reader:* (every collection, read-only); restricted "
        "documents stay hidden and free-form chat is refused",
        "suggested": [
            ("How long are audit logs retained?", "sample-data-retention-policy.md"),
            ("What is the level 3 salary band?", None),
            ("What is the gift limit for employees?", "code-of-conduct.md"),
            ("How often is the business continuity plan tested?", "business-continuity-plan-summary.md"),
        ],
        "notes": {"What is the level 3 salary band?": "HR-only document: expect a refusal"},
    },
}
# Staff personas without their own list (the admin, keys created in the console), and the sidebar's examples.
STAFF_SUGGESTED = [
    ("What is the hotel cap per night?", "sample-travel-expense-policy.md"),
    ("How long is an oral stop payment good for?", "stop-payment-procedure.md"),
    ("What should I do if my laptop is stolen?", "phishing-and-incident-reporting.md"),
    ("Which wires need a callback?", "sample-wire-transfer-verification.md"),
]
STAFF_POPULAR = [
    ("How long is an oral stop payment good for?", "stop-payment-procedure.md"),
    ("What is the hotel cap per night?", "sample-travel-expense-policy.md"),
    ("How much tuition is reimbursed each year?", "tuition-reimbursement.md"),
    ("What is the 401k match?", "benefits-overview.md"),
]
# Seeded on boot so the overview and audit log have something to show (labelled as this session's traffic).
SAMPLE_TRAFFIC = [
    ("priya", "What is the level 3 salary band?"),
    ("dana", "What is the level 3 salary band?"),
    ("dana", "How fast must the payments on-call engineer acknowledge a page?"),
    ("kiosk", "What time do branches close on Saturday?"),
    ("audrey", "How long are audit logs retained?"),
]
# Demo API keys created on boot: (label, groups). A member-facing web widget reads Member information only;
# the intranet search app reads what any employee may read.
SAMPLE_KEYS = [("member-faq-portal", ["public"]), ("staff-intranet-search", ["staff"])]


def persona_principal(persona: str) -> identity.Principal:
    """A persona's principal from its definition alone (no key, no rate limit): for evals and tests.

    OIDC personas go through identity.principal_for_claims with the demo's GROUP_ROLES; key personas get the
    user role and their key groups, as identity.principal_for_key gives a non-admin key.
    """
    spec = PERSONAS[persona]
    if spec["kind"] == "oidc":
        saved = settings.GATEWAY_OIDC_GROUP_ROLES
        settings.GATEWAY_OIDC_GROUP_ROLES = GROUP_ROLES
        try:
            return identity.principal_for_claims({"iss": DEMO_ISSUER, "aud": "llm-gateway", **spec["claims"]})
        finally:
            settings.GATEWAY_OIDC_GROUP_ROLES = saved
    if persona == "admin":
        return identity.Principal("api_key", "admin", "bootstrap admin", frozenset({"admin"}), frozenset())
    label = spec["label"]
    return identity.Principal("api_key", label, label, frozenset({"user"}), frozenset(spec.get("groups", ())))


ALL = "*"  # the console's "All sources": every collection the caller may read
# The console declines when the best passage's embedding similarity is below this, instead of quoting a
# passage that shares one word with the question. Measured on the sample library: real questions score
# 0.13 and up, off-topic ones ("What is our CEO's name?") 0.07. scripts/rag_eval.py applies the same rule
# through answer(), so its decline questions measure what the console does.
RELEVANCE_FLOOR = 0.09


async def retrieve_all(principal, question: str, top_k: int, config: dict):
    """One ranking over every collection the caller may read (the console's "All sources").

    Each collection's access decision is made first, exactly as for a single collection; only the admitted
    passages are pooled and ranked together, so hidden documents never reach scoring. Returns (sources,
    combined access decision, doc id -> collection).
    """
    pooled, decisions, where = [], [], {}
    for c in rag.list_collections(None):
        access = rag.access_for(principal, c["name"])
        decisions.append(access.audit())
        if access.allowed and access.doc_ids:
            for row in rag.candidates(c["name"], access):
                pooled.append((access, row))
                where[row["doc_id"]] = c["name"]
    combined = _combined(decisions)
    if not pooled:
        return [], combined, where
    vector_scores = None
    if config["mode"] != "bm25":
        q = (await rag.embed([question]))[0]
        matrix = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for _, r in pooled])
        vector_scores = (matrix @ q).tolist()
    ranked = retrieval.rank(
        question,
        [r["text"] for _, r in pooled],
        vector_scores,
        mode=config["mode"],
        top_k=max(1, min(top_k, 12)),
        rrf_k=settings.GATEWAY_RRF_K,
        reranker=retrieval.get_reranker(config["reranker"], settings.GATEWAY_CROSS_ENCODER_MODEL),
        rerank_candidates=settings.GATEWAY_RERANK_CANDIDATES,
    )
    sources = [
        rag.Source(
            n=i + 1,
            doc_id=pooled[j][1]["doc_id"],
            title=pooled[j][1]["title"],
            chunk=pooled[j][1]["idx"],
            score=float(detail["final"]),
            text=pooled[j][1]["text"],
            access=pooled[j][0].doc_basis.get(pooled[j][1]["doc_id"], ""),
            scores={k: v for k, v in detail.items() if k != "final"},
        )
        for i, (j, detail) in enumerate(ranked)
    ]
    return sources, combined, where


async def answer(question: str, sources: list) -> backends.ChatResult:
    """The console's answer step: decline below the relevance floor, otherwise ask the backend."""
    similarity = max((s.scores or {}).get("vector", 1.0) for s in sources)
    if similarity < RELEVANCE_FLOOR:  # nothing close enough to answer from: decline without generating
        return backends.ChatResult(NO_ANSWER, 0, len(NO_ANSWER.split()), "chatcmpl-demo", DEMO_MODEL)
    params = backends.ChatParams(model=DEMO_MODEL, messages=rag.build_messages(question, sources), temperature=0.1)
    return await backends.get_backend().chat(params)


def _combined(decisions: list[dict]) -> dict:
    visible = sum(d["documents_visible"] for d in decisions)
    return {
        "decision": "allow" if visible else "deny",
        "basis": "each collection's own rule",
        "documents_visible": visible,
        "documents_hidden": sum(d["documents_hidden"] for d in decisions),
        "collections_searched": sum(d["decision"] == "allow" for d in decisions),
    }


def _mask(key: str) -> str:  # same as gateway/main.py
    return key[:12] + "…" if len(key) > 12 else key


def _request():
    return types.SimpleNamespace(state=types.SimpleNamespace())


class DemoEngine:
    def __init__(self, workdir: str = "/tmp/plp-demo", rate_limit: int = 30):
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
        settings.GATEWAY_RRF_K = 60
        settings.GATEWAY_MODEL_POLICY = "warn"
        settings.GATEWAY_MODEL_LOCK_FILE = ""
        backends.set_backend(DemoBackend())
        auth._calls.clear()
        supply_chain.reset()
        self.model_server = SimulatedModelServer()
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
                "groups": k.groups,
                "created_at": k.created_at,
                "revoked_at": k.revoked_at,
                "revoked": bool(k.revoked_at),
                "requests_total": k.requests_total,
                "tokens_total": k.tokens_total,
            }
            for k in store.list_keys()
        ]

    def users(self) -> list[dict]:  # mirrors GET /admin/users
        return store.list_principal_usage()

    def rate_limits(self) -> dict:  # mirrors GET /admin/rate-limits
        now = time.monotonic()
        keys = []
        for k in store.list_keys():
            if k.revoked_at:
                continue
            window = [t for t in auth._calls.get(k.key, ()) if t >= now - auth._WINDOW_SEC]
            keys.append({"label": k.label, "key": _mask(k.key), "in_window": len(window)})
        return {"limit_per_min": settings.GATEWAY_RATE_LIMIT_PER_MIN, "window_seconds": 60, "keys": keys}

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
        """The built-in personas, then every other active API key (created on the Users & Keys screen)."""
        out = [
            {"id": pid, "name": p["name"], "kind": p["kind"], "note": p["note"], "department": p.get("department", "")}
            for pid, p in PERSONAS.items()
        ]
        bound = set(self._persona_keys.values())
        for k in store.list_keys():
            if k.revoked_at or k.key in bound:
                continue
            groups = ", ".join(k.groups) or "no groups"
            role = "admin" if k.is_admin else "user"
            note = f"API key, {role}, {groups}"
            out.append({"id": f"key:{k.label}:{k.key[-6:]}", "name": k.label, "kind": "api_key", "note": note})
        return out

    def suggestions(self) -> dict:
        """Suggested questions for the assistant screen: per persona, for other staff, and the sidebar's examples."""

        def items(pairs, notes=None):
            return [{"q": q, "note": (notes or {}).get(q, "")} for q, _doc in pairs]

        personas = {pid: items(p["suggested"], p.get("notes")) for pid, p in PERSONAS.items() if "suggested" in p}
        popular = {pid: items(p["popular"]) for pid, p in PERSONAS.items() if "popular" in p}
        return {
            "personas": personas,
            "popular_for": popular,
            "default": items(STAFF_SUGGESTED),
            "popular": items(STAFF_POPULAR),
        }

    def create_sample_keys(self) -> list[dict]:
        return [self.create_key(label, False, groups) for label, groups in SAMPLE_KEYS]

    async def load_library(self, catalog: dict, texts: dict[str, str]) -> dict:
        """Add every catalog document with its access list, then set each collection's access list."""
        added = 0
        for d in catalog["documents"]:
            out = await self.add_document(d["collection"], d["file"], texts[d["file"]], d["acl"])
            if out["status"] != 201:
                return {"status": out["status"], "detail": f"{d['file']}: {out.get('detail')}"}
            added += 1
        for c in catalog["collections"]:
            if c.get("acl"):
                out = self.set_collection_acl(c["name"], c["acl"])
                if out["status"] != 200:
                    return out
        return {"status": 201, "documents": added, "collections": len(catalog["collections"])}

    async def seed_sample_traffic(self) -> list[int]:
        return [(await self.ask_as(p, ALL, q))["status"] for p, q in SAMPLE_TRAFFIC]

    async def _principal(self, persona: str):
        if persona.startswith("key:"):
            suffix = persona.rsplit(":", 1)[1]
            rec = next((k for k in store.list_keys() if k.key.endswith(suffix) and not k.revoked_at), None)
            if rec is None:
                raise RuntimeError("that API key was revoked or doesn't exist")
            p, err = await self._authenticate(rec.key)
            if err:
                raise HTTPException(err["status"], err["detail"])
            return p
        spec = PERSONAS[persona]
        if spec["kind"] == "oidc":
            # Claims as a verified token would carry them; the role mapping below is the gateway's own code.
            return identity.principal_for_claims({"iss": DEMO_ISSUER, "aud": "llm-gateway", **spec["claims"]})
        if persona not in self._persona_keys:
            self._persona_keys[persona] = self.create_key(spec["label"], False, spec.get("groups", []))["key"]
        rec, err = await self._authenticate(self._persona_keys[persona])
        if err:
            raise HTTPException(err["status"], err["detail"])
        return rec

    async def whoami(self, persona: str, collection: str = "policies") -> dict:
        try:
            p = await self._principal(persona)
        except HTTPException as e:
            return {"status": e.status_code, "detail": e.detail}
        names = [c["name"] for c in rag.list_collections(None)] if collection == ALL else [collection]
        visible, hidden, decisions = [], 0, []
        for name in names:
            access = rag.access_for(p, name)
            visible += rag.list_documents(name, access)
            hidden += access.hidden
            decisions.append(access.audit())
        return {
            **p.public(),
            "collections": [c["name"] for c in rag.list_collections(p)],
            "can_chat": p.is_user,
            "visible": [d["title"] for d in visible],
            # Shown on the demo page to explain the decision; the API never tells a caller what it can't see.
            "hidden_count": hidden,
            "decision": decisions[0] if len(decisions) == 1 else _combined(decisions),
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

    async def ask_as(
        self,
        persona: str,
        collection: str,
        question: str,
        top_k: int = 4,
        mode: str | None = None,
        reranker: str | None = None,
    ) -> dict:
        try:
            principal = await self._principal(persona)
        except RuntimeError as e:
            return {"status": 401, "detail": str(e)}
        except HTTPException as e:  # rate limit on a key persona
            return {"status": e.status_code, "detail": e.detail}
        return await self._answer(principal, collection, question, top_k, mode, reranker)

    async def chat_as(self, persona: str, prompt: str) -> dict:
        """Raw chat needs the user role (auth.require_user); collection readers get 403."""
        try:
            p = await self._principal(persona)
        except RuntimeError as e:
            return {"status": 401, "detail": str(e)}
        except HTTPException as e:
            return {"status": e.status_code, "detail": e.detail}
        if not p.is_user:
            return {"status": 403, "detail": "the user role is required"}
        params = backends.ChatParams(model=DEMO_MODEL, messages=[{"role": "user", "content": prompt}])
        result = await backends.get_backend().chat(params)
        total = result.prompt_tokens + result.completion_tokens
        store.record_principal_usage(p, total)
        log_prompt(p.label, DEMO_MODEL, json.dumps(params.messages), result.content, total)
        return {"status": 200, "content": result.content, "total_tokens": total, "model": DEMO_MODEL}

    async def _answer(
        self, rec, collection: str, question: str, top_k: int, mode: str | None = None, reranker: str | None = None
    ) -> dict:
        started = time.perf_counter()
        try:
            config = rag.retrieval_config(mode or None, reranker or None)
            if collection == ALL:
                sources, access_audit, where = await retrieve_all(rec, question, top_k, config)
            else:
                access = rag.access_for(rec, collection)
                access_audit, where = access.audit(), {}
        except rag.RagError as e:
            return {"status": 400, "detail": str(e)}
        t_access = time.perf_counter()
        if collection == ALL:
            if not sources:
                return {"status": 404, "detail": "there are no documents you can read yet"}
        elif not access.doc_ids:
            if access.hidden:
                audit.append(
                    "document_question_denied", {"key": rec.label, "collection": collection, "access": access_audit}
                )
            return {"status": 404, "detail": f"collection {collection!r} has no documents yet"}
        else:
            sources = await rag.retrieve(
                collection, question, top_k, access, mode=config["mode"], reranker=config["reranker"]
            )
        if not sources:
            return {"status": 404, "detail": f"collection {collection!r} has no documents yet"}
        t_retrieval = time.perf_counter()
        result = await answer(question, sources)
        t_generation = time.perf_counter()
        timings = {
            "access": round((t_access - started) * 1000, 2),
            "retrieval": round((t_retrieval - t_access) * 1000, 2),
            "generation": round((t_generation - t_retrieval) * 1000, 2),
        }
        tokens = result.prompt_tokens + result.completion_tokens
        store.record_principal_usage(rec, tokens)
        cited = rag.cited_numbers(result.content)
        flags = {s.n: rag.injection_signals(s.text) for s in sources}
        entry = {
            "key": rec.label,
            "collection": collection,
            "model": DEMO_MODEL,
            "retrieval": config,
            "access": access_audit,
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
            "timings_ms": timings,
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
                    "collection": where.get(s.doc_id, collection),
                    "access": s.access,
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
            "access": access_audit,
            "retrieval": config,
            "timings_ms": timings,
            "usage": {"total_tokens": tokens},
            "asked_as": rec.label,
        }

    # ---------- console views (mirror /admin/overview, /admin/audit/entries, /admin/access-matrix) ----------

    def overview(self) -> dict:
        out = console.overview()
        out["version"] = VERSION
        out["backend"] = {"name": "demo", "default_model": DEMO_MODEL, "embed_model": DEMO_EMBED_MODEL}
        return out

    def audit_entries(self, limit: int = 200) -> dict:
        rows = console.audit_rows(limit=min(max(int(limit), 1), 2000))[::-1]
        for row in rows:
            row["summary"] = console.summarize_entry(row)
        return {"verify": audit.verify(), "entries": rows}

    def _persona_principal(self, persona: str):
        """A persona's principal without authenticating (no rate-limit hit): for the access matrix."""
        spec = PERSONAS[persona]
        if spec["kind"] == "oidc":
            return identity.principal_for_claims({"iss": DEMO_ISSUER, "aud": "llm-gateway", **spec["claims"]})
        if persona not in self._persona_keys:
            self._persona_keys[persona] = self.create_key(spec["label"], False, spec.get("groups", []))["key"]
        rec = store.get_active_key(self._persona_keys[persona])
        return identity.principal_for_key(rec) if rec else None  # None: the persona's key was revoked

    def access_matrix(self, collection: str = "policies") -> dict:
        principals, names = [], {}
        for pid, spec in PERSONAS.items():
            p = self._persona_principal(pid)
            if p is None:
                continue
            principals.append(p)
            names[p.label] = spec["name"]
        bound = set(self._persona_keys.values())
        for k in store.list_keys():
            if not k.revoked_at and k.key not in bound:
                principals.append(identity.principal_for_key(k))
        try:
            out = console.access_matrix(collection, principals)
        except rag.RagError as e:
            return {"status": 400, "detail": str(e)}
        for row in out["rows"]:
            row["persona"] = names.get(row["label"], "")
        return out

    def collections(self) -> list[dict]:  # the admin's view: every collection, with its access list
        out = rag.list_collections(None)
        for c in out:
            c["acl"] = rag.collection_acl(c["name"])
        return out

    def get_collection_acl(self, collection: str) -> dict:
        return {"collection": collection, "principals": rag.collection_acl(collection)}

    def set_collection_acl(self, collection: str, principals: list[str]) -> dict:
        try:
            before, after = rag.set_collection_acl(collection, principals, "bootstrap admin", _now())
        except rag.RagError as e:
            return {"status": 400, "detail": str(e)}
        change = {"collection": collection, "before": before, "after": after, "by": "bootstrap admin"}
        audit.append("collection_acl_changed", change)
        return {"status": 200, "collection": collection, "principals": after}

    def set_document_acl(self, collection: str, doc_id: str, principals: list[str]) -> dict:
        try:
            changed = rag.set_document_acl(collection, doc_id, principals)
        except rag.RagError as e:
            return {"status": 400, "detail": str(e)}
        if changed is None:
            return {"status": 404, "detail": "no such document in this collection"}
        before, after = changed
        audit.append(
            "document_acl_changed",
            {"collection": collection, "doc_id": doc_id, "before": before, "after": after, "by": "bootstrap admin"},
        )
        return {"status": 200, "collection": collection, "doc_id": doc_id, "acl": after}

    # ---------- runtime policy (mirrors /admin/policy) ----------

    def policy(self) -> dict:
        return {"policy": console.current_policy(), "fields": console.POLICY_FIELDS, "persisted": False}

    def validate_policy(self, doc) -> dict:
        try:
            return {"ok": True, "policy": console.validate_policy(doc), "errors": []}
        except console.PolicyError as e:
            return {"ok": False, "policy": None, "errors": e.errors}

    def apply_policy(self, doc) -> dict:
        try:
            before, after = console.apply_policy(doc)
        except console.PolicyError as e:
            return {"status": 422, "detail": e.errors}
        if after:
            audit.append("policy_changed", {"before": before, "after": after, "by": "bootstrap admin"})
        self.rate_limit = settings.GATEWAY_RATE_LIMIT_PER_MIN
        return {"status": 200, "policy": console.current_policy(), "changed": after, "persisted": False}

    # ---------- models and supply chain (mirrors /admin/models/*, against SimulatedModelServer) ----------

    def _lock_path(self) -> Path:
        return self.workdir / "models.lock.json"

    async def models(self) -> dict:
        lock_text = self._lock_path().read_text() if self._lock_path().exists() else ""
        verification = supply_chain.last_result() or await self.verify_models()
        return {
            "backend": {"name": "demo", "chat_model": DEMO_MODEL, "embed_model": DEMO_EMBED_MODEL, "healthy": True},
            "inventory": await self.model_server.model_inventory(),
            "lock_text": lock_text,
            "policy": settings.GATEWAY_MODEL_POLICY,
            "verification": verification,
        }

    async def verify_models(self, trigger: str = "admin") -> dict:
        try:
            pins = supply_chain.load_lock()
        except supply_chain.LockError as e:
            return {"ok": False, "error": str(e), "models": {}, "checked_at": time.time(), "backend": "ollama"}
        result = await supply_chain.verify(self.model_server, pins)
        counts: dict[str, int] = {}
        for m in result["models"].values():
            counts[m["status"]] = counts.get(m["status"], 0) + 1
        audit.append(
            "model_verification",
            {
                "trigger": trigger,
                "by": "bootstrap admin",
                "ok": result["ok"],
                "policy": settings.GATEWAY_MODEL_POLICY,
                "counts": counts,
            },
        )
        return result

    async def pin_served(self) -> dict:
        """Trust on first use, like scripts/pin_models.py: pin whatever is served now."""
        served = await supply_chain.served_models(self.model_server)
        doc = {
            "version": 1,
            "models": [
                {"name": n, "backend": "ollama", "digest": "sha256:" + m["digest"], **MODEL_METADATA.get(n, {})}
                for n, m in sorted(served.items())
            ],
        }
        return await self.set_lock(json.dumps(doc, indent=2))

    def validate_lock(self, text: str) -> dict:
        try:
            pins = supply_chain.parse_lock(json.loads(text))
        except ValueError as e:
            return {"ok": False, "error": str(e), "models": []}
        return {"ok": True, "error": None, "models": [dict(p.__dict__) for p in pins]}

    async def set_lock(self, text: str) -> dict:
        checked = self.validate_lock(text)
        if not checked["ok"]:
            return {"status": 400, **checked}
        self._lock_path().write_text(text if text.endswith("\n") else text + "\n")
        settings.GATEWAY_MODEL_LOCK_FILE = str(self._lock_path())
        return {"status": 200, "verification": await self.verify_models()}

    async def simulate_repull(self, name: str = "llama3.1:8b") -> dict:
        """A new upstream build under the same name: the served digest changes."""
        if name not in self.model_server.revisions:
            return {"status": 404, "detail": f"{name} is not served"}
        self.model_server.revisions[name] += 1
        audit.append("model_pull_finished", {"model": name, "requested_by": "bootstrap admin", "status": "simulated"})
        return {"status": 200, "verification": await self.verify_models("model_pull")}

    async def check_model(self, name: str) -> dict:
        """supply_chain.check_model under the current GATEWAY_MODEL_POLICY: what a request for `name` gets."""
        try:
            status = await supply_chain.check_model(name, self.model_server)
        except supply_chain.ModelPolicyError as e:
            return {"status": 403, "detail": str(e), "policy": settings.GATEWAY_MODEL_POLICY}
        return {"status": 200, "model_status": status, "policy": settings.GATEWAY_MODEL_POLICY}

    def mlbom(self) -> dict:
        """Model components of the ML-BOM, built by scripts/mlbom.py (fetched into the browser)."""
        try:
            from scripts.mlbom import model_components
        except ImportError:
            return {"status": 501, "detail": "scripts/mlbom.py was not loaded", "components": []}
        try:
            pins = supply_chain.load_lock()
        except supply_chain.LockError as e:
            return {"status": 400, "detail": str(e), "components": []}
        components = model_components(pins, supply_chain.last_result())
        return {"lock_file": settings.GATEWAY_MODEL_LOCK_FILE, "components": components}

    # ---------- evals ----------

    async def run_rag_eval(self) -> dict:
        """scripts/rag_eval.py in this runtime (it uses its own throwaway database and restores settings)."""
        sys.modules.setdefault("demo", types.ModuleType("demo"))
        sys.modules.setdefault("demo.engine", sys.modules[__name__])
        from scripts import rag_eval

        started = time.perf_counter()
        report = await rag_eval.evaluate_async(4)
        backends.set_backend(DemoBackend())  # evaluate() clears the backend when it finishes
        problems = rag_eval.check(report, json.loads(rag_eval.THRESHOLDS.read_text()))
        return {"report": report, "problems": problems, "seconds": round(time.perf_counter() - started, 2)}


VERSION = "0.7.0"  # keep in step with gateway/main.py (tests check)


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
