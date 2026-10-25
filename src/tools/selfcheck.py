"""End-to-end self-check that runs on a real Windows desktop with no API keys.

`HeyBuddy.exe --selfcheck` (or `python -m src.main --selfcheck`) boots the
real app objects and then, against a local *mock* Cloudflare Worker:

1. verifies settings are written to the user data dir;
2. enumerates monitors and checks per-monitor DPI awareness;
3. captures every screen exactly as a real turn would (JPEG, <=1280 px);
4. sends a typed question through the real `CompanionManager` pipeline:
   Claude SSE streaming (mock), screenshot attachment, POINT parsing and
   coordinate resolution, TTS download + bundled-ffmpeg MP3 decode;
5. checks the blue buddy overlay windows are click-through/no-activate,
   flies the buddy to the POINT target, and proves with two real screen
   grabs that the buddy is drawn on screen but hidden from screen capture.

Results go to `<user data>/selfcheck/selfcheck.json` plus PNG evidence, and
the process exits 0 only when every required check passed. CI runs this on
the built .exe so "it builds" also means "it works".
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from ..core.audio_player import _CREATE_NO_WINDOW, decode_mp3_to_pcm16, resolve_ffmpeg_executable
from ..core.companion_manager import CompanionManager, CompanionState
from ..models.config import AppConfig
from ..models.message import PointMarker
from ..utils.constants import APP_VERSION, IS_FROZEN, SETTINGS_FILE, USER_DATA_DIR
from ..utils.logger import get_logger
from ..utils import win32

log = get_logger(__name__)

OUTPUT_DIR = USER_DATA_DIR / "selfcheck"


def _make_test_mp3() -> bytes:
    ffmpeg_executable = resolve_ffmpeg_executable()
    if not ffmpeg_executable:
        return b""
    completed = subprocess.run(
        [ffmpeg_executable, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=660:duration=0.6", "-ac", "1", "-f", "mp3", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_CREATE_NO_WINDOW,
        timeout=30, check=False,
    )
    return completed.stdout if completed.returncode == 0 else b""


def build_sse_events(reply: Any) -> list[tuple[str, dict]]:
    """Anthropic-style SSE for a mock reply.

    `reply` is either the reply text, or a dict
    `{"text": "...", "tool_use": {"id", "name", "input"}}` for a round that
    calls a tool (stop_reason tool_use).
    """
    text = reply if isinstance(reply, str) else str(reply.get("text", ""))
    tool_use = None if isinstance(reply, str) else reply.get("tool_use")
    events: list[tuple[str, dict]] = [("message_start", {"type": "message_start"})]
    if text:
        events.append(("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "text", "text": ""}}))
        # Split mid-tag on purpose to exercise partial-tag hiding.
        for start in range(0, len(text), 7):
            events.append(("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": text[start:start + 7]},
            }))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
    if tool_use:
        raw = json.dumps(tool_use.get("input", {}))
        events.append(("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
            "type": "tool_use", "id": tool_use.get("id", "toolu_selfcheck"), "name": tool_use["name"], "input": {}}}))
        for start in range(0, len(raw), 9):
            events.append(("content_block_delta", {"type": "content_block_delta", "index": 1,
                                                   "delta": {"type": "input_json_delta", "partial_json": raw[start:start + 9]}}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": 1}))
    events.append(("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": "tool_use" if tool_use else "end_turn"}}))
    events.append(("message_stop", {"type": "message_stop"}))
    return events


class _MockWorker:
    """Speaks the real Worker contract: /chat (SSE), /tts, /transcribe-token."""

    def __init__(self, reply_text_factory: Callable[[dict], str], mp3_bytes: bytes) -> None:
        self.reply_text_factory = reply_text_factory
        self.mp3_bytes = mp3_bytes
        self.chat_requests: list[dict] = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                if self.path == "/chat":
                    payload = json.loads(body or b"{}")
                    mock.chat_requests.append(payload)
                    reply_text = mock.reply_text_factory(payload)
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    events = build_sse_events(reply_text)
                    for name, data in events:
                        self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        time.sleep(0.01)
                elif self.path == "/tts":
                    self.send_response(200)
                    self.send_header("content-type", "audio/mpeg")
                    self.send_header("content-length", str(len(mock.mp3_bytes)))
                    self.end_headers()
                    self.wfile.write(mock.mp3_bytes)
                elif self.path == "/transcribe-token":
                    response = json.dumps({"token": "selfcheck"}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                else:
                    self.send_response(404)
                    self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, name="MockWorker", daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def _pump(qt_app: QApplication, seconds: float) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


def _wait_until(qt_app: QApplication, predicate: Callable[[], bool], timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        _pump(qt_app, 0.05)
    return predicate()


def _count_buddy_blue_pixels(image: Any) -> int:
    """Count pixels close to the buddy's #3380FF in a PIL image."""
    pixels = image.convert("RGB").getdata()
    return sum(1 for red, green, blue in pixels if blue > 200 and 20 <= red <= 110 and 90 <= green <= 170)


def run_selfcheck(
    qt_app: QApplication,
    config: AppConfig,
    manager: CompanionManager,
    overlay: Any,
    panel: Any,
    duration_seconds: float = 6.0,
    jobs: Any = None,
) -> int:
    import mss
    from PIL import Image

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"version": APP_VERSION, "frozen": IS_FROZEN, "checks": {}}
    required_failures: list[str] = []

    def record(name: str, passed: bool, detail: Any = None, required: bool = True) -> None:
        results["checks"][name] = {"passed": bool(passed), "required": required, "detail": detail}
        log.info("SELFCHECK %-28s %s %s", name, "PASS" if passed else ("FAIL" if required else "WARN"), detail)
        if required and not passed:
            required_failures.append(name)

    # 1. settings
    config.save()
    record("settings_written", SETTINGS_FILE.exists(), str(SETTINGS_FILE))

    # 2. monitors / DPI
    monitors = win32.enumerate_monitors()
    record("monitors_enumerated", len(monitors) >= 1,
           [f"{m.index}:{m.width}x{m.height}@{m.left},{m.top} x{m.dpi_scale:.2f}" for m in monitors])

    # 3. capture
    screens = []
    try:
        screens = manager.screen.capture_all_screens()
        record("screen_capture", bool(screens) and all(
            max(screen.image_width, screen.image_height) <= 1280 and screen.image_bytes[:2] == b"\xff\xd8"
            for screen in screens
        ), [screen.label for screen in screens])
    except Exception as capture_error:
        record("screen_capture", False, repr(capture_error))

    # 4. full pipeline through a mock Worker
    mp3_bytes = _make_test_mp3()
    try:
        decoded = decode_mp3_to_pcm16(mp3_bytes) if mp3_bytes else None
        record("ffmpeg_mp3_decode", decoded is not None and len(decoded) > 1000,
               f"{0 if decoded is None else len(decoded)} samples via {resolve_ffmpeg_executable()}")
    except Exception as decode_error:
        record("ffmpeg_mp3_decode", False, repr(decode_error))

    target_image_point: dict[str, int] = {}

    def reply_for(payload: dict) -> str:
        user_content = payload["messages"][-1]["content"]
        if isinstance(user_content, str):
            user_content = [{"type": "text", "text": user_content}]
        image_blocks = [block for block in user_content if isinstance(block, dict) and block.get("type") == "image"]
        first_label = next((block["text"] for block in user_content if block.get("type") == "text"), "")
        width, height = 200, 200
        if "image dimensions: " in first_label:
            width_text, height_text = first_label.split("image dimensions: ")[1].split(" pixels")[0].split("x")
            width, height = int(width_text), int(height_text)
        target_image_point["x"] = int(width * 0.5)
        target_image_point["y"] = int(height * 0.5)
        target_image_point["images"] = len(image_blocks)
        return (
            "the start button lives right in the middle here, give it a click. "
            f"[POINT:{target_image_point['x']},{target_image_point['y']}:self check]"
        )

    mock_worker = _MockWorker(reply_for, mp3_bytes)
    manager.proxy.apply_worker_url(mock_worker.url)
    pointing_events: list[list[PointMarker]] = []
    manager.pointing_requested.connect(lambda markers: pointing_events.append(markers))
    errors: list[str] = []
    manager.error_occurred.connect(errors.append)
    states_seen: list[str] = []
    manager.state_changed.connect(lambda state: states_seen.append(state.value))

    # Keep the buddy visible for the screenshots even if the user disabled it.
    config.show_cursor_buddy = True
    manager.send_text("where is the start button?", include_screenshot=True)
    got_point = _wait_until(qt_app, lambda: bool(pointing_events), 20.0)
    request = mock_worker.chat_requests[0] if mock_worker.chat_requests else {}
    record("claude_request_shape", bool(request) and request.get("stream") is True
           and target_image_point.get("images") == len(screens) and request.get("model") == config.model,
           {"model": request.get("model"), "images": target_image_point.get("images")})
    record("point_resolved", got_point, [
        (marker.label, marker.physical_x, marker.physical_y) for marker in (pointing_events[0] if pointing_events else [])
    ])
    history = manager.history
    record("spoken_text_has_no_tags", bool(history) and "[POINT" not in history[-1].content,
           history[-1].content if history else None)

    if pointing_events and screens:
        marker = pointing_events[0][0]
        cursor_screen = next(screen for screen in screens if screen.is_cursor_screen)
        expected_x = cursor_screen.monitor_left + cursor_screen.monitor_width // 2
        expected_y = cursor_screen.monitor_top + cursor_screen.monitor_height // 2
        record("point_maps_to_screen_center",
               abs(marker.physical_x - expected_x) <= 4 and abs(marker.physical_y - expected_y) <= 4,
               {"got": (marker.physical_x, marker.physical_y), "expected": (expected_x, expected_y)})

        # 5. overlay: wait until the buddy is parked at the target, then grab.
        landed = _wait_until(qt_app, lambda: overlay.current_frame is not None
                             and overlay.current_frame.pose.mode.value == "pointing", 8.0)
        record("buddy_flew_to_target", landed,
               None if overlay.current_frame is None else (overlay.current_frame.pose.x, overlay.current_frame.pose.y))
        _pump(qt_app, 0.6)
        window_reports = []
        for window in overlay._windows:  # noqa: SLF001 - diagnostics only
            hwnd = int(window.winId())
            ex_style = win32.get_window_ex_style(hwnd)
            window_reports.append({
                "monitor": window.monitor.index,
                "click_through": bool(ex_style & win32.WS_EX_TRANSPARENT),
                "no_activate": bool(ex_style & win32.WS_EX_NOACTIVATE),
                "tool_window": bool(ex_style & win32.WS_EX_TOOLWINDOW),
                "affinity": win32.get_window_display_affinity(hwnd),
            })
        record("overlay_window_styles", bool(window_reports) and all(
            report["click_through"] and report["no_activate"] and report["tool_window"] for report in window_reports
        ), window_reports)
        record("overlay_excluded_from_capture",
               all(report["affinity"] == win32.WDA_EXCLUDEFROMCAPTURE for report in window_reports),
               window_reports, required=False)

        try:
            pose = overlay.current_frame.pose if overlay.current_frame else None
            if pose is not None:
                region = {"left": int(pose.x) - 60, "top": int(pose.y) - 40, "width": 260, "height": 100}
                with mss.mss() as grabber:
                    hidden_grab = grabber.grab(region)
                    hidden_image = Image.frombytes("RGB", hidden_grab.size, hidden_grab.rgb)
                    for window in overlay._windows:  # noqa: SLF001
                        win32.set_window_display_affinity(int(window.winId()), win32.WDA_NONE)
                    _pump(qt_app, 0.3)
                    # Compositors can lag a frame or two behind an affinity
                    # change; take the best of a few grabs.
                    visible_image = None
                    for _attempt in range(4):
                        visible_grab = grabber.grab(region)
                        candidate = Image.frombytes("RGB", visible_grab.size, visible_grab.rgb)
                        if visible_image is None or _count_buddy_blue_pixels(candidate) > _count_buddy_blue_pixels(visible_image):
                            visible_image = candidate
                        _pump(qt_app, 0.15)
                    for window in overlay._windows:  # noqa: SLF001
                        win32.exclude_window_from_capture(int(window.winId()))
                    full_grab = grabber.grab(grabber.monitors[0])
                    Image.frombytes("RGB", full_grab.size, full_grab.rgb).save(OUTPUT_DIR / "desktop.png")
                hidden_image.save(OUTPUT_DIR / "buddy_region_capture_excluded.png")
                visible_image.save(OUTPUT_DIR / "buddy_region_visible.png")
                visible_blue = _count_buddy_blue_pixels(visible_image)
                hidden_blue = _count_buddy_blue_pixels(hidden_image)
                record("buddy_rendered_on_screen", visible_blue > 40, {"blue_pixels": visible_blue})
                record("buddy_hidden_from_capture", hidden_blue < max(10, visible_blue // 10),
                       {"blue_pixels_when_excluded": hidden_blue}, required=False)

        except Exception as grab_error:
            record("buddy_rendered_on_screen", False, repr(grab_error))

    record("pipeline_reached_idle_or_responding", _wait_until(
        qt_app, lambda: manager.state in (CompanionState.IDLE, CompanionState.RESPONDING), 15.0,
    ), {"states": states_seen, "errors": errors})
    record("no_pipeline_errors", not [error for error in errors if "Voice unavailable" not in error], errors)

    # 6. HeyBuddy 0.3: tool round + buddy program (ink + guided target)
    try:
        _check_program_and_tools(qt_app, config, manager, overlay, mock_worker, screens, record)
    except Exception as phase_error:
        log.exception("program/tools phase crashed")
        record("program_phase", False, repr(phase_error))

    # 7. background job with a file artifact
    if jobs is not None:
        try:
            _check_background_job(qt_app, jobs, mock_worker, record)
        except Exception as job_error:
            log.exception("job phase crashed")
            record("background_job_done", False, repr(job_error))

    _pump(qt_app, max(0.0, duration_seconds - 3.0))
    mock_worker.close()
    results["passed"] = not required_failures
    results["required_failures"] = required_failures
    (OUTPUT_DIR / "selfcheck.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    if sys.stdout is not None:  # None inside the windowed .exe
        print(json.dumps(results, indent=2))
        sys.stdout.flush()
    overlay.shutdown()
    return 0 if not required_failures else 1


def _image_size_from_payload(payload: dict) -> tuple[int, int]:
    for message in reversed(payload.get("messages") or []):
        content = message.get("content")
        if isinstance(content, str):
            continue
        for block in content:
            text = block.get("text", "") if isinstance(block, dict) else ""
            if "image dimensions: " in text:  # cursor screen is always first
                width_text, height_text = text.split("image dimensions: ")[1].split(" pixels")[0].split("x")
                return int(width_text), int(height_text)
    return 200, 200


def _has_tool_result(payload: dict) -> bool:
    last = payload["messages"][-1]["content"] if payload.get("messages") else ""
    return isinstance(last, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in last)


def _check_program_and_tools(qt_app, config, manager, overlay, mock_worker, screens, record) -> None:
    import mss
    from PIL import Image

    _wait_until(qt_app, lambda: manager.state == CompanionState.IDLE, 15.0)
    overlay.clear_ink()
    _pump(qt_app, 0.6)
    config.tools_enabled = True
    config.annotations_enabled = True
    config.annotation_linger_seconds = 30.0
    requests_before = len(mock_worker.chat_requests)
    geometry: dict[str, int] = {}

    def program_reply(payload: dict) -> Any:
        width, height = _image_size_from_payload(payload)
        cx, cy = width // 2, height // 2
        geometry.update(cx=cx, cy=cy, width=width, height=height)
        if not _has_tool_result(payload):
            return {"text": '```buddy\nsay("let me check which app you are in")\n```',
                    "tool_use": {"id": "toolu_window", "name": "get_active_window", "input": {}}}
        return (
            "```buddy\n"
            'step(1);\n'
            'say("this is the middle of your screen.");\n'
            f'ring({cx - 90}, {cy - 45}, 180, 90, "middle");\n'
            'say("click this box to continue.");\n'
            f'target({cx - 70}, {cy + 70}, 140, 44, "continue");\n'
            'noted("Ran the HeyBuddy self-check.");\n'
            "```"
        )

    mock_worker.reply_text_factory = program_reply
    beats: list[Any] = []
    manager.beat_started.connect(lambda beat: beats.append(beat))
    manager.send_text("show me the middle of my screen", include_screenshot=True)
    got_marks = _wait_until(qt_app, lambda: any(m.kind == "target" for b in beats for m in b.marks), 25.0)
    phase_requests = mock_worker.chat_requests[requests_before:]
    tool_round_ok = len(phase_requests) >= 2 and _has_tool_result(phase_requests[1]) and bool(phase_requests[0].get("tools"))
    record("tool_round_trip", tool_round_ok, {
        "requests": len(phase_requests),
        "tools_offered": len(phase_requests[0].get("tools", [])) if phase_requests else 0,
    })
    kinds = [m.kind for b in beats for m in b.marks]
    record("buddy_program_beats", got_marks and "ring" in kinds and "target" in kinds,
           {"beats": [b.text for b in beats], "marks": kinds})
    _pump(qt_app, 1.0)  # strokes finish drawing
    ring = next((live for live in overlay.ink.marks if live.mark.kind == "ring"), None)
    record("ink_marks_live", ring is not None, len(overlay.ink.marks))
    targets = overlay.ink.armed_targets()
    if targets:
        left, top, width, height = targets[0].physical_rect()
        record("target_hit_test", overlay.ink.hit_target(left + width / 2, top + height / 2) is targets[0]
               and overlay.ink.hit_target(left - 200, top - 200) is None, targets[0].mark.label)
    else:
        record("target_hit_test", False, "no armed target")
    if ring is not None:
        left, top, width, height = ring.physical_rect()
        region = {"left": int(left) - 20, "top": int(top) - 20, "width": int(width) + 40, "height": int(height) + 40}
        with mss.mss() as grabber:
            hidden = grabber.grab(region)
            hidden_image = Image.frombytes("RGB", hidden.size, hidden.rgb)
            for window in overlay._windows:  # noqa: SLF001
                win32.set_window_display_affinity(int(window.winId()), win32.WDA_NONE)
            _pump(qt_app, 0.3)
            visible_image = None
            for _attempt in range(4):
                grab = grabber.grab(region)
                candidate = Image.frombytes("RGB", grab.size, grab.rgb)
                if visible_image is None or _count_buddy_blue_pixels(candidate) > _count_buddy_blue_pixels(visible_image):
                    visible_image = candidate
                _pump(qt_app, 0.15)
            for window in overlay._windows:  # noqa: SLF001
                win32.exclude_window_from_capture(int(window.winId()))
        hidden_image.save(OUTPUT_DIR / "ink_region_capture_excluded.png")
        visible_image.save(OUTPUT_DIR / "ink_region_visible.png")
        visible_blue = _count_buddy_blue_pixels(visible_image)
        hidden_blue = _count_buddy_blue_pixels(hidden_image)
        record("ink_rendered_on_screen", visible_blue > 60, {"blue_pixels": visible_blue})
        record("ink_hidden_from_capture", hidden_blue < max(10, visible_blue // 10),
               {"blue_pixels_when_excluded": hidden_blue}, required=False)
    memory = getattr(manager, "memory", None)
    record("noted_fact_saved", memory is not None and any("self-check" in fact for fact in memory.facts()),
           memory.facts()[-1:] if memory else None)
    overlay.clear_ink()


def _check_background_job(qt_app, jobs, mock_worker, record) -> None:
    def job_reply(payload: dict) -> Any:
        system = str(payload.get("system", ""))
        folder = system.split("your working folder: ", 1)[1].strip().splitlines()[0] if "your working folder: " in system else ""
        if not _has_tool_result(payload):
            return {"text": "", "tool_use": {"id": "toolu_write", "name": "write_file", "input": {
                "path": "output/selfcheck-report.txt", "content": "HeyBuddy background job self-check\n"}}}
        return f"done, the report is in output.\nARTIFACTS:\n{Path(folder) / 'output' / 'selfcheck-report.txt'}"

    mock_worker.reply_text_factory = job_reply
    finished: list[Any] = []
    jobs.job_finished.connect(lambda job: finished.append(job))
    job_id = jobs.start("Self check", "Write a one-line report file.")
    _wait_until(qt_app, lambda: bool(finished) or not jobs.get(job_id).is_active, 25.0)
    job = jobs.get(job_id)
    record("background_job_done", job is not None and job.status == "done"
           and bool(job.artifacts) and Path(job.artifacts[0]).is_file(),
           None if job is None else {"status": job.status, "artifacts": job.artifacts, "error": job.error})
