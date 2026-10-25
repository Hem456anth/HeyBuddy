"""Renders every ink mark kind offscreen, checks pixels, target clicks and
clearing. Also renders the notch in each state. Saves docs/ink-marks.png
when HEYBUDDY_SAVE_DOCS=1."""
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")
if sys.platform == "win32":
    pytest.skip("covered by --selfcheck on real Windows", allow_module_level=True)

from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.core.companion_manager import CompanionState  # noqa: E402
from src.core.scene_program import parse_reply_program, resolve_marks  # noqa: E402
from src.models.config import AppConfig  # noqa: E402
from src.models.message import CapturedScreen  # noqa: E402
from tests import fake_win32  # noqa: E402

PROGRAM = '''```buddy
say("all the marks")
ring(80, 60, 160, 70, "ring")
frame(320, 50, 220, 110, "frame")
highlight(80, 200, 260, 22)
underline(380, 230, 180)
arrow(80, 330, 300, 280, "arrow")
num(380, 300, 2)
write(420, 330, "note")
trace([[60, 420], [200, 380], [340, 440], [500, 400]])
target(560, 380, 140, 44, "continue")
```'''


def _render(window) -> QImage:
    image = QImage(window.size(), QImage.Format.Format_ARGB32)
    image.fill(QColor(32, 33, 36))
    painter = QPainter(image)
    window.render(painter)
    painter.end()
    return image


def _blue(image: QImage, x0=0, y0=0, x1=None, y1=None) -> int:
    x1 = x1 or image.width()
    y1 = y1 or image.height()
    count = 0
    for y in range(y0, min(y1, image.height()), 2):
        for x in range(x0, min(x1, image.width()), 2):
            color = image.pixelColor(x, y)
            if color.blue() > 150 and color.blue() > color.red() + 60:
                count += 1
    return count


def _tinted(image: QImage, x0, y0, x1, y1) -> int:
    count = 0
    for y in range(y0, y1, 2):
        for x in range(x0, x1, 2):
            color = image.pixelColor(x, y)
            if color.blue() > color.red() + 40:
                count += 1
    return count


def test_ink_marks_render_click_and_clear():
    app = QApplication.instance() or QApplication([])
    geometry = app.primaryScreen().geometry()
    width, height = geometry.width(), geometry.height()
    fake_win32.FAKE_MONITORS[:] = [fake_win32.MonitorInfo(1, 0, 0, width, height, 1.0, True)]
    fake_win32.FAKE_CURSOR[:] = [width - 5, height - 5]
    from src.ui.overlay_window import BuddyOverlayController

    config = AppConfig()
    config.show_cursor_buddy = False
    controller = BuddyOverlayController(config)
    window = controller._windows[0]
    screen = CapturedScreen(b"", "image/jpeg", "s", 1, True, width, height, 0, 0, width, height, 1.0)
    program = resolve_marks(parse_reply_program(PROGRAM), [screen])
    assert len(program.beats[0].marks) == 9
    controller.show_beat(program.beats[0])
    clicked = []
    controller.target_clicked.connect(clicked.append)

    def frames(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            controller._on_frame()
            app.processEvents()
            time.sleep(0.016)

    frames(2.2)
    image = _render(window)
    assert _blue(image, 70, 50, 250, 140) > 20, "ring"
    assert _blue(image, 310, 40, 550, 170) > 20, "frame"
    assert _tinted(image, 80, 200, 340, 222) > 50, "highlight"
    assert _blue(image, 540, 360, 720, 440) > 20, "target"
    if os.environ.get("HEYBUDDY_SAVE_DOCS") == "1":
        image.copy(0, 0, 760, 470).save(str(Path(__file__).resolve().parent.parent / "docs" / "ink-marks.png"))

    # Click (press, then release) inside the target continues the walkthrough.
    fake_win32.FAKE_CURSOR[:] = [630, 400]
    fake_win32.FAKE_MOUSE_DOWN[0] = True
    frames(0.05)
    fake_win32.FAKE_MOUSE_DOWN[0] = False
    frames(0.05)
    assert clicked == ["continue"]
    frames(0.7)
    assert not controller.ink.has_marks, "click clears the drawing"

    # A new question wipes old marks.
    controller.show_beat(program.beats[0])
    frames(0.2)
    controller.set_companion_state(CompanionState.LISTENING)
    frames(0.7)
    assert not controller.ink.has_marks
    controller.shutdown()


def test_notch_states_render():
    app = QApplication.instance() or QApplication([])
    from src.ui.notch_window import NotchWindow

    notch = NotchWindow()
    notch.show()
    sizes = {}
    for state in (CompanionState.IDLE, CompanionState.LISTENING, CompanionState.PROCESSING, CompanionState.RESPONDING):
        notch.set_state(state)
        notch.set_level(0.8)
        notch.set_caption("where is the export button")
        end = time.monotonic() + 0.6
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.016)
        sizes[state] = tuple(round(v) for v in notch._size)
    assert sizes[CompanionState.IDLE] == (60, 8)
    assert sizes[CompanionState.LISTENING][0] > 300
    notch.close()
