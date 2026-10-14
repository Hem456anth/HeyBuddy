"""Renders the buddy offscreen in each state and checks real pixels."""
import sys
import time

import pytest

pytest.importorskip("PyQt6.QtWidgets")
if sys.platform == "win32":
    pytest.skip("covered by --selfcheck on real Windows", allow_module_level=True)

from PyQt6.QtGui import QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.core.companion_manager import CompanionState  # noqa: E402
from src.models.config import AppConfig  # noqa: E402
from src.models.message import PointMarker  # noqa: E402
from tests import fake_win32  # noqa: E402


def _render(window) -> QImage:
    image = QImage(window.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    window.render(painter)
    painter.end()
    return image


def _blue_pixels(image: QImage) -> int:
    count = 0
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            color = image.pixelColor(x, y)
            if color.alpha() > 200 and color.blue() > 200 and color.red() < 110:
                count += 1
    return count


def test_buddy_states_render(tmp_path):
    app = QApplication.instance() or QApplication([])
    geometry = app.primaryScreen().geometry()
    fake_win32.FAKE_MONITORS[:] = [fake_win32.MonitorInfo(1, 0, 0, geometry.width(), geometry.height(), 1.0, True)]
    fake_win32.FAKE_CURSOR[:] = [200, 200]
    from src.ui.overlay_window import BuddyOverlayController

    controller = BuddyOverlayController(AppConfig())
    window = controller._windows[0]

    def frames(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            controller._on_frame()
            app.processEvents()
            time.sleep(0.016)

    frames(0.3)
    idle = _render(window)
    assert _blue_pixels(idle) > 10, "triangle should be drawn near the cursor"

    controller.set_companion_state(CompanionState.LISTENING)
    controller.set_audio_level(0.6)
    frames(0.1)
    listening = _render(window)

    controller.set_companion_state(CompanionState.PROCESSING)
    frames(0.1)
    processing = _render(window)

    controller.set_companion_state(CompanionState.RESPONDING)
    controller.show_pointing([PointMarker(0, 0, "save button", 1, physical_x=600, physical_y=500)])
    frames(1.8)
    pointing = _render(window)
    pose = controller.current_frame.pose
    assert pose.mode.value == "pointing"
    assert abs(pose.x - 608) < 1 and abs(pose.y - 512) < 1
    assert pose.bubble_text

    composite = QImage(geometry.width() * 2, geometry.height() * 2, QImage.Format.Format_ARGB32)
    composite.fill(0xFF202124)
    painter = QPainter(composite)
    for index, image in enumerate((idle, listening, processing, pointing)):
        painter.drawImage((index % 2) * geometry.width(), (index // 2) * geometry.height(), image)
    painter.end()
    composite.save(str(tmp_path / "buddy_states.png"))
    print("saved", tmp_path / "buddy_states.png")
    controller.shutdown()
