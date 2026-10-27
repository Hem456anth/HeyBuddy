from __future__ import annotations

from src.core.scene_program import parse_reply_program
from src.core.speech_chunker import StreamingReplyChunker


def _feed_in_pieces(text: str, size: int) -> tuple[list[tuple[int, str]], list[str]]:
    chunker = StreamingReplyChunker()
    emitted: list[tuple[int, str]] = []
    for start in range(0, len(text), size):
        for piece in chunker.feed(text[start:start + size]):
            emitted.append((start, piece))
    return emitted, chunker.flush()


PROGRAM = (
    "```buddy\n"
    'say("File menu ఇక్కడ ఉంది")\n'
    'ring(10, 20, 30, 40, "file")\n'
    "clear()\n"
    'say("తర్వాత Save As. నొక్కండి")\n'
    'target(50, 60, 70, 20, "Save As")\n'
    'say("అంతే!")\n'
    "```"
)


def test_program_lines_are_released_with_their_marks_before_the_reply_ends():
    emitted, rest = _feed_in_pieces(PROGRAM, 5)
    pieces = [piece for _start, piece in emitted] + rest
    assert len(pieces) == 3
    # The first line (and its ring) is released as soon as the second say()
    # starts, long before the reply ends.
    assert emitted[0][0] < PROGRAM.index('say("తర్వాత') + 10, "first line should be released while streaming"
    assert emitted[1][0] < PROGRAM.index('say("అంతే') + 10
    beats = [parse_reply_program(piece).beats for piece in pieces]
    assert [b.text for group in beats for b in group] == ["File menu ఇక్కడ ఉంది", "తర్వాత Save As. నొక్కండి", "అంతే!"]
    assert beats[0][0].marks[0].kind == "ring"
    # clear() before a say() travels with that say().
    assert beats[1][0].clear_before is True
    assert beats[1][0].marks[0].kind == "target"
    assert beats[2][0].marks == []


def test_whole_reply_and_chunked_reply_give_the_same_beats():
    whole = [(b.text, [m.kind for m in b.marks]) for b in parse_reply_program(PROGRAM).beats]
    for size in (1, 3, 17, len(PROGRAM)):
        emitted, rest = _feed_in_pieces(PROGRAM, size)
        chunked = [
            (b.text, [m.kind for m in b.marks])
            for piece in [p for _s, p in emitted] + rest
            for b in parse_reply_program(piece).beats
        ]
        assert chunked == whole, size


def test_prose_is_cut_into_sentences():
    emitted, rest = _feed_in_pieces("సరే. ఇది రెండవ వాక్యం! మూడవది", 2)
    assert [piece for _s, piece in emitted] == ["సరే.", "ఇది రెండవ వాక్యం!"]
    assert rest == ["మూడవది"]


def test_prose_then_program_and_bare_program():
    emitted, rest = _feed_in_pieces('ఒక్క నిమిషం.\n```buddy\nsay("చూస్తున్నా")\n```', 4)
    pieces = [p for _s, p in emitted] + rest
    assert pieces[0] == "ఒక్క నిమిషం."
    assert parse_reply_program(pieces[1]).beats[0].text == "చూస్తున్నా"
    emitted, rest = _feed_in_pieces('say("ఒకటి")\npoint(1, 2, "x")\nsay("రెండు")', 3)
    pieces = [p for _s, p in emitted] + rest
    assert [parse_reply_program(p).beats[0].text for p in pieces] == ["ఒకటి", "రెండు"]
    assert parse_reply_program(pieces[0]).beats[0].marks[0].kind == "point"


def test_flush_resets_between_rounds():
    chunker = StreamingReplyChunker()
    chunker.feed('```buddy\nsay("ఒకటి")')
    assert len(chunker.flush()) == 1
    # A terminator at the very end may still be followed by more text.
    assert chunker.feed("హలో.") == []
    assert chunker.feed(" ఇంకా") == ["హలో."]
    assert chunker.flush() == ["ఇంకా"]
