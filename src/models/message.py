"""Chat message + pointing data models."""
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


@dataclass(frozen=True)
class CapturedScreen:
    """One monitor's screenshot as it was shown to Claude for a single turn.

    Mirrors Clicky's `CompanionScreenCapture`. The important idea (and the
    main bug the first HeyBuddy port had) is that Claude answers in the
    *screenshot image's* pixel space, which is NOT the monitor's pixel
    space: we downscale to <=1280px. So every capture remembers both sizes
    and the monitor's physical rectangle, which is everything needed to turn
    `[POINT:x,y]` back into a real desktop position.
    """
    image_bytes: bytes
    media_type: str                 # "image/jpeg"
    label: str                      # text block sent right after the image
    screen_number: int              # 1-based, in the order sent to Claude
    is_cursor_screen: bool
    image_width: int                # pixels of the image Claude saw
    image_height: int
    monitor_left: int               # physical desktop pixels (per-monitor DPI aware)
    monitor_top: int
    monitor_width: int
    monitor_height: int
    dpi_scale: float = 1.0

    def image_point_to_physical(self, image_x: float, image_y: float) -> tuple[int, int]:
        """Map a coordinate in the screenshot image to physical desktop pixels."""
        clamped_x = max(0.0, min(float(image_x), float(self.image_width)))
        clamped_y = max(0.0, min(float(image_y), float(self.image_height)))
        scale_x = self.monitor_width / float(self.image_width or 1)
        scale_y = self.monitor_height / float(self.image_height or 1)
        physical_x = self.monitor_left + int(round(clamped_x * scale_x))
        physical_y = self.monitor_top + int(round(clamped_y * scale_y))
        # Keep the target strictly inside the monitor so the overlay that
        # owns this monitor is the one that draws it.
        physical_x = min(physical_x, self.monitor_left + self.monitor_width - 1)
        physical_y = min(physical_y, self.monitor_top + self.monitor_height - 1)
        return physical_x, physical_y


@dataclass
class PointMarker:
    """A `[POINT:x,y:label:screenN]` tag parsed from a Claude response.

    `x`/`y` are in the screenshot image's coordinate space. `screen_index`
    is the 1-based image number, or `None` when Claude omitted it (which per
    the prompt means "the cursor's screen"). `physical_x`/`physical_y` are
    filled in by `CompanionManager` once the marker is resolved against the
    captures of the turn that produced it.
    """
    x: int
    y: int
    label: str = ""
    screen_index: int | None = None
    physical_x: int | None = None
    physical_y: int | None = None

    @property
    def is_resolved(self) -> bool:
        return self.physical_x is not None and self.physical_y is not None


@dataclass
class Message:
    role: Role
    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    points: list[PointMarker] = field(default_factory=list)
    # True when Claude explicitly answered with [POINT:none].
    explicitly_no_point: bool = False

    def to_api_dict(self) -> dict[str, Any]:
        """Anthropic-style message dict (screenshots are attached separately)."""
        return {"role": self.role.value, "content": self.content}
