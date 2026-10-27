"""Cut Claude's streamed reply into speakable pieces as it arrives.

Without this, a voice round is only spoken once Claude has finished the
whole round. In Telugu mode (HeyBuddy 0.4) `CompanionManager` feeds every
streamed text delta into `StreamingReplyChunker` and speaks each piece as
soon as it is complete, so the first line starts playing while Claude is
still writing the rest.

Where a piece ends:

* inside a buddy program (```buddy fence, or a bare program that starts
  with `say(`/`step(`/`task(`), a piece is one `say()` line plus the marks
  after it. It is released when the *next* `say()` starts, because only
  then are its marks complete (marks appear while their line is spoken).
  `clear()`/`startOver()`/`erase()`/`step()` lines right before a `say()`
  belong to the next line and move with it;
* in plain prose, a piece is one sentence (`.`, `?`, `!`, `।`, or a line
  break).

Each program piece is returned re-wrapped in its own ```buddy fence so
`scene_program.parse_reply_program` parses it exactly like a full reply.
Pure Python; see tests/test_speech_chunker.py.
"""
from __future__ import annotations

import re

_SAY_LINE_RE = re.compile(r"^\s*say\s*\(")
_PROGRAM_START_RE = re.compile(r"^\s*(?:say|step|task)\s*\(")
_CARRY_TO_NEXT_RE = re.compile(r"^\s*(?:clear|startOver|erase|step)\s*\(")
_SENTENCE_END_RE = re.compile(r"[.!?।](?=\s)")
_FENCE_RE = re.compile(r"^\s*```")


def wrap_program(lines: list[str]) -> str:
    return "```buddy\n" + "\n".join(lines) + "\n```"


class StreamingReplyChunker:
    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._partial_line = ""
        self._in_program = False
        self._program_lines: list[str] = []
        self._program_has_say = False
        self._prose = ""

    # ----- public -----
    def feed(self, piece: str) -> list[str]:
        """Add streamed text; return the pieces that just became complete."""
        ready: list[str] = []
        text = self._partial_line + (piece or "")
        lines = text.split("\n")
        self._partial_line = lines.pop()
        for line in lines:
            ready.extend(self._consume_line(line))
        ready.extend(self._early_program_release())
        ready.extend(self._early_prose_sentences())
        return ready

    def flush(self) -> list[str]:
        """End of a round: return everything still buffered and reset."""
        ready: list[str] = []
        if self._partial_line:
            line, self._partial_line = self._partial_line, ""
            ready.extend(self._consume_line(line))
        ready.extend(self._emit_prose(everything=True))
        ready.extend(self._emit_program())
        self._reset()
        return [chunk for chunk in ready if chunk.strip()]

    # ----- internals -----
    def _consume_line(self, line: str) -> list[str]:
        ready: list[str] = []
        if _FENCE_RE.match(line):
            if self._in_program:
                ready.extend(self._emit_program())
                self._in_program = False
            else:
                ready.extend(self._emit_prose(everything=True))
                self._in_program = True
            return ready
        if not self._in_program and not self._prose.strip() and _PROGRAM_START_RE.match(line):
            # Bare program without a fence (some models drop it).
            self._in_program = True
        if self._in_program:
            if _SAY_LINE_RE.match(line) and self._program_has_say:
                ready.extend(self._release_before_next_say())
            if line.strip():
                self._program_lines.append(line)
            if _SAY_LINE_RE.match(line):
                self._program_has_say = True
            return ready
        self._prose += line + "\n"
        ready.extend(self._emit_prose(everything=False))
        return ready

    def _release_before_next_say(self) -> list[str]:
        carried: list[str] = []
        while self._program_lines and _CARRY_TO_NEXT_RE.match(self._program_lines[-1]):
            carried.insert(0, self._program_lines.pop())
        ready = self._emit_program()
        self._program_lines = carried
        return ready

    def _early_program_release(self) -> list[str]:
        """The next `say(` has started streaming: the previous line and its
        marks are complete, so speak them now instead of at the newline."""
        if self._in_program and self._program_has_say and _SAY_LINE_RE.match(self._partial_line):
            return self._release_before_next_say()
        return []

    def _emit_program(self) -> list[str]:
        lines = self._program_lines
        self._program_lines = []
        self._program_has_say = False
        if not any(line.strip() for line in lines):
            return []
        return [wrap_program(lines)]

    def _emit_prose(self, everything: bool) -> list[str]:
        ready: list[str] = []
        if everything:
            if self._prose.strip():
                ready.append(self._prose.strip())
            self._prose = ""
            return ready
        # Release complete sentences and complete lines; keep the tail.
        while True:
            newline = self._prose.find("\n")
            match = _SENTENCE_END_RE.search(self._prose)
            cut = -1
            if match is not None:
                cut = match.end()
            if newline != -1 and (cut == -1 or newline < cut):
                cut = newline + 1
            if cut == -1:
                break
            sentence, self._prose = self._prose[:cut], self._prose[cut:]
            if sentence.strip():
                ready.append(sentence.strip())
        return ready

    def _early_prose_sentences(self) -> list[str]:
        """Sentences that finished mid-line, before the newline arrived."""
        if self._in_program:
            return []
        candidate = self._partial_line
        if not candidate or _FENCE_RE.match(candidate) or "`" in candidate:
            return []
        if not self._prose.strip() and _PROGRAM_START_RE.match(candidate):
            return []
        last_end = None
        for match in _SENTENCE_END_RE.finditer(candidate):
            last_end = match.end()
        if last_end is None:
            return []
        self._prose += candidate[:last_end]
        self._partial_line = candidate[last_end:]
        return self._emit_prose(everything=False) + self._take_unterminated_sentence()

    def _take_unterminated_sentence(self) -> list[str]:
        # _emit_prose only cuts at a terminator followed by whitespace; the
        # sentence we just moved ends the buffer, so release it too.
        if self._prose.strip() and _SENTENCE_END_RE.search(self._prose + " "):
            sentence, self._prose = self._prose.strip(), ""
            return [sentence]
        return []
