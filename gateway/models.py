# Corey Mathie, 2026
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

GROUP_RE = re.compile(r"^[^\s,]{1,128}$")  # IdP group names: paths, GUIDs, names; no spaces or commas


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = False
    stream_options: dict | None = None  # OpenAI-style; {"include_usage": true} adds a final usage chunk


class ApiKey(BaseModel):
    key: str
    label: str
    is_admin: bool = False
    created_at: str
    revoked_at: str | None = None
    requests_total: int = 0
    tokens_total: int = 0
    groups: list[str] = Field(default_factory=list)  # used by document and collection ACLs


class ApiKeyCreate(BaseModel):
    label: str
    is_admin: bool = False
    groups: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("groups")
    @classmethod
    def _check_groups(cls, groups: list[str]) -> list[str]:
        for g in groups:
            if not GROUP_RE.match(g):
                raise ValueError(f"invalid group name {g!r}")
        return sorted(set(groups))
