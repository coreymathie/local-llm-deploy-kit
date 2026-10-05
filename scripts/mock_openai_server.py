# Corey Mathie, 2026
"""
A tiny OpenAI-compatible server that returns canned tokens. It is NOT a model.

Use it to measure the gateway's own overhead (auth, rate limiting, SQLite usage
accounting, audit, metrics) without a GPU, or to try BACKEND=openai_compatible
end to end:

    MOCK_TOKENS=64 MOCK_TOKEN_DELAY_MS=0 uvicorn scripts.mock_openai_server:app --port 8001
    BACKEND=openai_compatible OPENAI_COMPAT_BASE_URL=http://127.0.0.1:8001/v1 uvicorn gateway.main:app

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


def _tokens() -> int:
    return int(os.environ.get("MOCK_TOKENS", "32"))


def _delay() -> float:
    return float(os.environ.get("MOCK_TOKEN_DELAY_MS", "0")) / 1000.0


@app.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(body: dict):
    n = min(_tokens(), body.get("max_tokens") or _tokens())
    prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in body.get("messages", []))
    words = [f"tok{i} " for i in range(n)]
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
        digest = hashlib.sha256(str(text).encode()).digest()
        data.append({"object": "embedding", "index": i, "embedding": [b / 255.0 for b in digest[:16]]})
    return {"object": "list", "model": body.get("model", MODEL), "data": data}
