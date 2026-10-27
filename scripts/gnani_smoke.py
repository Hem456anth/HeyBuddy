"""Live smoke test of the Gnani Vachana APIs Telugu mode uses. NOT run in CI.

    set GNANI_API_KEY=...          (PowerShell: $env:GNANI_API_KEY = "...")
    python scripts/gnani_smoke.py [--json results.json] [--skip-realtime]

It talks to api.vachana.ai directly (not through the Worker) and spends a
handful of credits (about 13 short calls with the defaults):

1. TTS REST  (POST /api/v1/tts/inference) — synthesizes the synthetic
   phrases below as 16 kHz WAV; time to first byte and total time.
2. TTS SSE   (POST /api/v1/tts/sse)       — one phrase; time to the first
   audio chunk.
3. STT REST  (POST /stt/v3, language_code=te-IN) on each synthetic clip —
   round-trip accuracy (character error rate vs. the input text).
4. STT REST  with multi_lang_codes=te-IN,en-IN on the code-mixed clip.
5. STT realtime (WSS /stt/v3/stream, lang_code=te-IN): every clip in one
   session, 1,024-byte frames at real-time cadence with silence between
   clips; time from end of speech to each `transcript`.

The key is read from the environment and only ever sent as the
`X-API-Key-ID` header. It is never printed or written anywhere. All test
phrases are synthetic; no user audio is involved.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import unicodedata
import wave

import requests

API = "https://api.vachana.ai"
TTS_REST = f"{API}/api/v1/tts/inference"
TTS_SSE = f"{API}/api/v1/tts/sse"
STT_REST = f"{API}/stt/v3"
STT_WSS = "wss://api.vachana.ai/stt/v3/stream"
VOICE = "Varuni"
MODEL = "timbre-v2.5"
SAMPLE_RATE = 16000
FRAME_BYTES = 1024

# Synthetic phrases: plain Telugu, numbers already as words, and Tenglish.
PHRASES = [
    ("telugu", "నమస్తే, ఈ రోజు వాతావరణం చాలా బాగుంది."),
    ("numbers", "మీటింగ్ సాయంత్రం ఐదు గంటలకు ఉంది."),
    ("tenglish", "నా laptop లో Chrome browser open చేయండి."),
    ("question", "నా ఫైల్స్ ఎక్కడ సేవ్ అయ్యాయి?"),
]


def _key() -> str:
    key = os.environ.get("GNANI_API_KEY", "").strip()
    if not key:
        sys.exit("GNANI_API_KEY is not set")
    return key


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r"[\u200c\u200d]", "", text)          # ZWNJ/ZWJ
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _edit_distance(a: list | str, b: list | str) -> int:
    previous = list(range(len(b) + 1))
    for i, item_a in enumerate(a, 1):
        current = [i]
        for j, item_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (item_a != item_b)))
        previous = current
    return previous[-1]


def accuracy(reference: str, hypothesis: str) -> dict:
    ref, hyp = _normalize(reference), _normalize(hypothesis)
    ref_chars, hyp_chars = ref.replace(" ", ""), hyp.replace(" ", "")
    cer = _edit_distance(ref_chars, hyp_chars) / max(1, len(ref_chars))
    wer = _edit_distance(ref.split(), hyp.split()) / max(1, len(ref.split()))
    return {"cer": round(cer, 3), "wer": round(wer, 3), "exact": ref == hyp}


def tts_rest(session: requests.Session, text: str, language: str = "te-IN") -> tuple[bytes, float, float]:
    body = {"text": text, "voice": VOICE, "model": MODEL, "language": language, "speed": 1.0,
            "audio_config": {"sample_rate": SAMPLE_RATE, "num_channels": 1, "sample_width": 2,
                             "encoding": "linear_pcm", "container": "wav"}}
    started = time.perf_counter()
    response = session.post(TTS_REST, json=body, stream=True, timeout=60)
    first_byte = None
    audio = b""
    for piece in response.iter_content(4096):
        if first_byte is None:
            first_byte = time.perf_counter() - started
        audio += piece
    total = time.perf_counter() - started
    if response.status_code != 200:
        raise RuntimeError(f"TTS REST {response.status_code}: {audio[:300]!r}")
    return audio, first_byte or total, total


def tts_sse(session: requests.Session, text: str) -> dict:
    body = {"text": text, "voice": VOICE, "model": MODEL, "language": "te-IN", "speed": 1.0,
            "audio_config": {"sample_rate": 24000, "num_channels": 1, "sample_width": 2,
                             "encoding": "linear_pcm", "container": "raw"}}
    started = time.perf_counter()
    response = session.post(TTS_SSE, json=body, stream=True, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"TTS SSE {response.status_code}: {response.text[:300]}")
    response.encoding = "utf-8"
    first_chunk = None
    chunks = 0
    audio_bytes = 0
    event = "message"
    for line in response.iter_lines(decode_unicode=True):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            payload = json.loads(line[5:].strip() or "{}")
            if event == "error":
                raise RuntimeError(f"TTS SSE error event: {payload}")
            audio = payload.get("audio") or ""
            if audio:
                if first_chunk is None:
                    first_chunk = time.perf_counter() - started
                chunks += 1
                audio_bytes += len(base64.b64decode(audio))
            if event == "complete":
                break
    total = time.perf_counter() - started
    return {"first_chunk_s": round(first_chunk or total, 3), "total_s": round(total, 3),
            "chunks": chunks, "audio_seconds": round(audio_bytes / 2 / 24000, 2)}


def stt_rest(session: requests.Session, wav_bytes: bytes, multi: str | None = None) -> tuple[str, float, dict]:
    data = {"language_code": "te-IN"}
    if multi:
        data["multi_lang_codes"] = multi
    started = time.perf_counter()
    response = session.post(STT_REST, data=data, files={"audio_file": ("clip.wav", wav_bytes, "audio/wav")},
                            timeout=60)
    elapsed = time.perf_counter() - started
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:300]}
    if response.status_code != 200:
        return "", elapsed, {"status": response.status_code, "body": payload}
    transcript = payload.get("transcript")
    if isinstance(transcript, dict):
        # Multi-language requests return an object rather than a string;
        # keep the raw shape and use its text field if there is one.
        meta = {"status": 200, "raw_transcript": transcript}
        text = transcript.get("text") or transcript.get("transcript") or " ".join(
            str(value) for value in transcript.values() if isinstance(value, str))
        return text.strip(), elapsed, meta
    return (transcript or "").strip(), elapsed, {"status": 200}


def pcm_of(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes)) as wav_file:
        assert wav_file.getframerate() == SAMPLE_RATE and wav_file.getnchannels() == 1
        return wav_file.readframes(wav_file.getnframes())


def stt_realtime(key: str, clips: list[bytes], pause_ms: int = 900) -> dict:
    """One push-to-talk session through HeyBuddy's own GnaniStreamingSession.

    The session normally talks to the Worker; here it is pointed straight at
    Gnani (URL + `x-api-key-id`/`lang_code` headers), so this exercises the
    shipped code: 1,024-byte framing, real-time pacing, trailing silence on
    release, and joining segments. Audio is fed in 100 ms blocks at real
    time, like the microphone, with a pause between clips.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.api.gnani_streaming import GnaniStreamingSession

    class DirectSession(GnaniStreamingSession):
        def build_url(self) -> str:
            return STT_WSS

        def build_headers(self) -> list[str]:
            return [f"x-api-key-id: {key}", f"lang_code: {self.lang_code}",
                    f"x-sample-rate: {self.sample_rate}", f"x-min-silence-ms: {self.min_silence_ms}"]

    finals: list[tuple[float, str]] = []
    errors: list[str] = []
    session = DirectSession(proxy=None, lang_code="te-IN", sample_rate=SAMPLE_RATE, min_silence_ms=400,  # type: ignore[arg-type]
                            on_final=lambda text: finals.append((time.perf_counter(), text)),
                            on_error=errors.append)
    connect_started = time.perf_counter()
    session.start()
    connect_s = time.perf_counter() - connect_started
    block = SAMPLE_RATE // 10 * 2
    speech_end_times: list[float] = []
    next_block = time.perf_counter()
    for index, pcm in enumerate(clips):
        audio = pcm
        if index < len(clips) - 1:
            audio += b"\x00" * (SAMPLE_RATE * 2 * pause_ms // 1000)
        speech_end_at = None
        for start in range(0, len(audio), block):
            delay = next_block - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            session.feed_pcm(audio[start:start + block])
            next_block += 0.1
            if speech_end_at is None and start + block >= len(pcm):
                speech_end_at = time.perf_counter()
        speech_end_times.append(speech_end_at or time.perf_counter())
    release = time.perf_counter()
    transcript = session.stop(final_wait_seconds=5.0)
    stop_s = time.perf_counter() - release
    segments = []
    previous_text = ""
    for index, (arrived, text) in enumerate(finals):
        new_text = text[len(previous_text):].strip() if text.startswith(previous_text) else text
        previous_text = text
        speech_end = speech_end_times[index] if index < len(speech_end_times) else None
        segments.append({"text": new_text,
                         "end_of_speech_to_transcript_s": round(arrived - speech_end, 3) if speech_end else None})
    return {"connect_s": round(connect_s, 3), "release_to_final_transcript_s": round(stop_s, 3),
            "transcript": transcript, "segments": segments, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", help="write results to this file")
    parser.add_argument("--skip-realtime", action="store_true")
    parser.add_argument("--steps", default="tts_rest,tts_sse,stt_rest,stt_multi,realtime",
                        help="comma-separated subset to run (saves credits)")
    parser.add_argument("--clips-dir", help="cache synthetic WAVs here and reuse them on the next run")
    args = parser.parse_args()
    steps = set(args.steps.split(","))
    if args.skip_realtime:
        steps.discard("realtime")
    key = _key()
    session = requests.Session()
    session.headers["X-API-Key-ID"] = key
    results: dict = {"voice": VOICE, "model": MODEL, "date": time.strftime("%Y-%m-%d"), "tts_rest": [],
                     "stt_rest": [], "calls": 0}

    clips: dict[str, bytes] = {}
    if args.clips_dir:
        os.makedirs(args.clips_dir, exist_ok=True)
        for name, _text in PHRASES:
            path = os.path.join(args.clips_dir, f"{name}.wav")
            if os.path.exists(path):
                with open(path, "rb") as handle:
                    clips[name] = handle.read()
    for name, text in PHRASES:
        if name in clips and "tts_rest" not in steps:
            continue
        audio, first_byte, total = tts_rest(session, text)
        if args.clips_dir:
            with open(os.path.join(args.clips_dir, f"{name}.wav"), "wb") as handle:
                handle.write(audio)
        results["calls"] += 1
        clips[name] = audio
        seconds = len(pcm_of(audio)) / 2 / SAMPLE_RATE
        results["tts_rest"].append({"phrase": name, "first_byte_s": round(first_byte, 3),
                                    "total_s": round(total, 3), "audio_seconds": round(seconds, 2)})
        print(f"TTS REST  {name:9s} first byte {first_byte:.2f}s total {total:.2f}s ({seconds:.1f}s audio)")

    if "tts_sse" in steps:
        results["tts_sse"] = tts_sse(session, PHRASES[0][1])
        results["calls"] += 1
        print(f"TTS SSE   first chunk {results['tts_sse']['first_chunk_s']:.2f}s "
              f"total {results['tts_sse']['total_s']:.2f}s")

    for name, text in PHRASES if "stt_rest" in steps else []:
        transcript, elapsed, meta = stt_rest(session, clips[name])
        results["calls"] += 1
        row = {"phrase": name, "reference": text, "transcript": transcript, "elapsed_s": round(elapsed, 3),
               **meta, **accuracy(text, transcript)}
        results["stt_rest"].append(row)
        print(f"STT REST  {name:9s} {elapsed:.2f}s cer={row['cer']} wer={row['wer']} -> {transcript}")

    if "stt_multi" in steps:
        run_multi(session, clips, results)
    if "realtime" in steps:
        run_realtime(key, clips, results)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(results, handle, ensure_ascii=False, indent=2)
    print(f"done: {results['calls']} API calls")
    return 0


def run_multi(session: requests.Session, clips: dict[str, bytes], results: dict) -> None:
    transcript, elapsed, meta = stt_rest(session, clips["tenglish"], multi="te-IN,en-IN")
    results["calls"] += 1
    results["stt_rest_multi"] = {"multi_lang_codes": "te-IN,en-IN", "reference": PHRASES[2][1],
                                 "transcript": transcript, "elapsed_s": round(elapsed, 3), **meta,
                                 **accuracy(PHRASES[2][1], transcript)}
    print(f"STT multi tenglish {elapsed:.2f}s status={meta.get('status')} -> {transcript!r} "
          f"raw={json.dumps(meta.get('raw_transcript') or meta.get('body'), ensure_ascii=False)}")


def run_realtime(key: str, clips: dict[str, bytes], results: dict) -> None:
    realtime = stt_realtime(key, [pcm_of(clips[name]) for name, _ in PHRASES])
    results["calls"] += 1
    for segment, (name, text) in zip(realtime["segments"], PHRASES):
        segment.update({"phrase": name, "reference": text, **accuracy(text, segment["text"])})
    results["stt_realtime"] = realtime
    print(f"STT WSS   connect {realtime['connect_s']}s, release->final {realtime['release_to_final_transcript_s']}s")
    for segment in realtime["segments"]:
        print(f"STT WSS   {segment.get('phrase', '?'):9s} end-of-speech->transcript "
              f"{segment['end_of_speech_to_transcript_s']}s cer={segment.get('cer')} -> {segment['text']}")
    realtime["accuracy_joined"] = accuracy(" ".join(text for _n, text in PHRASES), realtime["transcript"])
    if realtime["errors"]:
        print("STT WSS errors:", realtime["errors"])


if __name__ == "__main__":
    raise SystemExit(main())
