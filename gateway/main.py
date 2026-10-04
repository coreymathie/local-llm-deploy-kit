# Corey Mathie, 2026
"""
FastAPI gateway in front of Ollama.

- OpenAI-compatible /v1/chat/completions (streaming and non-streaming)
- /v1/models passthrough, /v1/pull/{model} (admin, runs in the background)
- /admin/keys for key management
- /admin/audit/verify and /admin/audit/recent for the tamper-evident audit log
- /v1/collections/... private document Q&A (retrieval over local files, cited answers)
- /v1/embeddings OpenAI-compatible embeddings from the local embedding model
- /admin serves the single-file dashboard
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import audit, rag
from .auth import require_admin, require_key
from .config import settings
from .logging_setup import log_prompt, setup_logging
from .models import ApiKey, ApiKeyCreate, ChatCompletionRequest
from .redact import redact
from .store import create_key, ensure_bootstrap_admin, init_db, list_keys, record_usage, revoke_key

setup_logging()
log = logging.getLogger("gateway")

KeyDep = Annotated[ApiKey, Depends(require_key)]
AdminDep = Annotated[ApiKey, Depends(require_admin)]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    rag.init_rag()
    created = ensure_bootstrap_admin()
    if created:
        audit.append("admin_bootstrap", {"label": "bootstrap admin"})
        log.info("=" * 64)
        log.info("Bootstrap admin API key: %s", created)
        log.info("Save it now. It will not be printed again.")
        log.info("=" * 64)
    yield


app = FastAPI(title="local-llm-deploy-kit", version="0.4.0", lifespan=lifespan)


def _mask(key: str) -> str:
    return key[:12] + "…" if len(key) > 12 else key


@app.get("/health")
async def health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{settings.OLLAMA_HOST}/api/tags")
        ollama_ok = r.status_code == 200
    except httpx.HTTPError:
        ollama_ok = False
    return {"status": "ok", "ollama": ollama_ok}


# ---------- OpenAI-compatible chat ----------


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, key: KeyDep):
    model = body.model or settings.GATEWAY_DEFAULT_MODEL
    payload = {
        "model": model,
        "messages": [m.model_dump() for m in body.messages],
        "stream": body.stream,
        "options": {"temperature": body.temperature},
    }
    if body.max_tokens:
        payload["options"]["num_predict"] = body.max_tokens

    if body.stream:
        return StreamingResponse(_stream_chat(payload, key, model), media_type="text/event-stream")

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            r = await client.post(f"{settings.OLLAMA_HOST}/api/chat", json=payload)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"model runtime unavailable: {e.__class__.__name__}") from e
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)

    data = r.json()
    content = data["message"]["content"]
    prompt_tokens = data.get("prompt_eval_count", 0)
    completion_tokens = data.get("eval_count", 0)
    record_usage(key.key, prompt_tokens + completion_tokens)
    log_prompt(key.label, model, json.dumps(payload["messages"]), content, prompt_tokens + completion_tokens)

    return {
        "id": f"chatcmpl-{data.get('created_at', '')}",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def _stream_chat(payload, key: ApiKey, model: str):
    tokens = 0
    full: list[str] = []
    async with (
        httpx.AsyncClient(timeout=None) as client,
        client.stream("POST", f"{settings.OLLAMA_HOST}/api/chat", json=payload) as r,
    ):
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            chunk = json.loads(line)
            piece = chunk.get("message", {}).get("content")
            if piece:
                full.append(piece)
                sse = {"choices": [{"delta": {"content": piece}, "index": 0, "finish_reason": None}], "model": model}
                yield f"data: {json.dumps(sse)}\n\n"
            if chunk.get("done"):
                tokens = chunk.get("eval_count", 0) + chunk.get("prompt_eval_count", 0)
    yield "data: [DONE]\n\n"
    record_usage(key.key, tokens)
    log_prompt(key.label, model, json.dumps(payload["messages"]), "".join(full), tokens)


# ---------- Models ----------


@app.get("/v1/models")
async def models(_key: KeyDep):
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{settings.OLLAMA_HOST}/api/tags")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"model runtime unavailable: {e.__class__.__name__}") from e
    return {"object": "list", "data": [{"id": m["name"], "object": "model"} for m in r.json().get("models", [])]}


async def _pull(model_name: str, requested_by: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            r = await client.post(f"{settings.OLLAMA_HOST}/api/pull", json={"name": model_name, "stream": False})
        status = "completed" if r.status_code == 200 else f"failed ({r.status_code})"
    except httpx.HTTPError as e:
        status = f"failed ({e.__class__.__name__})"
    audit.append("model_pull_finished", {"model": model_name, "requested_by": requested_by, "status": status})


@app.post("/v1/pull/{model_name:path}", status_code=202)
async def pull_model(model_name: str, background: BackgroundTasks, admin: AdminDep):
    """Start a model download and return immediately; large pulls take minutes."""
    audit.append("model_pull_started", {"model": model_name, "requested_by": admin.label})
    background.add_task(_pull, model_name, admin.label)
    return {"status": "started", "model": model_name}


# ---------- Admin: keys ----------


@app.get("/admin/keys")
async def admin_list_keys(_a: AdminDep) -> list[ApiKey]:
    return list_keys()


@app.post("/admin/keys")
async def admin_create_key(req: ApiKeyCreate, admin: AdminDep) -> ApiKey:
    created = create_key(label=req.label, is_admin=req.is_admin)
    audit.append(
        "key_created",
        {"label": req.label, "is_admin": req.is_admin, "key": _mask(created.key), "by": admin.label},
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
    if isinstance(e, httpx.HTTPError):
        return HTTPException(502, f"model runtime unavailable: {e.__class__.__name__}")
    raise e


async def _store_document(collection: str, title: str, text: str, admin: ApiKey) -> dict:
    try:
        doc = await rag.add_document(collection, title, text, admin.label, _now())
    except (rag.RagError, httpx.HTTPError) as e:
        raise _rag_error(e) from e
    audit.append(
        "document_added",
        {"collection": collection, "doc_id": doc["id"], "title": title, "chunks": doc["chunks"], "by": admin.label},
    )
    return doc


@app.get("/v1/collections")
async def collections(_key: KeyDep) -> list[dict]:
    return rag.list_collections()


@app.get("/v1/collections/{collection}/documents")
async def collection_documents(collection: str, _key: KeyDep) -> list[dict]:
    try:
        return rag.list_documents(collection)
    except rag.RagError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/v1/collections/{collection}/documents", status_code=201)
async def upload_document(collection: str, admin: AdminDep, file: Annotated[UploadFile, File()]) -> dict:
    """Add a PDF, Markdown, or text file. Admin only: collections are curated knowledge bases."""
    limit = settings.GATEWAY_MAX_UPLOAD_MB * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"file is larger than {settings.GATEWAY_MAX_UPLOAD_MB} MB")
    try:
        text = rag.extract_text(file.filename or "upload.txt", data)
    except rag.RagError as e:
        raise HTTPException(400, str(e)) from e
    return await _store_document(collection, file.filename or "upload", text, admin)


@app.post("/v1/collections/{collection}/documents/text", status_code=201)
async def add_text_document(collection: str, body: TextDocument, admin: AdminDep) -> dict:
    return await _store_document(collection, body.title, body.text, admin)


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
        sources = await rag.retrieve(collection, body.question, body.top_k)
    except (rag.RagError, httpx.HTTPError) as e:
        raise _rag_error(e) from e
    if not sources:
        raise HTTPException(404, f"collection {collection!r} has no documents yet")

    model = body.model or settings.GATEWAY_DEFAULT_MODEL
    payload = {
        "model": model,
        "messages": rag.build_messages(body.question, sources),
        "stream": False,
        "options": {"temperature": 0.1},
    }
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            r = await client.post(f"{settings.OLLAMA_HOST}/api/chat", json=payload)
    except httpx.HTTPError as e:
        raise _rag_error(e) from e
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    data = r.json()
    answer = data["message"]["content"]
    tokens = data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
    record_usage(key.key, tokens)
    cited = rag.cited_numbers(answer)

    # Always record who asked which collection, which passages were retrieved, and which the answer cited.
    entry = {
        "key": key.label,
        "collection": collection,
        "model": model,
        "sources": [
            {"doc_id": s.doc_id, "chunk": s.chunk, "score": round(s.score, 4), "cited": s.n in cited} for s in sources
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
                "excerpt": s.text[:400],
            }
            for s in sources
        ],
        "usage": {"total_tokens": tokens},
    }


@app.post("/v1/embeddings")
async def embeddings(body: EmbeddingsRequest, key: KeyDep) -> dict:
    inputs = [body.input] if isinstance(body.input, str) else body.input
    if not inputs or len(inputs) > 256:
        raise HTTPException(400, "send between 1 and 256 inputs")
    try:
        vectors = await rag.embed(inputs, model=body.model)
    except httpx.HTTPError as e:
        raise _rag_error(e) from e
    record_usage(key.key, 0)
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
