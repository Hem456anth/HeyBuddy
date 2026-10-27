"""Text clean-up before Telugu speech synthesis (Telugu mode, HeyBuddy 0.4).

Gnani's Timbre TTS documents that numbers, dates and currency should reach
it as spoken words (its server-side normaliser has its deepest coverage for
English, Hindi, Marathi and Tamil; Telugu is "under continuous refinement").
So in Telugu mode every line is normalised on the client first, which keeps
the read-out predictable:

* markup the ear can't use is removed: fenced code, inline backticks,
  markdown emphasis / headings / bullets / links, legacy `[POINT:...]`
  tags, buddy-program calls that leaked into prose, `<audio-tag>` markers;
* digits, times, dates, ₹ amounts, percentages and simple ordinals become
  Telugu words (`₹2,500` -> `రెండు వేల ఐదు వందల రూపాయలు`);
* everything written in Latin script (English technical terms, app and
  product names, proper names) is left exactly as written, so Tenglish
  replies keep their English words.

Pure Python, no Qt/Win32: unit-tested on any OS (tests/test_telugu_normalizer.py).
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Number words
# ---------------------------------------------------------------------------

_UNITS = [
    "సున్నా", "ఒకటి", "రెండు", "మూడు", "నాలుగు", "ఐదు", "ఆరు", "ఏడు", "ఎనిమిది", "తొమ్మిది",
    "పది", "పదకొండు", "పన్నెండు", "పదమూడు", "పద్నాలుగు", "పదిహేను", "పదహారు", "పదిహేడు",
    "పద్దెనిమిది", "పందొమ్మిది",
]
_TENS = {
    2: "ఇరవై", 3: "ముప్పై", 4: "నలభై", 5: "యాభై",
    6: "అరవై", 7: "డెబ్బై", 8: "ఎనభై", 9: "తొంభై",
}
# Final word -> its form before a noun ("ఐదు వందలు" but "ఐదు వందల రూపాయలు").
_OBLIQUE_ENDINGS = {
    "ఒకటి": "ఒక",
    "వందలు": "వందల",
    "వేలు": "వేల",
    "లక్షలు": "లక్షల",
    "కోట్లు": "కోట్ల",
}
_ORDINALS = {
    1: "మొదటి", 2: "రెండవ", 3: "మూడవ", 4: "నాలుగవ", 5: "ఐదవ",
    6: "ఆరవ", 7: "ఏడవ", 8: "ఎనిమిదవ", 9: "తొమ్మిదవ", 10: "పదవ",
}
_MONTHS = [
    "జనవరి", "ఫిబ్రవరి", "మార్చి", "ఏప్రిల్", "మే", "జూన్",
    "జూలై", "ఆగస్టు", "సెప్టెంబర్", "అక్టోబర్", "నవంబర్", "డిసెంబర్",
]
_POINT_WORD = "పాయింట్"   # colloquial loanword, as Gnani's own TN uses for Indic languages
_PERCENT_WORD = "శాతం"
_MINUS_WORD = "మైనస్"
_RUPEE_SINGULAR = "రూపాయి"
_RUPEES = "రూపాయలు"
_RUPEES_OBLIQUE = "రూపాయల"
_PAISE = "పైసలు"
_TO_WORD = "నుండి"
_MAX_SPOKEN_NUMBER = 10 ** 12 - 1


def _oblique(words: str) -> str:
    """Turn the last word into its pre-noun form."""
    head, _, last = words.rpartition(" ")
    replacement = _OBLIQUE_ENDINGS.get(last)
    if replacement is None:
        return words
    return f"{head} {replacement}".strip()


def _below_hundred(number: int) -> str:
    if number < 20:
        return _UNITS[number]
    tens, units = divmod(number, 10)
    return _TENS[tens] + (f" {_UNITS[units]}" if units else "")


def _below_thousand(number: int) -> str:
    hundreds, rest = divmod(number, 100)
    if hundreds == 0:
        return _below_hundred(rest)
    if hundreds == 1:
        return "వంద" if rest == 0 else f"నూట {_below_hundred(rest)}"
    if rest == 0:
        return f"{_UNITS[hundreds]} వందలు"
    return f"{_UNITS[hundreds]} వందల {_below_hundred(rest)}"


def number_to_telugu(number: int, adjectival: bool = False) -> str:
    """Spoken Telugu for an integer, Indian grouping (వేలు, లక్షలు, కోట్లు).

    `adjectival=True` gives the form used before a noun
    ("ఐదు వందల రూపాయలు", "ఒక రూపాయి").
    """
    if number < 0:
        return f"{_MINUS_WORD} {number_to_telugu(-number, adjectival)}"
    if number > _MAX_SPOKEN_NUMBER:
        return digits_to_telugu(str(number))
    if number == 0:
        return _UNITS[0]

    crores, remainder = divmod(number, 10 ** 7)
    lakhs, remainder = divmod(remainder, 10 ** 5)
    thousands, rest = divmod(remainder, 1000)

    parts: list[str] = []

    def group(count: int, single: str, plural: str, oblique: str, is_last: bool) -> str:
        if count == 1:
            return single
        count_words = _oblique(number_to_telugu(count) if count >= 100 else _below_hundred(count))
        return f"{count_words} {plural if is_last else oblique}"

    if crores:
        parts.append(group(crores, "కోటి", "కోట్లు", "కోట్ల", not (lakhs or thousands or rest)))
    if lakhs:
        parts.append(group(lakhs, "లక్ష", "లక్షలు", "లక్షల", not (thousands or rest)))
    if thousands:
        parts.append(group(thousands, "వెయ్యి", "వేలు", "వేల", not rest))
    if rest:
        parts.append(_below_thousand(rest))
    words = " ".join(parts)
    return _oblique(words) if adjectival else words


def digits_to_telugu(digits: str) -> str:
    """Read a digit string one digit at a time (phone numbers, OTPs)."""
    return " ".join(_UNITS[int(char)] for char in digits if char.isdigit())


def _decimal_to_telugu(raw: str, adjectival: bool = False) -> str:
    """`12.5` -> `పన్నెండు పాయింట్ ఐదు`; Indian/Western commas are ignored."""
    cleaned = raw.replace(",", "")
    if "." in cleaned:
        whole, fraction = cleaned.split(".", 1)
        whole_words = number_to_telugu(int(whole or "0"))
        return f"{whole_words} {_POINT_WORD} {digits_to_telugu(fraction)}"
    return number_to_telugu(int(cleaned), adjectival=adjectival)


# ---------------------------------------------------------------------------
# Markup stripping
# ---------------------------------------------------------------------------

_FENCED_CODE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_POINT_TAG_RE = re.compile(r"\[POINT:[^\]]*\]")
_SAY_CALL_RE = re.compile(r"""\bsay\(\s*(["'])(.*?)(?<!\\)\1\s*\)[ \t]*;?""", re.DOTALL)
_PROGRAM_CALL_LINE_RE = re.compile(
    r"^\s*(?:point|ring|frame|highlight|underline|arrow|num|write|trace|target|clear|startOver|erase|"
    r"step|noted|seen|task|plan)\s*\(.*\)\s*;?\s*$",
    re.MULTILINE,
)
_ANGLE_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z _-]{0,30}>")
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|>)\s+", re.MULTILINE)
_LEFTOVER_SYMBOLS_RE = re.compile(r"[*#|~^]+")
_SPACES_RE = re.compile(r"[ \t\u00a0]+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r" +([,.!?।])")


def strip_markup(text: str) -> str:
    """Remove everything that should not be read aloud."""
    text = _FENCED_CODE_RE.sub(" ", text or "")
    text = _POINT_TAG_RE.sub(" ", text)
    text = _PROGRAM_CALL_LINE_RE.sub(" ", text)
    text = _SAY_CALL_RE.sub(lambda match: match.group(2), text)
    text = _INLINE_CODE_RE.sub(lambda match: match.group(1), text)
    text = _LINK_RE.sub(lambda match: match.group(1), text)
    text = _ANGLE_TAG_RE.sub(" ", text)
    text = _BOLD_RE.sub(lambda match: match.group(2), text)
    text = _ITALIC_RE.sub(lambda match: match.group(1), text)
    text = _HEADING_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _LEFTOVER_SYMBOLS_RE.sub(" ", text)
    return text


# ---------------------------------------------------------------------------
# Numbers in context
# ---------------------------------------------------------------------------

# A number not glued to Latin letters ("mp3", "H2O", "v2" stay as written).
_NUMBER_CORE = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_STANDALONE = r"(?<![A-Za-z0-9_.,])(?:{core})(?![A-Za-z0-9_]|[.,]\d)"

_DATE_RE = re.compile(r"(?<![\d/.-])(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?![\d/.-])")
_TIME_RE = re.compile(
    r"(?<![\d:.])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])"
    r"(?:\s*([AaPp])\.?\s?[Mm]\.?(?![A-Za-z]))?"
)
_BARE_HOUR_RE = re.compile(r"(?<![\d:.])(\d{1,2})\s*([AaPp])\.?\s?[Mm]\.?(?![A-Za-z])")
_CURRENCY_RE = re.compile(
    r"(?:₹|\bRs\.?|\bINR)\s?(" + _NUMBER_CORE + r")(?:\s?/-)?"
    r"(?:\s?(lakhs?|crores?|లక్షలు|లక్షల|లక్ష|కోట్లు|కోట్ల|కోటి)(?![A-Za-z]))?"
)
_PERCENT_RE = re.compile(r"(?<![A-Za-z0-9_.])(" + _NUMBER_CORE + r")\s?%")
_ORDINAL_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})(?:st|nd|rd|th)\b")
_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?)\s?[–—]\s?(\d+(?:\.\d+)?)(?![A-Za-z0-9_])"
)
_VERSION_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:\.\d+){2,})(?![A-Za-z0-9_]|\.\d)")
_NUMBER_RE = re.compile(_STANDALONE.format(core=_NUMBER_CORE))
_LONG_DIGIT_RUN = 6


def _hour_words(hour: int) -> str:
    # One o'clock is "ఒంటి గంట" in Telugu, not "ఒకటి గంట".
    return "ఒంటి గంట" if hour == 1 else f"{number_to_telugu(hour)} గంటలు"


def _period_word(hour24: int) -> str:
    if hour24 == 0:
        return "అర్ధరాత్రి"
    if hour24 < 5:
        return "తెల్లవారుజామున"
    if hour24 < 12:
        return "ఉదయం"
    if hour24 < 16:
        return "మధ్యాహ్నం"
    if hour24 < 20:
        return "సాయంత్రం"
    return "రాత్రి"


def _spoken_time(hour: int, minute: int, second: int | None, meridiem: str | None) -> str | None:
    if minute > 59 or (second is not None and second > 59):
        return None
    period = ""
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        is_pm = meridiem.lower() == "p"
        hour24 = (hour % 12) + (12 if is_pm else 0)
        period = _period_word(hour24)
        hour12 = hour
    else:
        if hour > 23:
            return None
        if hour == 0 or hour >= 13:
            period = _period_word(hour)
            hour12 = 12 if hour == 0 else hour - 12
        else:
            hour12 = hour
    hour_text = _hour_words(hour12)
    pieces = [period] if period else []
    if minute == 0 and not second:
        pieces.append(hour_text)
    else:
        # "పది గంటల ముప్పై నిమిషాలు": the hour takes its pre-noun form.
        hour_prefix = "ఒంటి గంట" if hour12 == 1 else f"{number_to_telugu(hour12)} గంటల"
        pieces.append(hour_prefix)
        if minute:
            pieces.append("ఒక నిమిషం" if minute == 1 else f"{number_to_telugu(minute)} నిమిషాలు")
        if second:
            pieces.append("ఒక సెకను" if second == 1 else f"{number_to_telugu(second)} సెకన్లు")
    return " ".join(pieces)


def _replace_time(match: re.Match[str]) -> str:
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3)) if match.group(3) else None
    spoken = _spoken_time(hour, minute, second, match.group(4))
    return spoken if spoken is not None else match.group(0)


def _replace_bare_hour(match: re.Match[str]) -> str:
    spoken = _spoken_time(int(match.group(1)), 0, None, match.group(2))
    return spoken if spoken is not None else match.group(0)


def _replace_date(match: re.Match[str]) -> str:
    day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return match.group(0)
    return f"{number_to_telugu(day)} {_MONTHS[month - 1]} {number_to_telugu(year)}"


def _replace_currency(match: re.Match[str]) -> str:
    raw_amount = match.group(1).replace(",", "")
    magnitude = (match.group(2) or "").lower()
    if magnitude:
        if magnitude.startswith("lakh") or magnitude.startswith("లక్ష"):
            scale = "లక్షల"
        else:
            scale = "కోట్ల"
        amount_words = _decimal_to_telugu(raw_amount, adjectival=True)
        return f"{amount_words} {scale} {_RUPEES}"
    if "." in raw_amount:
        rupees_text, paise_text = raw_amount.split(".", 1)
        rupees = int(rupees_text or "0")
        paise = int((paise_text + "0")[:2])
        if paise:
            rupee_part = f"{number_to_telugu(rupees, adjectival=True)} {_RUPEES_OBLIQUE} " if rupees else ""
            return f"{rupee_part}{number_to_telugu(paise, adjectival=True)} {_PAISE}".strip()
        raw_amount = rupees_text
    rupees = int(raw_amount or "0")
    if rupees == 1:
        return f"ఒక {_RUPEE_SINGULAR}"
    return f"{number_to_telugu(rupees, adjectival=True)} {_RUPEES}"


def _replace_percent(match: re.Match[str]) -> str:
    return f"{_decimal_to_telugu(match.group(1))} {_PERCENT_WORD}"


def _replace_ordinal(match: re.Match[str]) -> str:
    number = int(match.group(1))
    return _ORDINALS.get(number, match.group(0))


def _replace_range(match: re.Match[str]) -> str:
    return f"{_decimal_to_telugu(match.group(1))} {_TO_WORD} {_decimal_to_telugu(match.group(2))}"


def _replace_version(match: re.Match[str]) -> str:
    return f" {_POINT_WORD} ".join(number_to_telugu(int(part)) for part in match.group(1).split("."))


def _replace_number(match: re.Match[str]) -> str:
    raw = match.group(0)
    if "," not in raw and "." not in raw and len(raw) >= _LONG_DIGIT_RUN:
        return digits_to_telugu(raw)  # phone numbers, OTPs, IDs
    if "," not in raw and len(raw) > 1 and raw.startswith("0") and "." not in raw:
        return digits_to_telugu(raw)  # "007", PIN-like codes
    return _decimal_to_telugu(raw)


def normalize_numbers(text: str) -> str:
    text = _DATE_RE.sub(_replace_date, text)
    text = _TIME_RE.sub(_replace_time, text)
    text = _BARE_HOUR_RE.sub(_replace_bare_hour, text)
    text = _CURRENCY_RE.sub(_replace_currency, text)
    text = _PERCENT_RE.sub(_replace_percent, text)
    text = _ORDINAL_RE.sub(_replace_ordinal, text)
    text = _RANGE_RE.sub(_replace_range, text)
    text = _VERSION_RE.sub(_replace_version, text)
    text = _NUMBER_RE.sub(_replace_number, text)
    return text


def normalize_for_tts(text: str) -> str:
    """Everything a Telugu say() line needs before it goes to Gnani TTS."""
    text = strip_markup(text)
    text = normalize_numbers(text)
    text = _SPACES_RE.sub(" ", text.replace("\n", " "))
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    return text.strip()
