"""Buddy program parsing: our format, HeyClicky-style bare programs, legacy tags."""
from src.core.scene_program import display_text_for_partial, parse_reply_program, resolve_marks
from src.models.message import CapturedScreen


def _screens():
    return [
        CapturedScreen(b"", "image/jpeg", "s1", 1, True, 1280, 800, 0, 0, 2560, 1600, 2.0),
        CapturedScreen(b"", "image/jpeg", "s2", 2, False, 1280, 720, 2560, 0, 1920, 1080, 1.0),
    ]


def test_fenced_program_beats_and_marks():
    reply = '''sure.
```buddy
say("click export up here")
ring(1150, 30, 70, 28, "export")
say("then choose pdf")
frame(900, 120, 300, 220)
arrow(100, 100, 300, 200, "drag", screen=2)
```'''
    program = parse_reply_program(reply)
    assert [b.text for b in program.beats] == ["sure.", "click export up here", "then choose pdf"]
    assert [m.kind for m in program.beats[1].marks] == ["ring"]
    assert [m.kind for m in program.beats[2].marks] == ["frame", "arrow"]
    assert program.beats[2].marks[1].screen_index == 2
    assert program.spoken_text == "sure. click export up here then choose pdf"


def test_bare_heyclicky_style_program_with_js_options():
    reply = (
        'step(1);\nsay("I can trace the space.");\n'
        'trace([[164, 165], [1155, 165], [1155, 587]], {straight: true});\n'
        'startOver();\nsay("click it");\ntarget(771, 359, 432, 27, "Open the introduction");\n'
        'noted("Low-intermediate Python.");\ntask("Learn LangChain");\nseen("Watching a course.");'
    )
    program = parse_reply_program(reply)
    assert program.beats[0].marks[0].kind == "trace"
    assert program.beats[0].marks[0].params["straight"] is True
    assert program.beats[1].clear_before is True
    assert program.targets[0].label == "Open the introduction"
    assert program.noted_facts == ["Low-intermediate Python."]
    assert program.task_title == "Learn LangChain"
    assert program.seen_notes == ["Watching a course."]
    assert not program.parse_errors


def test_nothing_is_executed_and_bad_calls_are_dropped():
    reply = '```buddy\n__import__("os").system("calc")\nsay(open("x").read())\nring(1, 2)\nsay("ok")\n```'
    program = parse_reply_program(reply)
    assert [b.text for b in program.beats] == ["ok"]
    assert len(program.parse_errors) == 3


def test_semicolons_inside_strings_survive():
    program = parse_reply_program('```buddy\nsay("first; second"); point(5, 6, "x")\n```')
    assert program.beats[0].text == "first; second"
    assert program.beats[0].marks[0].params == {"x": 5, "y": 6, "label": "x"}


def test_legacy_point_tags_still_work():
    program = parse_reply_program("over there [POINT:400,300:terminal:screen2]")
    assert program.beats[0].text == "over there"
    mark = program.beats[0].marks[0]
    assert (mark.kind, mark.params["x"], mark.screen_index) == ("point", 400, 2)
    assert parse_reply_program("html is markup [POINT:none]").explicitly_no_point


def test_resolution_maps_boxes_to_physical_pixels():
    program = parse_reply_program('```buddy\nsay("a")\nframe(640, 400, 64, 40)\npoint(640, 360, "t", screen=2)\n```')
    resolve_marks(program, _screens())
    frame, point = program.beats[0].marks
    assert (frame.physical["x"], frame.physical["y"], frame.physical["w"], frame.physical["h"]) == (1280, 800, 128, 80)
    assert (point.physical["x"], point.physical["y"]) == (2560 + 960, 540)
    assert frame.physical["monitor_left"] == 0 and point.physical["monitor_left"] == 2560


def test_marks_dropped_without_screenshots():
    program = resolve_marks(parse_reply_program('```buddy\nsay("a")\nring(1,2,3,4)\n```'), [])
    assert program.beats[0].marks == []


def test_partial_display_hides_code():
    partial = 'ok ```buddy\nsay("click the gear")\nring(1, 2, 3, 4)\nsay("then sa'
    assert display_text_for_partial(partial) == "ok click the gear"
    assert display_text_for_partial("hello [POINT:12") == "hello"
