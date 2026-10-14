"""High-level Claude chat client built on top of the Cloudflare proxy.

Ports Clicky's `ClaudeAPI.analyzeImageStreaming` + `parsePointingCoordinates`:

* every turn sends ALL monitors as labeled JPEG images, cursor screen first;
* the previous exchanges are sent as plain text (no old screenshots), capped
  at `CONVERSATION_HISTORY_MAX_EXCHANGES`;
* the reply is streamed over SSE and `[POINT:...]` tags are parsed out of
  the final text so they are never spoken.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Callable, Iterable, Iterator, Sequence

from ..models.message import CapturedScreen, Message, PointMarker, Role
from ..utils.constants import (
    CONVERSATION_HISTORY_MAX_EXCHANGES,
    DEFAULT_SYSTEM_PROMPT,
    POINT_TAG_PATTERN,
)
from ..utils.logger import get_logger
from .cloudflare_proxy import CloudflareProxy

log = get_logger(__name__)
_POINT_RE = re.compile(POINT_TAG_PATTERN)
# Collapses the double spaces left behind when a tag is removed mid-sentence.
_WHITESPACE_RUN_RE = re.compile(r"[ \t]{2,}")


class ClaudeClient:
    """Sends chat turns to the Worker and parses pointing tags from replies."""

    def __init__(
        self,
        proxy: CloudflareProxy,
        model: str,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_tokens: int = 2048,
        history_max_exchanges: int = CONVERSATION_HISTORY_MAX_EXCHANGES,
    ) -> None:
        self.proxy = proxy
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.history_max_exchanges = history_max_exchanges

    def apply_model(self, new_model: str) -> None:
        """Hot-swap the model used for subsequent /chat requests."""
        cleaned = (new_model or "").strip()
        if cleaned and cleaned != self.model:
            log.info("Claude model: %s -> %s", self.model, cleaned)
            self.model = cleaned

    # ----- streaming -----
    def send_stream(
        self,
        history: Iterable[Message],
        screens: Sequence[CapturedScreen] = (),
    ) -> Iterator[str]:
        """Yield text deltas as Claude responds via SSE."""
        payload = self.build_payload(history, screens)
        for sse_event in self.proxy.chat_stream(payload):
            text = self.extract_text_delta(sse_event.event, sse_event.data)
            if text:
                yield text

    def stream_full_reply(
        self,
        history: Iterable[Message],
        screens: Sequence[CapturedScreen] = (),
        on_partial: Callable[[str], None] | None = None,
        should_continue: Callable[[], bool] | None = None,
    ) -> Message:
        """Drive the SSE stream to completion and return the finished Message.

        `on_partial` receives the *spoken* text so far (POINT tags hidden) so
        the panel never flashes raw tags. `should_continue` lets the manager
        abandon a stream when the user interrupts with a new push-to-talk.
        """
        chunks: list[str] = []
        for delta in self.send_stream(history, screens=screens):
            if should_continue is not None and not should_continue():
                log.info("Claude stream abandoned (turn superseded)")
                break
            chunks.append(delta)
            if on_partial is not None:
                try:
                    on_partial(strip_point_tags_for_display("".join(chunks)))
                except Exception:
                    log.exception("on_partial callback raised")
        return parse_reply("".join(chunks))

    # ----- payload construction -----
    def build_payload(
        self,
        history: Iterable[Message],
        screens: Sequence[CapturedScreen] = (),
    ) -> dict:
        conversation = [m for m in history if m.role != Role.SYSTEM]
        # Keep only the last N exchanges (+ the new user turn). Anthropic
        # requires the first message to be from the user, so trim from the
        # front until it is.
        max_messages = self.history_max_exchanges * 2 + 1
        conversation = conversation[-max_messages:]
        while conversation and conversation[0].role != Role.USER:
            conversation = conversation[1:]
        messages = [m.to_api_dict() for m in conversation]

        if screens and messages and messages[-1]["role"] == "user":
            content_blocks: list[dict] = []
            for screen in screens:
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": screen.media_type,
                        "data": base64.b64encode(screen.image_bytes).decode("ascii"),
                    },
                })
                content_blocks.append({"type": "text", "text": screen.label})
            content_blocks.append({"type": "text", "text": messages[-1]["content"]})
            messages[-1] = {"role": "user", "content": content_blocks}
        return {
            "model": self.model,
            "system": self.system_prompt,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }

    # ----- response parsing -----
    @staticmethod
    def extract_text_delta(event_name: str, data_payload: str) -> str:
        """Pull text from one Anthropic SSE event, or `""` if it has none.

        Only `content_block_delta` events whose delta is a `text_delta`
        carry spoken text. Thinking deltas, pings and lifecycle events are
        ignored. A stream-level `error` event is raised so the manager can
        show the real reason (overloaded, credit balance, ...).
        """
        if not data_payload or data_payload == "[DONE]":
            return ""
        try:
            obj = json.loads(data_payload)
        except json.JSONDecodeError:
            return ""
        if not isinstance(obj, dict):
            return ""
        if obj.get("type") == "error":
            error_info = obj.get("error") or {}
            message = error_info.get("message") if isinstance(error_info, dict) else str(error_info)
            raise RuntimeError(f"Claude stream error: {message or error_info}")
        if obj.get("type") == "content_block_delta":
            delta = obj.get("delta") or {}
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                return delta.get("text", "") or ""
        return ""


def parse_reply(text: str) -> Message:
    """Split Claude's raw reply into spoken text + POINT markers."""
    points: list[PointMarker] = []
    explicitly_no_point = False
    for match in _POINT_RE.finditer(text):
        x_raw, y_raw, label_raw, screen_raw = match.groups()
        if x_raw is None or y_raw is None:
            explicitly_no_point = True
            continue
        points.append(PointMarker(
            x=int(x_raw),
            y=int(y_raw),
            label=(label_raw or "").strip(),
            screen_index=int(screen_raw) if screen_raw else None,
        ))
    spoken_text = _clean_spoken_text(_POINT_RE.sub("", text))
    return Message(
        role=Role.ASSISTANT,
        content=spoken_text,
        points=points,
        explicitly_no_point=explicitly_no_point,
    )


def strip_point_tags_for_display(partial_text: str) -> str:
    """Hide complete tags AND a half-streamed trailing `[POINT...` fragment."""
    without_complete_tags = _POINT_RE.sub("", partial_text)
    fragment_start = without_complete_tags.rfind("[")
    if fragment_start != -1 and "]" not in without_complete_tags[fragment_start:]:
        candidate = without_complete_tags[fragment_start:]
        if "[POINT:".startswith(candidate[:7]) or candidate.startswith("[POINT"):
            without_complete_tags = without_complete_tags[:fragment_start]
    return _clean_spoken_text(without_complete_tags)


def _clean_spoken_text(text: str) -> str:
    return _WHITESPACE_RUN_RE.sub(" ", text).strip()
