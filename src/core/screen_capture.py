"""Multi-monitor screen capture with a `PIL.ImageGrab` fallback.

We prefer `mss` because it is ~5x faster than PIL on Windows and exposes the
virtual-screen layout directly. If `mss` raises (it occasionally fails on
machines with broken DXGI drivers or under remote-desktop sessions), we fall
back to `PIL.ImageGrab.grab(all_screens=True)` and crop to the requested
monitor manually.

Monitor numbering matches `utils.win32.enumerate_monitors`: 1-based, primary
first, then by (left, top). That is the same order we tell Claude about in the
system prompt so `screenN` tags round-trip cleanly.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import mss
from PIL import Image, ImageGrab

from ..utils.logger import get_logger
from ..utils.win32 import MonitorInfo, enumerate_monitors, get_cursor_position

log = get_logger(__name__)


@dataclass
class Screenshot:
    """Captured screenshot of a single monitor."""
    png_bytes: bytes
    width: int
    height: int
    monitor_index: int   # 1-based, matches MonitorInfo.index
    dpi_scale: float


class ScreenCapture:
    def list_monitors(self) -> list[MonitorInfo]:
        return enumerate_monitors()

    def capture_primary(self) -> Screenshot:
        return self.capture_monitor(1)

    def capture_at_cursor(self) -> Screenshot:
        """Capture whichever monitor the cursor is currently on."""
        cursor_x, cursor_y = get_cursor_position()
        monitors = enumerate_monitors()
        for monitor in monitors:
            if (
                monitor.left <= cursor_x < monitor.left + monitor.width
                and monitor.top <= cursor_y < monitor.top + monitor.height
            ):
                return self.capture_monitor(monitor.index)
        return self.capture_primary()

    def capture_monitor(self, index: int) -> Screenshot:
        monitors = enumerate_monitors()
        if index < 1 or index > len(monitors):
            raise IndexError(
                f"monitor {index} out of range (have {len(monitors)} attached)",
            )
        monitor = monitors[index - 1]
        try:
            return self._capture_via_mss(monitor)
        except Exception:
            log.exception("mss capture failed; falling back to PIL.ImageGrab")
            return self._capture_via_pil(monitor)

    # ----- backends -----
    def _capture_via_mss(self, monitor: MonitorInfo) -> Screenshot:
        region = {
            "left": monitor.left,
            "top": monitor.top,
            "width": monitor.width,
            "height": monitor.height,
        }
        with mss.mss() as sct:
            raw = sct.grab(region)
            image = Image.frombytes("RGB", raw.size, raw.rgb)
        return self._encode(image, monitor)

    def _capture_via_pil(self, monitor: MonitorInfo) -> Screenshot:
        # `all_screens=True` returns the full virtual desktop; we crop to the
        # requested monitor's rectangle.
        full = ImageGrab.grab(all_screens=True)
        crop_box = (
            monitor.left,
            monitor.top,
            monitor.left + monitor.width,
            monitor.top + monitor.height,
        )
        return self._encode(full.crop(crop_box), monitor)

    def _encode(self, image: Image.Image, monitor: MonitorInfo) -> Screenshot:
        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=True)
        log.debug(
            "Captured monitor %d (%dx%d, %d bytes, %.2fx DPI)",
            monitor.index, image.width, image.height, buf.tell(), monitor.dpi_scale,
        )
        return Screenshot(
            png_bytes=buf.getvalue(),
            width=image.width,
            height=image.height,
            monitor_index=monitor.index,
            dpi_scale=monitor.dpi_scale,
        )
