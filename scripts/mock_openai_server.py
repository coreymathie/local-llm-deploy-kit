# Corey Mathie, 2026
"""
A tiny OpenAI-compatible server. It is NOT a model.

Two modes (MOCK_MODE):

- tokens (default): canned tokens ("tok0 tok1 ...") and 16-number embeddings. Use it to measure the
  gateway's own overhead (auth, rate limiting, SQLite usage accounting, audit, metrics) without a GPU:

    MOCK_TOKENS=64 MOCK_TOKEN_DELAY_MS=0 uvicorn scripts.mock_openai_server:app --port 8001
    BACKEND=openai_compatible OPENAI_COMPAT_BASE_URL=http://127.0.0.1:8001/v1 uvicorn gateway.main:app

- simulated: the browser demo's stand-ins (demo/engine.py), so document Q&A is meaningful without a
  model: embeddings are hashed bag-of-words vectors (512 numbers, not a neural model) and a document
  question built by gateway/rag.py is answered by quoting the best-matching sentences of its sources
  with [n] citations (extractive, nothing is generated). Other chat requests get a fixed sentence
  saying no model is running. `docker compose up` uses this mode (service `mock-llm`).

Endpoints: /v1/chat/completions (streaming and not), /v1/embeddings, /v1/models.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI(title="mock-openai-server")

MODEL = "mock-model"
EMBED_MODEL = "mock-embed"


def _simulated() -> bool:
    return os.environ.get("MOCK_MODE", "tokens") == "simulated"


async def _simulated_reply(body: dict) -> str:
    from demo.engine import DemoBackend  # imported lazily: tokens mode needs nothing from the repo
    from gateway.backends import ChatParams

    messages = body.get("messages") or [{"role": "user", "content": ""}]
    result = await DemoBackend().chat(ChatParams(model=body.get("model", MODEL), messages=messages))
    if result.content.startswith("Demo mode:"):
        return "Simulated backend: no language model is running, so there is nothing to generate for this prompt."
    return result.content


def _tokens() -> int:
    return int(os.environ.get("MOCK_TOKENS", "32"))


def _delay() -> float:
    return float(os.environ.get("MOCK_TOKEN_DELAY_MS", "0")) / 1000.0


@app.get("/v1/models")
async def models() -> dict:
    names = [MODEL, EMBED_MODEL] if _simulated() else [MODEL]
    return {"object": "list", "data": [{"id": name, "object": "model"} for name in names]}


@app.post("/v1/chat/completions")
async def chat(body: dict):
    n = min(_tokens(), body.get("max_tokens") or _tokens())
    prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in body.get("messages", []))
    words = [f"tok{i} " for i in range(n)]
    if _simulated():
        words = [w + " " for w in (await _simulated_reply(body)).split()]
        n = len(words)
    if not body.get("stream"):
        await asyncio.sleep(_delay() * n)
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": body.get("model", MODEL),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(words)}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": n, "total_tokens": prompt_tokens + n},
        }

    async def events():
        for w in words:
            if _delay():
                await asyncio.sleep(_delay())
            yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': w}}]})}\n\n"
        if (body.get("stream_options") or {}).get("include_usage"):
            usage = {"prompt_tokens": prompt_tokens, "completion_tokens": n, "total_tokens": prompt_tokens + n}
            yield f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/v1/embeddings")
async def embeddings(body: dict) -> dict:
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    data = []
    for i, text in enumerate(inputs):
        if _simulated():
            from demo.engine import hashed_embedding

            vector = hashed_embedding(str(text))
        else:
            digest = hashlib.sha256(str(text).encode()).digest()
            vector = [b / 255.0 for b in digest[:16]]
        data.append({"object": "embedding", "index": i, "embedding": vector})
    return {"object": "list", "model": body.get("model", MODEL), "data": data}
