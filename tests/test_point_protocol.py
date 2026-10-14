from src.api.claude_client import ClaudeClient, parse_reply, strip_point_tags_for_display
from src.models.message import CapturedScreen, Message, Role


def test_parse_point_with_label_and_screen():
    reply = parse_reply("that's on your other monitor. [POINT:400,300:terminal:screen2]")
    assert reply.content == "that's on your other monitor."
    assert len(reply.points) == 1
    point = reply.points[0]
    assert (point.x, point.y, point.label, point.screen_index) == (400, 300, "terminal", 2)


def test_parse_point_without_screen_means_cursor_screen():
    reply = parse_reply("click the color inspector. [POINT:1100,42:color inspector]")
    assert reply.points[0].screen_index is None
    assert reply.points[0].label == "color inspector"


def test_parse_point_none_is_not_spoken():
    reply = parse_reply("html is the skeleton of a web page. [POINT:none]")
    assert reply.content == "html is the skeleton of a web page."
    assert reply.points == []
    assert reply.explicitly_no_point is True


def test_parse_point_without_label_and_spaces():
    reply = parse_reply("here. [POINT: 12 , 34]".replace("POINT: ", "POINT:"))
    assert reply.points and (reply.points[0].x, reply.points[0].y) == (12, 34)
    assert reply.points[0].label == ""


def test_parse_label_only_screen_suffix():
    reply = parse_reply("over there [POINT:5,6:screen2]")
    assert reply.points[0].screen_index == 2
    assert reply.points[0].label == ""


def test_multiple_points_in_order():
    reply = parse_reply("first this [POINT:1,2:file menu] then that [POINT:3,4:save]")
    assert [p.label for p in reply.points] == ["file menu", "save"]
    assert reply.content == "first this then that"


def test_partial_tag_hidden_while_streaming():
    assert strip_point_tags_for_display("click save. [POI") == "click save."
    assert strip_point_tags_for_display("click save. [POINT:12,3") == "click save."
    assert strip_point_tags_for_display("array [0] is first") == "array [0] is first"


def _screen(number: int, cursor: bool, left: int, width: int, height: int, image_w: int, image_h: int) -> CapturedScreen:
    return CapturedScreen(b"\xff\xd8jpeg", "image/jpeg", f"screen {number}", number, cursor,
                          image_w, image_h, left, 0, width, height, 1.0)


def test_image_point_maps_to_physical_monitor_pixels():
    # 2880x1800 laptop panel shown to Claude as 1280x800.
    screen = _screen(1, True, 0, 2880, 1800, 1280, 800)
    assert screen.image_point_to_physical(640, 400) == (1440, 900)
    assert screen.image_point_to_physical(0, 0) == (0, 0)
    # Out-of-range coordinates are clamped inside the monitor.
    assert screen.image_point_to_physical(5000, -20) == (2879, 0)


def test_payload_sends_all_screens_labeled_and_caps_history():
    client = ClaudeClient(proxy=None, model="claude-sonnet-5-5", history_max_exchanges=2)  # type: ignore[arg-type]
    history = []
    for index in range(5):
        history.append(Message(Role.USER, f"q{index}"))
        history.append(Message(Role.ASSISTANT, f"a{index}"))
    history.append(Message(Role.USER, "where is the save button?"))
    screens = [
        _screen(1, True, 0, 2880, 1800, 1280, 800),
        _screen(2, False, 2880, 1920, 1080, 1280, 720),
    ]
    payload = client.build_payload(history, screens)
    messages = payload["messages"]
    assert payload["model"] == "claude-sonnet-5-5"
    assert len(messages) == 5  # 2 exchanges + new question
    assert messages[0]["role"] == "user" and messages[0]["content"] == "q3"
    final_blocks = messages[-1]["content"]
    assert [block["type"] for block in final_blocks] == ["image", "text", "image", "text", "text"]
    assert final_blocks[0]["source"]["media_type"] == "image/jpeg"
    assert final_blocks[-1]["text"] == "where is the save button?"


def test_sse_error_event_raises():
    import json
    import pytest
    with pytest.raises(RuntimeError, match="Overloaded"):
        ClaudeClient.extract_text_delta("error", json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))


def test_sse_ignores_thinking_deltas():
    import json
    thinking = {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}}
    text = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}
    assert ClaudeClient.extract_text_delta("content_block_delta", json.dumps(thinking)) == ""
    assert ClaudeClient.extract_text_delta("content_block_delta", json.dumps(text)) == "hi"
