# Corey Mathie, 2026
"""
FastAPI gateway in front of a local inference server (Ollama by default, or any
OpenAI-compatible server such as vLLM, SGLang, TGI or NVIDIA NIM; see backends.py).

- OpenAI-compatible /v1/chat/completions (streaming and non-streaming)
- /v1/models passthrough, /v1/pull/{model} (admin, Ollama only, runs in the background)
- /admin/keys for key management; /v1/me for the caller's identity and roles
- Bearer API keys and (optionally) OIDC JWTs, mapped to roles; see identity.py
- /admin/audit/verify and /admin/audit/recent for the tamper-evident audit log
- /v1/collections/... private document Q&A (retrieval over local files, cited answers)
- /v1/embeddings OpenAI-compatible embeddings from the local embedding model
- /admin serves the single-file dashboard
- /metrics Prometheus metrics (optionally bearer-protected)
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import audit, crypto, identity, metrics, rag, supply_chain
from .auth import require_admin, require_key, require_user
from .backends import BackendHTTPError, BackendNotSupported, ChatParams, aclose_clients, get_backend
from .config import settings
from .identity import Principal
from .logging_setup import log_prompt, setup_logging
from .models import ApiKey, ApiKeyCreate, ChatCompletionRequest
from .redact import redact
from .store import (
    create_key,
    ensure_bootstrap_admin,
    init_db,
    list_keys,
    list_principal_usage,
    record_principal_usage,
    revoke_key,
)

__version__ = "0.6.0"

setup_logging()
log = logging.getLogger("gateway")

KeyDep = Annotated[Principal, Depends(require_key)]  # any authenticated caller with a role
UserDep = Annotated[Principal, Depends(require_user)]  # user or admin role
AdminDep = Annotated[Principal, Depends(require_admin)]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    identity.validate_settings()
    crypto.validate_settings()
    supply_chain.load_lock()  # a malformed lock file stops startup
    supply_chain.reset()
    init_db()
    rag.init_rag()
    metrics.INFO.labels(__version__, settings.BACKEND).set(1)
    created = ensure_bootstrap_admin()
    if created:
        audit.append("admin_bootstrap", {"label": "bootstrap admin"})
        log.info("=" * 64)
        log.info("Bootstrap admin API key: %s", created)
        log.info("Save it now. It will not be printed again.")
        log.info("=" * 64)
    if settings.GATEWAY_MODEL_POLICY != "off":
        try:  # bounded, so an unreachable backend doesn't hold up startup; requests re-verify on demand
            await asyncio.wait_for(_verify_models("startup"), timeout=10)
        except TimeoutError:
            log.warning("model verification at startup timed out; it will run on the first request")
    yield
    await aclose_clients()


app = FastAPI(title="local-llm-deploy-kit", version=__version__, lifespan=lifespan)
app.add_middleware(metrics.MetricsMiddleware)


def _mask(key: str) -> str:
    return key[:12] + "…" if len(key) > 12 else key


async def _verify_models(trigger: str, by: str = "gateway") -> dict:
    """Verify served models against the lock file and audit the outcome."""
    result = await supply_chain.verify(get_backend())
    counts: dict[str, int] = {}
    for m in result["models"].values():
        counts[m["status"]] = counts.get(m["status"], 0) + 1
    audit.append(
        "model_verification",
        {
            "trigger": trigger,
            "by": by,
            "ok": result["ok"],
            "policy": settings.GATEWAY_MODEL_POLICY,
            "counts": counts,
            "problems": {
                n: m["status"] for n, m in result["models"].items() if m["status"] in ("mismatch", "missing", "error")
            },
            **({"error": result["error"]} if result.get("error") else {}),
        },
    )
    return result


async def _model_allowed(model: str) -> str | None:
    """Apply GATEWAY_MODEL_POLICY; 403 under enforce for unpinned or unverified models."""
    try:
        return await supply_chain.check_model(model, get_backend())
    except supply_chain.ModelPolicyError as e:
        raise HTTPException(403, str(e)) from e


def _upstream_error(e: Exception) -> HTTPException:
    """Map a backend failure to the gateway's error contract (unchanged since 0.3)."""
    if isinstance(e, BackendHTTPError):
        return HTTPException(e.status_code, e.text)
    return HTTPException(502, f"model runtime unavailable: {e.__class__.__name__}")


@app.get("/health")
async def health() -> dict:
    backend = get_backend()
    ok = await backend.health()
    out = {"status": "ok", "backend": backend.name, "backend_ok": ok}
    if backend.name == "ollama":
        out["ollama"] = ok  # kept for clients written against 0.4
    return out


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(authorization: str | None = Header(default=None)) -> Response:
    if not settings.GATEWAY_METRICS_ENABLED:
        raise HTTPException(404, "metrics are disabled")
    token = settings.GATEWAY_METRICS_TOKEN
    if token and not secrets.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(401, "metrics token required")
    body, content_type = metrics.render()
    return Response(body, media_type=content_type)


# ---------- OpenAI-compatible chat ----------


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, key: UserDep):
    model = body.model or settings.GATEWAY_DEFAULT_MODEL
    params = ChatParams(
        model=model,
        messages=[m.model_dump() for m in body.messages],
        temperature=body.temperature,
        max_tokens=body.max_tokens,
    )
    await _model_allowed(model)
    backend = get_backend()

    if body.stream:
        # Pull the first event before answering so an unreachable or failing backend becomes a
        # proper 502/4xx instead of a 200 stream that breaks half-way.
        events = backend.stream_chat(params).__aiter__()
        try:
            first = await events.__anext__()
        except StopAsyncIteration:
            first = None
        except (BackendHTTPError, httpx.HTTPError) as e:
            raise _upstream_error(e) from e
        include_usage = bool((body.stream_options or {}).get("include_usage"))
        return StreamingResponse(
            _stream_chat(first, events, params, key, include_usage), media_type="text/event-stream"
        )

    try:
        result = await backend.chat(params)
    except (BackendHTTPError, httpx.HTTPError) as e:
        raise _upstream_error(e) from e

    total = result.prompt_tokens + result.completion_tokens
    record_principal_usage(key, total)
    metrics.record_tokens(key.metrics_label, model, result.prompt_tokens, result.completion_tokens)
    log_prompt(key.label, model, json.dumps(params.messages), result.content, total)

    return {
        "id": result.id,
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": result.content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": total,
        },
    }


async def _stream_chat(first, events, params: ChatParams, key: Principal, include_usage: bool):
    model = params.model
    prompt_tokens = completion_tokens = 0
    full: list[str] = []

    async def _all():
        if first is not None:
            yield first
        async for event in events:
            yield event

    async for event in _all():
        if event.content:
            full.append(event.content)
            sse = {
                "choices": [{"delta": {"content": event.content}, "index": 0, "finish_reason": None}],
                "model": model,
            }
            yield f"data: {json.dumps(sse)}\n\n"
        if event.done:
            prompt_tokens, completion_tokens = event.prompt_tokens, event.completion_tokens
    if include_usage:  # OpenAI's stream_options.include_usage: a final chunk with no choices
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        yield f"data: {json.dumps({'choices': [], 'model': model, 'usage': usage})}\n\n"
    yield "data: [DONE]\n\n"
    tokens = prompt_tokens + completion_tokens
    record_principal_usage(key, tokens)
    metrics.record_tokens(key.metrics_label, model, prompt_tokens, completion_tokens)
    log_prompt(key.label, model, json.dumps(params.messages), "".join(full), tokens)


# ---------- Models ----------


@app.get("/v1/models")
async def models(_key: UserDep):
    try:
        names = await get_backend().list_models()
    except (BackendHTTPError, httpx.HTTPError) as e:
        raise HTTPException(502, f"model runtime unavailable: {e.__class__.__name__}") from e
    return {"object": "list", "data": [{"id": name, "object": "model"} for name in names]}


async def _pull(model_name: str, requested_by: str) -> None:
    try:
        status = await get_backend().pull(model_name)
    except (httpx.HTTPError, BackendNotSupported) as e:
        status = f"failed ({e.__class__.__name__})"
    audit.append("model_pull_finished", {"model": model_name, "requested_by": requested_by, "status": status})
    if settings.GATEWAY_MODEL_POLICY != "off":
        await _verify_models("model_pull", requested_by)  # a pull can change what a pinned name serves


@app.post("/v1/pull/{model_name:path}", status_code=202)
async def pull_model(model_name: str, background: BackgroundTasks, admin: AdminDep):
    """Start a model download and return immediately; large pulls take minutes. Ollama backend only."""
    if not get_backend().supports_pull:
        raise HTTPException(501, "this backend serves the models it was started with; pull them on that server")
    audit.append("model_pull_started", {"model": model_name, "requested_by": admin.label})
    background.add_task(_pull, model_name, admin.label)
    return {"status": "started", "model": model_name}


# ---------- Identity ----------


@app.get("/v1/me")
async def me(principal: KeyDep) -> dict:
    """Who the gateway thinks you are: kind (api_key | oidc), label, roles, groups, readable collections."""
    return {**principal.public(), "collections": [c["name"] for c in rag.list_collections(principal)]}


@app.get("/admin/users")
async def admin_users(_a: AdminDep) -> list[dict]:
    """Token (OIDC) callers seen so far, with usage. API keys are listed under /admin/keys."""
    return list_principal_usage()


# ---------- Admin: model supply chain ----------


@app.get("/admin/models/verification")
async def admin_model_verification(_a: AdminDep) -> dict:
    """The last verification of served models against the lock file (runs one if none yet)."""
    return supply_chain.last_result() or await _verify_models("admin_view", _a.label)


@app.post("/admin/models/verify")
async def admin_verify_models(admin: AdminDep) -> dict:
    """Re-verify now (re-hashes pinned files for openai_compatible backends). Audited."""
    try:
        supply_chain.load_lock()
    except supply_chain.LockError as e:
        raise HTTPException(400, str(e)) from e
    return await _verify_models("admin", admin.label)


# ---------- Admin: keys ----------


@app.get("/admin/keys")
async def admin_list_keys(_a: AdminDep) -> list[ApiKey]:
    return list_keys()


@app.post("/admin/keys")
async def admin_create_key(req: ApiKeyCreate, admin: AdminDep) -> ApiKey:
    created = create_key(label=req.label, is_admin=req.is_admin, groups=req.groups)
    audit.append(
        "key_created",
        {
            "label": req.label,
            "is_admin": req.is_admin,
            "groups": created.groups,
            "key": _mask(created.key),
            "by": admin.label,
        },
    )
    return created


@app.delete("/admin/keys/{key}")
async def admin_revoke_key(key: str, admin: AdminDep) -> dict:
    active_admins = [k for k in list_keys() if k.is_admin and not k.revoked_at]
    if len(active_admins) == 1 and active_admins[0].key == key:
        raise HTTPException(409, "can't revoke the last active admin key; create another admin key first")
    revoke_key(key)
    audit.append("key_revoked", {"key": _mask(key), "by": admin.label})
    return {"status": "revoked", "key": key}


# ---------- Admin: audit ----------


@app.get("/admin/audit/verify")
async def admin_audit_verify(_a: AdminDep) -> dict:
    return audit.verify()


@app.get("/admin/audit/recent")
async def admin_audit_recent(_a: AdminDep, limit: int = 50) -> list[dict]:
    return audit.recent(min(limit, 500))


# ---------- Private document Q&A ----------


class TextDocument(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)
    acl: list[str] = Field(default_factory=list)  # empty: inherit the collection's access


class AclUpdate(BaseModel):
    principals: list[str] = Field(default_factory=list)  # group:<name>, key:<label>, user:<name>; [] removes


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=4, ge=1, le=12)
    model: str | None = None


class EmbeddingsRequest(BaseModel):
    input: str | list[str]
    model: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _rag_error(e: Exception) -> HTTPException:
    if isinstance(e, rag.RagError):
        return HTTPException(400, str(e))
    if isinstance(e, BackendHTTPError):
        return HTTPException(e.status_code, e.text)
    if isinstance(e, httpx.HTTPError):
        return HTTPException(502, f"model runtime unavailable: {e.__class__.__name__}")
    raise e


async def _store_document(
    collection: str, title: str, text: str, admin: Principal, acl: list[str] | None = None
) -> dict:
    await _model_allowed(settings.GATEWAY_EMBED_MODEL)
    try:
        doc = await rag.add_document(collection, title, text, admin.label, _now(), acl=acl)
    except (rag.RagError, httpx.HTTPError) as e:
        raise _rag_error(e) from e
    audit.append(
        "document_added",
        {
            "collection": collection,
            "doc_id": doc["id"],
            "title": title,
            "chunks": doc["chunks"],
            "acl": doc["acl"],
            "by": admin.label,
        },
    )
    return doc


@app.get("/v1/collections")
async def collections(key: KeyDep) -> list[dict]:
    """Only the collections the caller may read, with counts of only the documents it may read."""
    return rag.list_collections(key)


@app.get("/v1/collections/{collection}/documents")
async def collection_documents(collection: str, key: KeyDep) -> list[dict]:
    try:
        rag.check_collection(collection)
    except rag.RagError as e:
        raise HTTPException(400, str(e)) from e
    # Unreadable collections and hidden documents look exactly like absent ones: no existence oracle.
    return rag.list_documents(collection, rag.access_for(key, collection), include_acl=key.is_admin)


@app.post("/v1/collections/{collection}/documents", status_code=201)
async def upload_document(
    collection: str,
    admin: AdminDep,
    file: Annotated[UploadFile, File()],
    acl: Annotated[str, Form()] = "",
) -> dict:
    """Add a PDF, Markdown, or text file. Admin only: collections are curated knowledge bases.

    `acl` (form field, optional): comma- or space-separated entries such as "group:hr key:hr-bot".
    """
    limit = settings.GATEWAY_MAX_UPLOAD_MB * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"file is larger than {settings.GATEWAY_MAX_UPLOAD_MB} MB")
    try:
        text = rag.extract_text(file.filename or "upload.txt", data)
    except rag.RagError as e:
        raise HTTPException(400, str(e)) from e
    entries = [e for e in acl.replace(",", " ").split() if e]
    return await _store_document(collection, file.filename or "upload", text, admin, entries)


@app.post("/v1/collections/{collection}/documents/text", status_code=201)
async def add_text_document(collection: str, body: TextDocument, admin: AdminDep) -> dict:
    return await _store_document(collection, body.title, body.text, admin, body.acl)


@app.put("/v1/collections/{collection}/documents/{doc_id}/acl")
async def set_document_acl(collection: str, doc_id: str, body: AclUpdate, admin: AdminDep) -> dict:
    """Replace a document's access list; [] makes it inherit the collection's access again."""
    try:
        changed = rag.set_document_acl(collection, doc_id, body.principals)
    except rag.RagError as e:
        raise HTTPException(400, str(e)) from e
    if changed is None:
        raise HTTPException(404, "no such document in this collection")
    before, after = changed
    audit.append(
        "document_acl_changed",
        {"collection": collection, "doc_id": doc_id, "before": before, "after": after, "by": admin.label},
    )
    return {"collection": collection, "doc_id": doc_id, "acl": after}


@app.get("/admin/collections/{collection}/acl")
async def get_collection_acl(collection: str, _a: AdminDep) -> dict:
    try:
        rag.check_collection(collection)
    except rag.RagError as e:
        raise HTTPException(400, str(e)) from e
    return {"collection": collection, "principals": rag.collection_acl(collection)}


@app.put("/admin/collections/{collection}/acl")
async def set_collection_acl(collection: str, body: AclUpdate, admin: AdminDep) -> dict:
    """Replace a collection's access list; [] removes it (GATEWAY_COLLECTION_DEFAULT_ACCESS applies)."""
    try:
        before, after = rag.set_collection_acl(collection, body.principals, admin.label, _now())
    except rag.RagError as e:
        raise HTTPException(400, str(e)) from e
    audit.append(
        "collection_acl_changed", {"collection": collection, "before": before, "after": after, "by": admin.label}
    )
    return {"collection": collection, "principals": after}


@app.delete("/v1/collections/{collection}/documents/{doc_id}")
async def remove_document(collection: str, doc_id: str, admin: AdminDep) -> dict:
    try:
        removed = rag.delete_document(collection, doc_id)
    except rag.RagError as e:
        raise HTTPException(400, str(e)) from e
    if not removed:
        raise HTTPException(404, "no such document in this collection")
    audit.append(
        "document_removed", {"collection": collection, "doc_id": doc_id, "title": removed["title"], "by": admin.label}
    )
    return {"status": "removed", "doc_id": doc_id}


@app.post("/v1/collections/{collection}/ask")
async def ask(collection: str, body: AskRequest, key: KeyDep) -> dict:
    """Answer from the collection's documents only, with numbered citations."""
    try:
        rag.check_collection(collection)
    except rag.RagError as e:
        raise HTTPException(400, str(e)) from e
    if settings.GATEWAY_RETRIEVAL_MODE != "bm25":
        await _model_allowed(settings.GATEWAY_EMBED_MODEL)
    access = rag.access_for(key, collection)  # decided before any passage is loaded or scored
    if not access.doc_ids:
        if access.hidden:  # documents exist that this caller may not read: record the denial
            audit.append(
                "document_question_denied", {"key": key.label, "collection": collection, "access": access.audit()}
            )
        raise HTTPException(404, f"collection {collection!r} has no documents yet")
    try:
        sources = await rag.retrieve(collection, body.question, body.top_k, access)
    except (rag.RagError, httpx.HTTPError) as e:
        raise _rag_error(e) from e
    except crypto.CryptoError as e:  # missing or wrong key, or altered ciphertext: fail closed, say why
        log.error("document decryption failed in %s: %s", collection, e)
        raise HTTPException(500, "stored documents could not be decrypted; check the encryption keyring") from e
    if not sources:
        raise HTTPException(404, f"collection {collection!r} has no documents yet")

    model = body.model or settings.GATEWAY_DEFAULT_MODEL
    model_status = await _model_allowed(model)
    params = ChatParams(model=model, messages=rag.build_messages(body.question, sources), temperature=0.1)
    try:
        result = await get_backend().chat(params)
    except (BackendHTTPError, httpx.HTTPError) as e:
        raise _rag_error(e) from e
    answer = result.content
    tokens = result.prompt_tokens + result.completion_tokens
    record_principal_usage(key, tokens)
    metrics.record_tokens(key.metrics_label, model, result.prompt_tokens, result.completion_tokens)
    cited = rag.cited_numbers(answer)
    flags = {s.n: rag.injection_signals(s.text) for s in sources}

    # Always record who asked which collection, which passages were retrieved, and which the answer cited.
    entry = {
        "key": key.label,
        "collection": collection,
        "model": model,
        **({"model_verification": model_status} if model_status else {}),
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
    }
    if settings.GATEWAY_LOG_PROMPTS:
        if settings.GATEWAY_REDACT_PROMPTS:
            entry["question"], _ = redact(body.question)
            entry["answer"], _ = redact(answer)
        else:
            entry["question"], entry["answer"] = body.question, answer
    audit.append("document_question", entry)

    return {
        "answer": answer,
        "model": model,
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
                "excerpt": s.text[:400],
            }
            for s in sources
        ],
        "retrieval": rag.retrieval_config(),
        "usage": {"total_tokens": tokens},
    }


@app.post("/v1/embeddings")
async def embeddings(body: EmbeddingsRequest, key: UserDep) -> dict:
    inputs = [body.input] if isinstance(body.input, str) else body.input
    if not inputs or len(inputs) > 256:
        raise HTTPException(400, "send between 1 and 256 inputs")
    await _model_allowed(body.model or settings.GATEWAY_EMBED_MODEL)
    try:
        vectors = await rag.embed(inputs, model=body.model)
    except (BackendHTTPError, httpx.HTTPError) as e:
        raise _rag_error(e) from e
    record_principal_usage(key, 0)
    return {
        "object": "list",
        "model": body.model or settings.GATEWAY_EMBED_MODEL,
        "data": [{"object": "embedding", "index": i, "embedding": v.tolist()} for i, v in enumerate(vectors)],
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


# ---------- Admin UI ----------


@app.get("/admin")
async def admin_page():
    html = Path(__file__).resolve().parent.parent / "admin-ui" / "index.html"
    return FileResponse(str(html), media_type="text/html")
