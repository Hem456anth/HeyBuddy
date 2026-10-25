"""Request schemas (pydantic v2): strict sizes, no unknown roles, no client-supplied system role."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_IMAGE_DATA_URL = 6_000_000  # ~4.5 MB of base64 JPEG
_BASE64 = re.compile(r"[A-Za-z0-9+/=]+")


class ImageURL(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=MAX_IMAGE_DATA_URL)
    detail: Literal["auto", "low", "high"] | None = None

    @field_validator("url")
    @classmethod
    def only_inline_images(cls, value: str) -> str:
        prefix = re.match(r"^data:image/(jpeg|png|webp);base64,", value)
        if not prefix or not _BASE64.fullmatch(value[prefix.end():]):
            raise ValueError("images must be inline data:image/(jpeg|png|webp);base64 URLs")
        return value


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text", "image_url"]
    text: str | None = Field(default=None, max_length=40_000)
    image_url: ImageURL | None = None


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=64)
    arguments: str = Field(max_length=60_000)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(max_length=128)
    type: Literal["function"] = "function"
    function: FunctionCall


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    tool_calls: list[ToolCall] | None = Field(default=None, max_length=16)
    tool_call_id: str | None = Field(default=None, max_length=128)

    @field_validator("content")
    @classmethod
    def content_size(cls, value: Any) -> Any:
        if isinstance(value, str) and len(value) > 60_000:
            raise ValueError("message too long")
        if isinstance(value, list) and len(value) > 12:
            raise ValueError("too many content parts")
        return value


class FunctionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = Field(default="", max_length=2000)
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not TOOL_NAME.match(value):
            raise ValueError("bad tool name")
        if value.startswith("cx_"):
            raise ValueError("the cx_ prefix is reserved for server integrations")
        return value


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["function"] = "function"
    function: FunctionSpec


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(default=None, max_length=100)
    system: str = Field(default="", max_length=30_000)
    messages: list[Message] = Field(min_length=1, max_length=60)
    tools: list[ToolSpec] = Field(default_factory=list, max_length=30)
    integrations: bool = True
    max_tokens: int = Field(default=2048, ge=16, le=32_000)


class TTSRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    voice: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pairing_secret: str = Field(min_length=1, max_length=512)
    device_name: str = Field(default="phone", max_length=60)


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    toolkit: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: str = Field(max_length=64)
    arguments: dict[str, Any]
    confirmed: bool = False
