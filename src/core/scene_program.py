"""The "buddy program" reply format: narration lines + on-screen marks.

HeyClicky's brain does not answer with bare `[POINT:...]` tags. It streams a
tiny script that interleaves spoken lines with drawing calls (`say(...)`,
`point(...)`, `ring(...)`, `arrow(...)`, ...). Each spoken line "owns" the
marks that follow it, and those marks appear while that line is spoken
(see docs/HEYCLICKY_TEARDOWN.md, "Ink").

HeyBuddy reproduces that *behaviour* with its own, deliberately small and
safe format. Nothing is executed: the fenced block is parsed with Python's
`ast` module and only whitelisted calls with literal arguments are kept.

    sure, the export button is up here.
    ```buddy
    say("click export in the top right")
    point(1180, 42, "export")
    say("then pick pdf from this list")
    frame(900, 120, 300, 220, "formats")
    ```

Rules:
* Prose outside the fence is spoken first, as its own beat.
* Inside the fence every `say()` starts a new beat; marks attach to the
  most recent beat (or to a silent first beat).
* Coordinates are pixels of the screenshot image the model saw, origin at
  its top-left. `screen=N` picks the Nth image (default: cursor screen).
* Legacy Clicky replies (`[POINT:x,y:label:screenN]`) still parse, so an
  older prompt or model keeps working.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from ..models.message import CapturedScreen
from ..utils.constants import POINT_TAG_PATTERN

FENCE_RE = re.compile(r"```(?:buddy|heybuddy|clicky|js|javascript)?[ \t]*\n(.*?)(?:```|\Z)", re.DOTALL)
_POINT_RE = re.compile(POINT_TAG_PATTERN)
_BARE_PROGRAM_RE = re.compile(r"^\s*(?:step|say|task)\s*\(")
_WHITESPACE_RE = re.compile(r"[ \t]{2,}")

# kind -> (positional parameter names, number of leading params that must be numbers)
MARK_SIGNATURES: dict[str, tuple[tuple[str, ...], int]] = {
    "point": (("x", "y", "label"), 2),
    "ring": (("x", "y", "w", "h", "label"), 4),
    "frame": (("x", "y", "w", "h", "label"), 4),
    "highlight": (("x", "y", "w", "h"), 4),
    "underline": (("x", "y", "w"), 3),
    "arrow": (("x1", "y1", "x2", "y2", "label"), 4),
    "num": (("x", "y", "n"), 2),
    "write": (("x", "y", "text"), 2),
    # Outlines a real control and waits for the user to click inside it;
    # the click starts an automatic follow-up turn (guided walkthroughs).
    "target": (("x", "y", "w", "h", "label"), 4),
    # Free polyline through [[x, y], ...]; `straight=True` = no smoothing.
    "trace": (("points",), 1),
}
# Calls that change the beat structure or memory but draw nothing.
#   say(text)        start a new spoken beat
#   clear()/startOver()/erase(id)  fade existing marks before the next beat
#   step(n)          grouping hint, ignored (kept for model compatibility)
#   noted(fact)      durable fact about the user -> local memory file
#   seen(fact)       what the user is doing (context only, logged)
#   task(title)      names the multi-step task in the notch
#   plan(text)       longer plan text, shown in the panel, not spoken
CONTROL_CALLS = {"say", "clear", "startOver", "erase", "step", "noted", "seen", "task", "plan"}
MAX_STATEMENTS = 80
MAX_TEXT_CHARS = 600


@dataclass
class Mark:
    """One drawing instruction in screenshot-image coordinates."""
    kind: str
    params: dict[str, Any]
    screen_index: int | None = None
    # Filled by `resolve_marks`: physical desktop pixels.
    physical: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return str(self.params.get("label") or self.params.get("text") or "")

    @property
    def is_resolved(self) -> bool:
        return bool(self.physical)


@dataclass
class Beat:
    """One spoken line plus the marks shown while it is spoken."""
    text: str
    marks: list[Mark] = field(default_factory=list)
    clear_before: bool = False


@dataclass
class SceneProgram:
    beats: list[Beat]
    explicitly_no_point: bool = False
    parse_errors: list[str] = field(default_factory=list)
    noted_facts: list[str] = field(default_factory=list)
    seen_notes: list[str] = field(default_factory=list)
    task_title: str = ""
    plan_text: str = ""

    @property
    def targets(self) -> list[Mark]:
        return [mark for mark in self.marks if mark.kind == "target"]

    @property
    def spoken_text(self) -> str:
        return " ".join(beat.text for beat in self.beats if beat.text).strip()

    @property
    def marks(self) -> list[Mark]:
        return [mark for beat in self.beats for mark in beat.marks]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _literal(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, (int, float, str, bool))):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(element) for element in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _literal(node.operand)
        if isinstance(inner, (int, float)):
            return -inner if isinstance(node.op, ast.USub) else inner
    raise ValueError("only literal numbers and strings are allowed")


def _split_statements(code: str) -> list[str]:
    """Split on newlines and top-level semicolons (JS habit), ignoring
    semicolons inside string literals."""
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in code:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'`":
            quote = char
            current.append(char)
        elif char in ";\n":
            statements.append("".join(current))
            current = []
        else:
            current.append(char)
    statements.append("".join(current))
    cleaned = []
    for statement in statements:
        statement = statement.strip()
        if not statement or statement.startswith(("//", "#")):
            continue
        cleaned.append(statement.replace("`", '"'))
    return cleaned


_JS_WORDS = {"true": "True", "false": "False", "null": "None", "undefined": "None"}
_OBJECT_TAIL_RE = re.compile(r",\s*\{([^{}]*)\}\s*\)\s*$")


def _js_to_python(statement: str) -> str:
    """Accept the JS habits models bring: true/false, a trailing options
    object `{straight: true}` (-> keyword args) and a final `;`."""
    statement = statement.strip().rstrip(";").strip()
    match = _OBJECT_TAIL_RE.search(statement)
    if match:
        pairs = []
        for part in match.group(1).split(","):
            if ":" in part:
                key, value = part.split(":", 1)
                key = key.strip().strip("\"'")
                if key.isidentifier():
                    pairs.append(f"{key}={value.strip()}")
        statement = statement[: match.start()] + (", " + ", ".join(pairs) if pairs else "") + ")"
    return re.sub(r"\b(true|false|null|undefined)\b", lambda m: _JS_WORDS[m.group(1)], statement)


def parse_statement(statement: str) -> tuple[str, list[Any], dict[str, Any]]:
    """Parse one `name(args)` call with literal arguments. Raises ValueError."""
    statement = _js_to_python(statement)
    try:
        tree = ast.parse(statement, mode="eval")
    except SyntaxError as error:
        raise ValueError(f"not a call: {statement[:60]!r}") from error
    call = tree.body
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        raise ValueError(f"not a call: {statement[:60]!r}")
    name = call.func.id
    if name not in MARK_SIGNATURES and name not in CONTROL_CALLS:
        raise ValueError(f"unknown call {name}()")
    args = [_literal(argument) for argument in call.args]
    kwargs = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ValueError("**kwargs not allowed")
        kwargs[keyword.arg] = _literal(keyword.value)
    return name, args, kwargs


def _build_mark(name: str, args: list[Any], kwargs: dict[str, Any]) -> Mark:
    names, required = MARK_SIGNATURES[name]
    screen_value = kwargs.pop("screen", None)
    params: dict[str, Any] = {}
    for index, value in enumerate(args[: len(names)]):
        params[names[index]] = value
    # A trailing extra positional like point(x, y, "label", 2) = screen.
    if len(args) > len(names) and screen_value is None and isinstance(args[len(names)], (int, float)):
        screen_value = args[len(names)]
    for key, value in kwargs.items():
        if key in names:
            params[key] = value
    if name == "trace":
        points = params.get("points")
        if not isinstance(points, list) or len(points) < 2 or not all(
            isinstance(pt, list) and len(pt) >= 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pt[:2])
            for pt in points
        ):
            raise ValueError("trace() needs [[x, y], ...] with at least two points")
        params["points"] = [[float(pt[0]), float(pt[1])] for pt in points[:64]]
        params["straight"] = bool(kwargs.get("straight", False))
        params["closed"] = bool(kwargs.get("closed", False))
        return Mark(kind=name, params=params, screen_index=int(screen_value) if isinstance(screen_value, (int, float)) else None)
    for key in names[:required]:
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name}() needs numeric {key}")
    for key in ("label", "text"):
        if key in params:
            params[key] = str(params[key])[:120]
    if "n" in params:
        params["n"] = str(params["n"])[:4]
    screen_index = int(screen_value) if isinstance(screen_value, (int, float)) else None
    return Mark(kind=name, params=params, screen_index=screen_index)


def _clean(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def parse_reply_program(text: str) -> SceneProgram:
    """Turn a full model reply into beats. Never raises."""
    errors: list[str] = []
    text = text or ""
    if "```" not in text and _BARE_PROGRAM_RE.match(text):
        # Some models drop the fence and answer with the bare program.
        text = "```buddy\n" + text + "\n```"
    fences = list(FENCE_RE.finditer(text))
    if not fences:
        return _parse_legacy(text or "")

    outside: list[str] = []
    cursor = 0
    for fence in fences:
        outside.append(text[cursor:fence.start()])
        cursor = fence.end()
    outside.append(text[cursor:])
    prose = _clean(_POINT_RE.sub("", " ".join(outside)))
    code = "\n".join(fence.group(1) for fence in fences)
    beats: list[Beat] = []
    if prose:
        beats.append(Beat(text=prose[:MAX_TEXT_CHARS * 2]))
    pending_clear = False
    program = SceneProgram(beats=beats, parse_errors=errors)
    for statement in _split_statements(code)[:MAX_STATEMENTS]:
        try:
            name, args, kwargs = parse_statement(statement)
            first_text = _clean(str(args[0] if args else kwargs.get("text", "")))[:MAX_TEXT_CHARS]
            if name == "say":
                if first_text:
                    beats.append(Beat(text=first_text, clear_before=pending_clear))
                    pending_clear = False
            elif name in ("clear", "startOver", "erase"):
                pending_clear = True
            elif name == "step":
                continue
            elif name == "noted":
                if first_text:
                    program.noted_facts.append(first_text[:300])
            elif name == "seen":
                if first_text:
                    program.seen_notes.append(first_text[:300])
            elif name == "task":
                program.task_title = first_text[:80]
            elif name == "plan":
                program.plan_text = first_text
            else:
                mark = _build_mark(name, args, kwargs)
                if not beats:
                    beats.append(Beat(text="", clear_before=pending_clear))
                    pending_clear = False
                beats[-1].marks.append(mark)
        except (ValueError, IndexError) as error:
            errors.append(str(error))
    if not beats:
        beats.append(Beat(text=""))
    return program


def _parse_legacy(text: str) -> SceneProgram:
    marks: list[Mark] = []
    explicitly_no_point = False
    for match in _POINT_RE.finditer(text):
        x_raw, y_raw, label_raw, screen_raw = match.groups()
        if x_raw is None or y_raw is None:
            explicitly_no_point = True
            continue
        marks.append(Mark(
            kind="point",
            params={"x": int(x_raw), "y": int(y_raw), "label": (label_raw or "").strip()},
            screen_index=int(screen_raw) if screen_raw else None,
        ))
    spoken = _clean(_POINT_RE.sub("", text))
    return SceneProgram(beats=[Beat(text=spoken, marks=marks)], explicitly_no_point=explicitly_no_point)


def display_text_for_partial(partial: str) -> str:
    """What the panel shows while the reply streams: prose plus the say()
    lines seen so far, never raw code or tags."""
    fence_start = (partial or "").find("```")
    if fence_start == -1:
        without_tags = _POINT_RE.sub("", partial or "")
        bracket = without_tags.rfind("[")
        if bracket != -1 and "]" not in without_tags[bracket:] and without_tags[bracket:bracket + 2] in ("[", "[P"):
            without_tags = without_tags[:bracket]
        return _clean(without_tags)
    prose = _clean(_POINT_RE.sub("", partial[:fence_start]))
    lines = re.findall(r"say\(\s*([\"'])(.*?)(?<!\\)\1", partial[fence_start:], re.DOTALL)
    spoken = " ".join(_clean(line) for _, line in lines)
    return _clean(f"{prose} {spoken}")


# ---------------------------------------------------------------------------
# Resolution to physical desktop pixels
# ---------------------------------------------------------------------------


def _pick_screen(mark: Mark, screens: list[CapturedScreen]) -> CapturedScreen:
    cursor_screen = next((screen for screen in screens if screen.is_cursor_screen), screens[0])
    if mark.screen_index is not None and 1 <= mark.screen_index <= len(screens):
        return screens[mark.screen_index - 1]
    return cursor_screen


def resolve_marks(program: SceneProgram, screens: list[CapturedScreen]) -> SceneProgram:
    """Fill `mark.physical` for every mark; drop marks when no screenshot."""
    if not screens:
        for beat in program.beats:
            beat.marks = []
        return program
    for beat in program.beats:
        for mark in beat.marks:
            screen = _pick_screen(mark, screens)
            mark.screen_index = screen.screen_number
            p = mark.params
            physical: dict[str, Any] = {}
            if mark.kind == "trace":
                flat: list[float] = []
                for px, py in p["points"]:
                    fx, fy = screen.image_point_to_physical(px, py)
                    flat.extend((float(fx), float(fy)))
                physical["points"] = flat  # type: ignore[assignment]
                physical["x"], physical["y"] = flat[0], flat[1]
            elif mark.kind == "arrow":
                physical["x1"], physical["y1"] = screen.image_point_to_physical(p["x1"], p["y1"])
                physical["x2"], physical["y2"] = screen.image_point_to_physical(p["x2"], p["y2"])
            else:
                physical["x"], physical["y"] = screen.image_point_to_physical(p["x"], p["y"])
                if "w" in p:
                    right, bottom = screen.image_point_to_physical(p["x"] + p["w"], p["y"] + p.get("h", 0))
                    physical["w"] = max(1.0, right - physical["x"])
                    physical["h"] = max(1.0, bottom - physical["y"]) if "h" in p else 0.0
            physical["monitor_left"] = screen.monitor_left
            physical["monitor_top"] = screen.monitor_top
            physical["dpi_scale"] = screen.dpi_scale
            mark.physical = physical
    return program
