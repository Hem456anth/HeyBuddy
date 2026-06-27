"""Short auditory cues for state transitions.

Two tones:

* `play_start_chime` — rising A4 → E5 over ~150ms. Plays on the
  IDLE → LISTENING transition (the user just started push-to-talk).
* `play_stop_chime`  — falling E5 → A4 over ~150ms. Plays on the
  LISTENING → anything else transition (the user just released).

Generated in-process via numpy + sounddevice — no asset files. Played
non-blocking via `sd.play`, which queues onto sounddevice's output
device thread, so the chime never delays the state transition or the
network requests that follow it.

Why these notes: A4 and E5 are a perfect fifth apart — a clean
interval that reads as a deliberate signal rather than a random
synth blip. Rising = "starting to listen", falling = "stopping to
listen". Mirrors the conventions used by macOS / iOS Siri activation
sounds.

The chimes never overlap TTS playback because they fire only on
listening transitions; TTS playback happens during RESPONDING, which
is a separate transition.
"""
from __future__ import annotations

import numpy as np
import sounddevice as sd

from ..utils.logger import get_logger

log = get_logger(__name__)

# Audio parameters tuned to be unobtrusive — quiet, short, soft edges.
_SAMPLE_RATE = 44_100
_DURATION_SEC = 0.15
# Amplitude is intentionally low (0.18 of full-scale). Chimes are a
# background cue, not a foreground sound — they shouldn't compete with
# whatever the user is listening to.
_AMPLITUDE = 0.18
_FREQ_LOW = 440.0     # A4
_FREQ_HIGH = 659.25   # E5  (perfect fifth above A4)
# Edge fades prevent the clicks you'd hear from a sine wave that starts
# or ends abruptly at non-zero amplitude.
_FADE_SEC = 0.015


def _build_chime(rising: bool) -> np.ndarray:
    """Generate a swept sine-wave chime with edge fades.

    Frequency is linearly interpolated low→high (rising) or high→low
    (falling) across the duration. Phase is computed by integrating
    instantaneous frequency rather than using `sin(2*pi*f*t)` — the
    latter would phase-discontinuity at sample boundaries when `f` is
    changing, which sounds rougher than the smooth glissando we want.
    """
    n_samples = int(_SAMPLE_RATE * _DURATION_SEC)
    if rising:
        freq = np.linspace(_FREQ_LOW, _FREQ_HIGH, n_samples)
    else:
        freq = np.linspace(_FREQ_HIGH, _FREQ_LOW, n_samples)
    # Cumulative phase from instantaneous frequency — sample i's phase
    # = sum of (2*pi*f_k / sample_rate) for k in [0, i]. Smooth.
    phase = np.cumsum(2.0 * np.pi * freq / _SAMPLE_RATE)

    # Linear in/out fade so we don't click at the boundaries.
    fade_samples = max(1, int(_SAMPLE_RATE * _FADE_SEC))
    envelope = np.ones(n_samples, dtype=np.float32)
    ramp = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
    envelope[:fade_samples] = ramp
    envelope[-fade_samples:] = ramp[::-1]

    return (_AMPLITUDE * np.sin(phase) * envelope).astype(np.float32)


def play_start_chime() -> None:
    """Rising tone — fires on hotkey press (IDLE → LISTENING)."""
    try:
        sd.play(_build_chime(rising=True), samplerate=_SAMPLE_RATE)
    except Exception:
        log.exception("Failed to play start chime")


def play_stop_chime() -> None:
    """Falling tone — fires on hotkey release (LISTENING → processing)."""
    try:
        sd.play(_build_chime(rising=False), samplerate=_SAMPLE_RATE)
    except Exception:
        log.exception("Failed to play stop chime")
