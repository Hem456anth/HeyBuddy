"""Chat message data model."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


@dataclass
class PointMarker:
    """A `[POINT:x,y:label:screenN]` tag parsed from a Claude response.

    Coordinates are in the target monitor's *logical* pixel space
    (post-DPI scale). The overlay converts to physical pixels via
    `utils.win32.logical_to_physical_on_monitor` before driving the cursor.
    """
    x: int
    y: int
    label: str
    # 1-based monitor index. Defaults to 1 (primary) when Claude omits the
    # trailing `:screenN` segment so older / single-monitor tags still work.
    screen_index: int = 1


@dataclass
class Message:
    role: Role
    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    points: list[PointMarker] = field(default_factory=list)
    # Optional screenshot attached to this turn (PNG bytes)
    screenshot: bytes | None = None

    def to_api_dict(self) -> dict[str, Any]:
        """Anthropic-style message dict (no screenshot inlined here)."""
        return {"role": self.role.value, "content": self.content}
