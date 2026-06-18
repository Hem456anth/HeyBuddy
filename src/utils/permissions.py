"""Runtime diagnostics for the Settings panel.

These functions are intentionally synchronous — they're called from button
clicks that already happen on the Qt thread and complete in well under a
second. If we ever add a checker that talks to the network for longer, move
it onto a `QThread` so the UI doesn't freeze.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import requests
import sounddevice as sd

from .constants import WORKER_TRANSCRIBE_TOKEN_PATH
from .logger import get_logger

log = get_logger(__name__)


@dataclass
class DiagnosticResult:
    """Outcome of one self-check, suitable for direct rendering in the panel."""
    ok: bool
    title: str
    detail: str


# ---------------------------------------------------------------------------
# Microphone
# ---------------------------------------------------------------------------


def check_microphone(sample_rate: int = 16_000, duration_seconds: float = 0.4) -> DiagnosticResult:
    """Open the default input device for a short window and measure RMS.

    A near-zero RMS means we got data but the mic is muted; an exception
    means the device is missing or another app holds it exclusively.
    """
    try:
        recorded = sd.rec(
            int(sample_rate * duration_seconds),
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            blocking=True,
        )
    except Exception as device_error:
        return DiagnosticResult(
            ok=False,
            title="Microphone unavailable",
            detail=str(device_error),
        )

    samples = np.asarray(recorded, dtype=np.int32).reshape(-1)
    if samples.size == 0:
        return DiagnosticResult(
            ok=False,
            title="Microphone returned no samples",
            detail="Check that a recording device is selected as the default in Windows.",
        )
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    # ~50 is room-quiet for a typical 16-bit mic; below ~5 likely means the
    # input is muted in the Windows sound mixer.
    if rms < 5.0:
        return DiagnosticResult(
            ok=False,
            title="Microphone is silent",
            detail=(
                "Captured audio at near-zero level. The device may be muted "
                "or the input volume set to 0 in Windows Sound settings."
            ),
        )
    return DiagnosticResult(
        ok=True,
        title="Microphone OK",
        detail=f"Captured {duration_seconds:.1f}s at RMS={rms:.0f}.",
    )


# ---------------------------------------------------------------------------
# Cloudflare Worker reachability
# ---------------------------------------------------------------------------


def ping_worker(worker_url: str, timeout_seconds: float = 5.0) -> DiagnosticResult:
    """POST `/transcribe-token` and check we get a usable JSON token back.

    We pick `/transcribe-token` because it's the cheapest round-trip that
    proves end-to-end the Worker is reachable AND the upstream
    `streaming.assemblyai.com` token endpoint accepts the Worker's secret.
    `/chat` would require us to build a full Anthropic request, and `/tts`
    would synthesize a real audio file we'd discard.
    """
    if not worker_url.strip():
        return DiagnosticResult(
            ok=False,
            title="Worker URL is empty",
            detail="Set `worker_url` in config/settings.json or via the Settings dialog.",
        )
    normalized = worker_url.strip()
    if "://" not in normalized:
        normalized = f"https://{normalized}"
    url = f"{normalized.rstrip('/')}{WORKER_TRANSCRIBE_TOKEN_PATH}"
    start = time.time()
    try:
        response = requests.post(url, json={}, timeout=timeout_seconds)
    except requests.RequestException as net_error:
        return DiagnosticResult(
            ok=False,
            title="Worker unreachable",
            detail=f"{type(net_error).__name__}: {net_error}",
        )
    elapsed_ms = (time.time() - start) * 1000.0
    if response.status_code != 200:
        return DiagnosticResult(
            ok=False,
            title=f"Worker responded {response.status_code}",
            detail=(response.text or "").strip()[:200] or "(empty body)",
        )
    try:
        data = response.json()
    except ValueError:
        return DiagnosticResult(
            ok=False,
            title="Worker returned non-JSON",
            detail=(response.text or "").strip()[:200],
        )
    if not (data.get("token") or data.get("temp_token")):
        return DiagnosticResult(
            ok=False,
            title="Worker returned no token",
            detail=(
                "The Worker responded but the body had no `token` field. "
                "Check that `ASSEMBLYAI_API_KEY` is set as a Worker secret."
            ),
        )
    return DiagnosticResult(
        ok=True,
        title="Worker OK",
        detail=f"Token endpoint responded in {elapsed_ms:.0f} ms.",
    )
