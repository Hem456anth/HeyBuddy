"""LLM provider interface + Vercel AI Gateway implementation.

Vercel AI Gateway (verified against https://vercel.com/docs/ai-gateway/openai-compat, accessed 2026-10-25):
  base URL  https://ai-gateway.vercel.sh/v1
  auth      Authorization: Bearer <AI_GATEWAY_API_KEY>
  endpoint  POST /chat/completions  (OpenAI Chat Completions spec: stream, tools, image_url content parts)
  models    "creator/model" ids, e.g. anthropic/claude-sonnet-5.5
Streaming is SSE `data: {chat.completion.chunk}` lines ending with `data: [DONE]`.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .http import UpstreamError, timeout, with_retries


@dataclass
class LLMEvent:
    """Normalized stream event: kind is text | tool_call | done."""
    kind: str
    text: str = ""
    tool_call: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""


class LLMProvider(Protocol):
    def stream(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
               max_tokens: int) -> AsyncIterator[LLMEvent]: ...


class VercelGatewayProvider:
    name = "vercel-ai-gateway"

    def __init__(self, api_key: str, base_url: str, client: httpx.AsyncClient, timeout_seconds: float = 60.0) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout(timeout_seconds)

    async def stream(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     max_tokens: int) -> AsyncIterator[LLMEvent]:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

        async def attempt() -> httpx.Response:
            request = self._client.build_request("POST", f"{self._base_url}/chat/completions", json=body,
                                                 headers=headers, timeout=self._timeout)
            return await self._client.send(request, stream=True)

        response = await with_retries(attempt)
        try:
            if response.status_code != 200:
                raw = (await response.aread()).decode("utf-8", "replace")
                raise UpstreamError(self.name, response.status_code, _error_message(raw))
            pending: dict[int, dict[str, Any]] = {}
            finish = ""
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if "error" in chunk and not chunk.get("choices"):
                    raise UpstreamError(self.name, 502, _error_message(data))
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield LLMEvent("text", text=content)
                    for call in delta.get("tool_calls") or []:
                        index = int(call.get("index", 0))
                        slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if call.get("id"):
                            slot["id"] = call["id"]
                        function = call.get("function") or {}
                        if function.get("name"):
                            slot["name"] += function["name"]
                        if function.get("arguments"):
                            slot["arguments"] += function["arguments"]
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
            for index in sorted(pending):
                slot = pending[index]
                try:
                    arguments = json.loads(slot["arguments"]) if slot["arguments"].strip() else {}
                except json.JSONDecodeError:
                    arguments = {"_unparsed": slot["arguments"][:2000]}
                if not isinstance(arguments, dict):
                    arguments = {"_value": arguments}
                yield LLMEvent("tool_call", tool_call={"id": slot["id"] or f"call_{index}", "name": slot["name"],
                                                       "arguments": arguments})
            yield LLMEvent("done", finish_reason=finish or ("tool_calls" if pending else "stop"))
        finally:
            await response.aclose()


def _error_message(raw: str) -> str:
    try:
        payload = json.loads(raw)
        error = payload.get("error", payload)
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or error)[:300]
        return str(error)[:300]
    except (json.JSONDecodeError, AttributeError):
        return raw[:300]
