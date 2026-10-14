"""Multi-monitor screen capture for Claude (port of Clicky's
`CompanionScreenCaptureUtility.captureAllScreensAsJPEG`).

Every turn captures *all* monitors, cursor screen first, downscales each so
its longest edge is `SCREENSHOT_MAX_DIMENSION`, JPEG-encodes it and labels it
with its real pixel size. Claude answers `[POINT:x,y]` in that image space;
`CapturedScreen.image_point_to_physical` maps it back to the desktop.

HeyBuddy's own windows (the cursor overlay and the panel) are hidden from
capture with `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`, the Windows
counterpart of Clicky excluding its own windows from the ScreenCaptureKit
filter, so Claude never sees the buddy itself.

Backends: `mss` (fast GDI BitBlt) with a `PIL.ImageGrab` fallback for remote
desktop / odd driver setups. Both run fine off the Qt thread.
"""
from __future__ import annotations

import io
from typing import Sequence

import mss
from PIL import Image, ImageGrab

from ..models.message import CapturedScreen
from ..utils.constants import SCREENSHOT_JPEG_QUALITY, SCREENSHOT_MAX_DIMENSION
from ..utils.logger import get_logger
from ..utils.win32 import MonitorInfo, enumerate_monitors, get_cursor_position

log = get_logger(__name__)


def compute_downscaled_size(width: int, height: int, max_dimension: int) -> tuple[int, int]:
    """Aspect-preserving size whose longest edge is at most `max_dimension`."""
    if width <= 0 or height <= 0:
        return max(1, width), max(1, height)
    longest_edge = max(width, height)
    if longest_edge <= max_dimension:
        return width, height
    scale = max_dimension / float(longest_edge)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def order_monitors_cursor_first(
    monitors: Sequence[MonitorInfo],
    cursor_x: int,
    cursor_y: int,
) -> list[tuple[MonitorInfo, bool]]:
    """Return (monitor, is_cursor_screen) pairs with the cursor's screen first.

    Same ordering Clicky uses, so `screen1` in Claude's world is always the
    screen the user is looking at and an omitted `:screenN` means "here".
    """
    def contains_cursor(monitor: MonitorInfo) -> bool:
        return (
            monitor.left <= cursor_x < monitor.left + monitor.width
            and monitor.top <= cursor_y < monitor.top + monitor.height
        )

    flagged = [(monitor, contains_cursor(monitor)) for monitor in monitors]
    if not any(is_cursor for _, is_cursor in flagged) and flagged:
        # Cursor in a gap between monitors: treat the primary as focus.
        flagged = [(monitor, monitor.is_primary) for monitor, _ in flagged]
    # `sorted` is stable, so non-cursor screens keep enumeration order.
    return sorted(flagged, key=lambda pair: not pair[1])


def build_screen_label(screen_number: int, total_screens: int, is_cursor_screen: bool,
                       image_width: int, image_height: int) -> str:
    if total_screens == 1:
        base_label = "user's screen (cursor is here)"
    elif is_cursor_screen:
        base_label = (
            f"screen {screen_number} of {total_screens} - cursor is on this screen (primary focus)"
        )
    else:
        base_label = f"screen {screen_number} of {total_screens} - secondary screen"
    return f"{base_label} (image dimensions: {image_width}x{image_height} pixels)"


class ScreenCapture:
    def __init__(
        self,
        max_dimension: int = SCREENSHOT_MAX_DIMENSION,
        jpeg_quality: int = SCREENSHOT_JPEG_QUALITY,
    ) -> None:
        self.max_dimension = max_dimension
        self.jpeg_quality = jpeg_quality

    def list_monitors(self) -> list[MonitorInfo]:
        return enumerate_monitors()

    def capture_all_screens(self) -> list[CapturedScreen]:
        """Capture every monitor, cursor screen first, labeled for Claude."""
        monitors = enumerate_monitors()
        if not monitors:
            raise RuntimeError("No display available for capture")
        cursor_x, cursor_y = get_cursor_position()
        ordered = order_monitors_cursor_first(monitors, cursor_x, cursor_y)

        captured_screens: list[CapturedScreen] = []
        for monitor, is_cursor_screen in ordered:
            try:
                full_resolution_image = self._grab_monitor(monitor)
            except Exception:
                log.exception("Failed to capture monitor %d; skipping it", monitor.index)
                continue
            # Dense 1-based numbering even if an earlier monitor failed.
            position = len(captured_screens) + 1
            target_width, target_height = compute_downscaled_size(
                full_resolution_image.width, full_resolution_image.height, self.max_dimension,
            )
            if (target_width, target_height) != full_resolution_image.size:
                resized_image = full_resolution_image.resize(
                    (target_width, target_height), Image.Resampling.LANCZOS,
                )
            else:
                resized_image = full_resolution_image
            jpeg_buffer = io.BytesIO()
            resized_image.convert("RGB").save(
                jpeg_buffer, format="JPEG", quality=self.jpeg_quality, optimize=True,
            )
            captured_screens.append(CapturedScreen(
                image_bytes=jpeg_buffer.getvalue(),
                media_type="image/jpeg",
                label=build_screen_label(
                    position, len(ordered), is_cursor_screen, target_width, target_height,
                ),
                screen_number=position,
                is_cursor_screen=is_cursor_screen,
                image_width=target_width,
                image_height=target_height,
                monitor_left=monitor.left,
                monitor_top=monitor.top,
                monitor_width=monitor.width,
                monitor_height=monitor.height,
                dpi_scale=monitor.dpi_scale,
            ))
            log.debug(
                "Captured monitor %d as screen%d: %dx%d -> %dx%d (%d bytes)",
                monitor.index, position, monitor.width, monitor.height,
                target_width, target_height, jpeg_buffer.tell(),
            )
        if not captured_screens:
            raise RuntimeError("Every monitor failed to capture")
        return captured_screens

    # ----- backends -----
    def _grab_monitor(self, monitor: MonitorInfo) -> Image.Image:
        try:
            region = {
                "left": monitor.left,
                "top": monitor.top,
                "width": monitor.width,
                "height": monitor.height,
            }
            with mss.mss() as screen_grabber:
                raw_capture = screen_grabber.grab(region)
                return Image.frombytes("RGB", raw_capture.size, raw_capture.rgb)
        except Exception:
            log.exception("mss capture failed; falling back to PIL.ImageGrab")
            full_desktop = ImageGrab.grab(all_screens=True)
            # ImageGrab's (0,0) is the virtual desktop's top-left, which can
            # be negative in desktop coordinates when a monitor sits left of
            # or above the primary.
            virtual_left = min(0, monitor.left)
            virtual_top = min(0, monitor.top)
            for candidate in enumerate_monitors():
                virtual_left = min(virtual_left, candidate.left)
                virtual_top = min(virtual_top, candidate.top)
            crop_box = (
                monitor.left - virtual_left,
                monitor.top - virtual_top,
                monitor.left - virtual_left + monitor.width,
                monitor.top - virtual_top + monitor.height,
            )
            return full_desktop.crop(crop_box)
