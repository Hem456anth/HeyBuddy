from __future__ import annotations

import json


def openai_sse(*chunks: dict, done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def text_chunk(text: str, finish: str | None = None) -> dict:
    return {"id": "c1", "object": "chat.completion.chunk", "model": "anthropic/claude-sonnet-5.5",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]}


def tool_chunks(call_id: str, name: str, arguments: str) -> list[dict]:
    half = len(arguments) // 2
    return [
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                                            "function": {"name": name, "arguments": arguments[:half]}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments[half:]}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        name, data = "", ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        if name:
            events.append((name, json.loads(data)))
    return events
