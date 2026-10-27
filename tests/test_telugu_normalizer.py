"""Telugu TTS normaliser. Synthetic sentences only (no user data)."""
from __future__ import annotations

import re

import pytest

from src.core.telugu_normalizer import (
    digits_to_telugu,
    normalize_for_tts,
    number_to_telugu,
    strip_markup,
)


@pytest.mark.parametrize("number, words", [
    (0, "సున్నా"),
    (7, "ఏడు"),
    (19, "పందొమ్మిది"),
    (21, "ఇరవై ఒకటి"),
    (100, "వంద"),
    (101, "నూట ఒకటి"),
    (250, "రెండు వందల యాభై"),
    (500, "ఐదు వందలు"),
    (1000, "వెయ్యి"),
    (2500, "రెండు వేల ఐదు వందలు"),
    (10000, "పది వేలు"),
    (100000, "లక్ష"),
    (150000, "లక్ష యాభై వేలు"),
    (2500000, "ఇరవై ఐదు లక్షలు"),
    (10000000, "కోటి"),
    (234567890, "ఇరవై మూడు కోట్ల నలభై ఐదు లక్షల అరవై ఏడు వేల ఎనిమిది వందల తొంభై"),
    (-5, "మైనస్ ఐదు"),
])
def test_number_words(number, words):
    assert number_to_telugu(number) == words


def test_adjectival_forms_before_a_noun():
    assert number_to_telugu(500, adjectival=True) == "ఐదు వందల"
    assert number_to_telugu(1, adjectival=True) == "ఒక"
    assert number_to_telugu(3000, adjectival=True) == "మూడు వేల"


def test_digit_by_digit():
    assert digits_to_telugu("907") == "తొమ్మిది సున్నా ఏడు"


@pytest.mark.parametrize("text, spoken", [
    ("ధర ₹2,500 మాత్రమే", "ధర రెండు వేల ఐదు వందల రూపాయలు మాత్రమే"),
    ("₹1", "ఒక రూపాయి"),
    ("₹4.50", "నాలుగు రూపాయల యాభై పైసలు"),
    ("₹0.75", "డెబ్బై ఐదు పైసలు"),
    ("Rs. 599/- కి", "ఐదు వందల తొంభై తొమ్మిది రూపాయలు కి"),
    ("INR 1,00,000", "లక్ష రూపాయలు"),
    ("₹12 lakh", "పన్నెండు లక్షల రూపాయలు"),
    ("₹3.5 కోట్లు", "మూడు పాయింట్ ఐదు కోట్ల రూపాయలు"),
])
def test_currency(text, spoken):
    assert normalize_for_tts(text) == spoken


@pytest.mark.parametrize("text, spoken", [
    ("battery 45% ఉంది", "battery నలభై ఐదు శాతం ఉంది"),
    ("12.5% పెరిగింది", "పన్నెండు పాయింట్ ఐదు శాతం పెరిగింది"),
    ("100 %", "వంద శాతం"),
])
def test_percentages(text, spoken):
    assert normalize_for_tts(text) == spoken


@pytest.mark.parametrize("text, spoken", [
    ("మీటింగ్ 10:30 కి", "మీటింగ్ పది గంటల ముప్పై నిమిషాలు కి"),
    ("5:30 PM కి రండి", "సాయంత్రం ఐదు గంటల ముప్పై నిమిషాలు కి రండి"),
    ("9:00 am", "ఉదయం తొమ్మిది గంటలు"),
    ("20:00", "రాత్రి ఎనిమిది గంటలు"),
    ("1:15", "ఒంటి గంట పదిహేను నిమిషాలు"),
    ("7 PM", "సాయంత్రం ఏడు గంటలు"),
    ("12:00 PM", "మధ్యాహ్నం పన్నెండు గంటలు"),
    ("00:05", "అర్ధరాత్రి పన్నెండు గంటల ఐదు నిమిషాలు"),
])
def test_times(text, spoken):
    assert normalize_for_tts(text) == spoken


def test_dates_ordinals_ranges_versions():
    assert normalize_for_tts("15/11/2026 న") == "పదిహేను నవంబర్ రెండు వేల ఇరవై ఆరు న"
    assert normalize_for_tts("2nd tab") == "రెండవ tab"
    assert normalize_for_tts("3–5 నిమిషాలు") == "మూడు నుండి ఐదు నిమిషాలు"
    assert normalize_for_tts("version 1.2.3") == "version ఒకటి పాయింట్ రెండు పాయింట్ మూడు"
    assert normalize_for_tts("Python 3.12") == "Python మూడు పాయింట్ ఒకటి రెండు"


def test_long_digit_runs_and_leading_zeros_are_read_digit_by_digit():
    assert normalize_for_tts("OTP 482913") == "OTP నాలుగు ఎనిమిది రెండు తొమ్మిది ఒకటి మూడు"
    assert normalize_for_tts("code 007") == "code సున్నా సున్నా ఏడు"


def test_english_terms_and_names_are_preserved():
    text = "Ravi, Chrome లో Ctrl+Shift+T నొక్కండి. mp3 file H2O.txt Windows 11 లో ఉంది"
    spoken = normalize_for_tts(text)
    for kept in ("Ravi", "Chrome", "Ctrl+Shift+T", "mp3", "H2O.txt", "Windows"):
        assert kept in spoken
    assert "పదకొండు" in spoken
    assert not re.search(r"\b11\b", spoken)


def test_markup_and_program_tags_are_stripped():
    text = (
        "## Steps\n- **File** menu లో `Save As` click చేయండి [POINT:120,40:file:screen2]\n"
        "* [docs](https://example.com) చూడండి <laugh>\n"
        "```buddy\nsay(\"ignored\")\npoint(1, 2, \"x\")\n```"
    )
    spoken = normalize_for_tts(text)
    assert spoken == "Steps File menu లో Save As click చేయండి docs చూడండి"


def test_leaked_program_lines_keep_only_the_spoken_text():
    assert strip_markup('say("సరే, చూద్దాం")\nring(10, 20, 30, 40, "menu")').strip() == "సరే, చూద్దాం"


def test_plain_telugu_is_unchanged():
    text = "నమస్తే, మీకు ఎలా సహాయం చేయగలను?"
    assert normalize_for_tts(text) == text
