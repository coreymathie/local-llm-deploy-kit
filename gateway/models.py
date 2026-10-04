# Corey Mathie, 2026
from typing import Literal

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = False


class ApiKey(BaseModel):
    key: str
    label: str
    is_admin: bool = False
    created_at: str
    revoked_at: str | None = None
    requests_total: int = 0
    tokens_total: int = 0


class ApiKeyCreate(BaseModel):
    label: str
    is_admin: bool = False
