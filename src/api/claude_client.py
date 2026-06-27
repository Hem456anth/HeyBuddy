"""High-level Claude chat client built on top of the Cloudflare proxy."""
from __future__ import annotations

import base64
import json
import re
from typing import Iterable

from ..models.message import Message, PointMarker, Role
from ..utils.constants import DEFAULT_SYSTEM_PROMPT, POINT_TAG_PATTERN
from ..utils.logger import get_logger
from .cloudflare_proxy import CloudflareProxy

log = get_logger(__name__)
_POINT_RE = re.compile(POINT_TAG_PATTERN)


class ClaudeClient:
    """Sends chat turns to the Worker and parses pointing tags from replies."""

    def __init__(
        self,
        proxy: CloudflareProxy,
        model: str,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_tokens: int = 1024,
    ) -> None:
        self.proxy = proxy
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens

    def apply_model(self, new_model: str) -> None:
        """Hot-swap the model used for subsequent /chat requests.

        Settings dialog calls this after the user picks a different
        Claude model from the dropdown so the change takes effect on
        the next turn without restarting the app. In-flight streams
        finish on the previous model — there's no way to retarget a
        running SSE stream without canceling and re-issuing.
        """
        cleaned = (new_model or "").strip()
        if cleaned and cleaned != self.model:
            log.info("Claude model: %s -> %s", self.model, cleaned)
            self.model = cleaned

    def send(self, history: Iterable[Message], screenshot: bytes | None = None) -> Message:
        """Send a turn synchronously; return the assistant Message."""
        payload = self._build_payload(history, screenshot)
        resp = self.proxy.chat(payload, stream=False)
        resp.raise_for_status()
        text = self._extract_text(resp.json())
        return self._build_reply(text)

    def send_stream(
        self,
        history: Iterable[Message],
        screenshot: bytes | None = None,
    ) -> Iterable[str]:
        """Yield incremental text deltas as Claude responds via SSE.

        The Worker forwards Anthropic's SSE verbatim, so we see the full
        Anthropic event taxonomy (`message_start`, `content_block_start`,
        `content_block_delta`, `content_block_stop`, `message_delta`,
        `message_stop`, `ping`, `error`). We only care about
        `content_block_delta` events whose delta is a `text_delta`.
        """
        payload = self._build_payload(history, screenshot)
        for sse_event in self.proxy.chat_stream(payload):
            text = self._extract_text_delta(sse_event.event, sse_event.data)
            if text:
                yield text

    def stream_full_reply(
        self,
        history: Iterable[Message],
        screenshot: bytes | None = None,
        on_partial: "Iterable[str] | None" = None,  # noqa: F821 — docs only
    ) -> Message:
        """Drive the SSE stream to completion and return the finished Message.

        `on_partial`, if provided as a callable, is invoked with each text
        delta so the UI can show response text as it arrives. We intentionally
        accumulate the full text before parsing POINT tags rather than
        emitting them incrementally — tags arrive as a single delta in
        practice and partial-tag parsing is brittle.
        """
        from typing import Callable
        chunks: list[str] = []
        callable_partial: Callable[[str], None] | None = (
            on_partial if callable(on_partial) else None
        )
        for delta in self.send_stream(history, screenshot=screenshot):
            chunks.append(delta)
            if callable_partial is not None:
                try:
                    callable_partial(delta)
                except Exception:
                    log.exception("on_partial callback raised")
        full_text = "".join(chunks)
        return self._build_reply(full_text)

    # ----- payload construction -----
    def _build_payload(
        self,
        history: Iterable[Message],
        screenshot: bytes | None,
    ) -> dict:
        messages = [m.to_api_dict() for m in history if m.role != Role.SYSTEM]
        if screenshot and messages and messages[-1]["role"] == "user":
            # Attach image to the latest user turn using Anthropic content blocks
            last = messages[-1]
            messages[-1] = {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(screenshot).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": last["content"]},
                ],
            }
        return {
            "model": self.model,
            "system": self.system_prompt,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }

    # ----- response parsing -----
    @staticmethod
    def _extract_text(payload: dict) -> str:
        # Anthropic shape: {"content": [{"type":"text","text":"..."}], ...}
        content = payload.get("content")
        if isinstance(content, list):
            return "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        # Fallbacks for proxies that flatten the response
        return payload.get("text") or payload.get("reply") or ""

    @staticmethod
    def _extract_text_delta(event_name: str, data_payload: str) -> str:
        """Pull text from a single Anthropic SSE event, or `""` if none.

        Anthropic event taxonomy we care about:

        * `content_block_delta` with `delta.type == "text_delta"` → real text
        * everything else (`ping`, `message_start`, `content_block_start`,
          `content_block_stop`, `message_delta`, `message_stop`, `error`) →
          no text to emit at this stage; the caller may track them later.
        """
        if not data_payload or data_payload == "[DONE]":
            return ""
        try:
            obj = json.loads(data_payload)
        except json.JSONDecodeError:
            # Some intermediaries strip the JSON shape and pass raw text;
            # be tolerant rather than dropping the chunk.
            return data_payload if event_name == "content_block_delta" else ""
        if not isinstance(obj, dict):
            return ""
        # Surface error events loudly — the upstream client can decide how to
        # react, but silently swallowing them would mask credential issues.
        if obj.get("type") == "error":
            log.error("Anthropic stream error event: %s", obj.get("error"))
            return ""
        if obj.get("type") == "content_block_delta":
            delta = obj.get("delta") or {}
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                return delta.get("text", "") or ""
        return ""

    @staticmethod
    def _build_reply(text: str) -> Message:
        points: list[PointMarker] = []
        for m in _POINT_RE.finditer(text):
            screen_raw = m.group(4)
            points.append(
                PointMarker(
                    x=int(m.group(1)),
                    y=int(m.group(2)),
                    label=m.group(3).strip(),
                    screen_index=int(screen_raw) if screen_raw else 1,
                )
            )
        # Strip tags so they don't get spoken aloud
        clean = _POINT_RE.sub("", text).strip()
        return Message(role=Role.ASSISTANT, content=clean, points=points)
