from src.core.screen_capture import build_screen_label, compute_downscaled_size, order_monitors_cursor_first
from tests.fake_win32 import MonitorInfo


def test_downscale_keeps_aspect_and_limit():
    assert compute_downscaled_size(2880, 1800, 1280) == (1280, 800)
    assert compute_downscaled_size(1080, 1920, 1280) == (720, 1280)
    assert compute_downscaled_size(1024, 768, 1280) == (1024, 768)


def test_cursor_screen_first():
    left = MonitorInfo(1, 0, 0, 1920, 1080, 1.0, True)
    right = MonitorInfo(2, 1920, 0, 1920, 1080, 1.0, False)
    ordered = order_monitors_cursor_first([left, right], 2500, 500)
    assert [m.index for m, _ in ordered] == [2, 1]
    assert ordered[0][1] is True
    # Cursor in a gap: primary becomes the focus screen.
    gap = order_monitors_cursor_first([left, right], -50, -50)
    assert gap[0][0].index == 1 and gap[0][1] is True


def test_labels_match_clicky_wording():
    assert build_screen_label(1, 1, True, 1280, 800) == "user's screen (cursor is here) (image dimensions: 1280x800 pixels)"
    assert "primary focus" in build_screen_label(1, 2, True, 1280, 720)
    assert "secondary screen" in build_screen_label(2, 2, False, 1280, 720)
