# Telugu mode (HeyBuddy 0.4, Gnani Vachana)

Telugu mode lets you talk to HeyBuddy in Telugu, or in Telugu mixed with
English ("Tenglish"), and hear it answer in Telugu, while it keeps drawing on
your screen and using tools exactly as in English mode. Speech in both
directions uses **Gnani's Vachana models**: Prisma v2.5 for STT and Timbre
v2.5 for TTS. Claude still does the thinking.

Built for Gnani AI's **Great Indian AI Internship Challenge** (deadline
10 Nov 2026, 23:59 IST). See the [challenge checklist](#challenge-checklist).

## What changes when Telugu mode is on

| Stage | English mode (unchanged) | Telugu mode |
| --- | --- | --- |
| Speech to text | AssemblyAI realtime v3 (`/transcribe-token`) | Gnani realtime STT, `lang_code: te-IN`, WebSocket via the Worker (`/gnani/stt-stream`); if that comes back empty although the mic heard you, one retry with Gnani REST STT (`/gnani/stt`) |
| Reply | Claude, buddy program | Claude, same buddy program, plus a system-prompt addendum: spoken text in Telugu script, English technical terms and names kept as written, program syntax / tags left in ASCII |
| Text clean-up | none | `core/telugu_normalizer.py`: digits, times, dates, ₹ amounts and percentages become spoken Telugu; markdown, code, `[POINT]` tags and stray program calls are removed; Latin-script words are kept as written |
| Text to speech | ElevenLabs MP3 per line (`/tts`) | Gnani **SSE streaming** TTS (`/gnani/tts-stream`), raw PCM played from the first chunk; falls back to REST (`/gnani/tts`) if the stream fails before any audio arrives |
| When speech starts | after each Claude round ends | as soon as each `say()` line (with its marks) is complete in Claude's stream (`core/speech_chunker.py`) |

Switch from the tray: **Language → English / Telugu / తెలుగు (Gnani)**. The
choice is saved as `language_mode` in `settings.json` and applies from the
next turn.

## Setup

1. Get a Gnani API key from the [Gnani API platform](https://app.gnani.ai/voice/).
2. Add it to your Worker as a secret (never put it in the app or the repo):

   ```bash
   cd worker
   npx wrangler secret put GNANI_API_KEY
   ```

3. Optional Worker variables (in both `wrangler.toml` files; defaults shown):

   | Variable | Default | Meaning |
   | --- | --- | --- |
   | `GNANI_TTS_VOICE` | `Varuni` | Timbre v2.5 Telugu voices: Suhana, Lehara, Lavanya, Yukti, Varuni. A Timbre v3.0 voice (e.g. Shivani) needs `GNANI_TTS_MODEL = "timbre-v3.0"`, because voices belong to one model. |
   | `GNANI_TTS_MODEL` | `timbre-v2.5` | `timbre-v2.5` or `timbre-v3.0` |

4. `npx wrangler deploy`, then in HeyBuddy pick **Language → Telugu**.
   Without `GNANI_API_KEY` the Gnani routes answer
   `503 Telugu mode is not configured`. The English routes keep working.

Optional settings (`settings.json` → `telugu`):

| Key | Default | Meaning |
| --- | --- | --- |
| `stt_lang_code` | `te-IN` | language of the realtime STT session |
| `tts_language` | `te-IN` | Gnani TTS `language` (`te-IN` or `auto`) |
| `min_silence_ms` | `400` | VAD silence that closes a segment (lower is faster; segments are joined anyway) |
| `stream_speech` | `true` | speak each line as soon as Claude has written it |
| `tts_speed` | `1.0` | 0.85 to 1.15 |
| `tts_sample_rate` | `24000` | PCM rate requested from Gnani |

## Worker routes added

All new routes are under `/gnani/`. `/chat`, `/tts` and `/transcribe-token`
are unchanged. The optional `APP_SHARED_SECRET` check (`X-HeyBuddy-Key`)
applies to every route, including the WebSocket upgrade.

| Route | Client sends | Worker forwards to |
| --- | --- | --- |
| `GET /gnani/stt-stream` (WebSocket) | headers `x-heybuddy-lang` (default `te-IN`), `x-sample-rate`, `x-min-silence-ms`; binary PCM frames | `wss://api.vachana.ai/stt/v3/stream`, opened with `fetch()` + `Upgrade: websocket`; adds `x-api-key-id`, maps the language header to `lang_code`, passes through only the documented `x-sample-rate`, `x-format` and VAD headers, relays frames both ways |
| `POST /gnani/stt` | WAV body (≤ 60 s), header `x-heybuddy-lang` | `POST https://api.vachana.ai/stt/v3` (multipart `audio_file`, `language_code`) |
| `POST /gnani/tts` | `{text, language?, speed?, audio_config?}` | `POST https://api.vachana.ai/api/v1/tts/inference` with `voice`/`model` from Worker vars |
| `POST /gnani/tts-stream` | same | `POST https://api.vachana.ai/api/v1/tts/sse`, SSE passed through |

The Worker validates language codes, clamps `speed` to 0.85–1.15, allows
only containers `raw`/`wav`/`mp3` and the documented sample rates, and caps
text at 5,000 characters.

## Verified endpoints (sources)

Read on **2026-10-27**. Only what these pages document is used.

* STT realtime: [Speech-to-Text (Realtime)](https://docs.gnani.ai/api/STT/stt-websocket.md). `wss://api.vachana.ai/stt/v3/stream`; headers `x-api-key-id`, `lang_code`, `x-sample-rate`, VAD headers; raw PCM s16le mono in frames of exactly 1,024 bytes (512 samples) at real-time cadence; JSON events `connected`, `processing`, `transcript`, `error`.
* STT REST: [Speech-to-Text (REST)](https://docs.gnani.ai/api/STT/speech-to-text.md). `POST https://api.vachana.ai/stt/v3`, multipart, clips ≤ 60 s, `language_code`; `multi_lang_codes` is mentioned for code-mixing.
* TTS REST: [Text-to-Speech (REST)](https://docs.gnani.ai/api/TTS/tts-inference.md). `POST https://api.vachana.ai/api/v1/tts/inference`, header `X-API-Key-ID`.
* TTS SSE: [Text-to-Speech (Streaming)](https://docs.gnani.ai/api/TTS/tts-sse.md). `POST https://api.vachana.ai/api/v1/tts/sse`; events `start`, `chunk` (base64 `audio`), `complete`, `error`.
* Voices: [Voice Catalog](https://docs.gnani.ai/api/TTS/available-voices.md). Timbre v2.5 Telugu voices: Suhana, Lehara, Lavanya, Yukti, Varuni.
* Number formatting: [Text Normalization Guide](https://docs.gnani.ai/api/TTS/tts-input-formating.md). Telugu number words are "under continuous refinement" there, which is why HeyBuddy converts numbers itself.
* Workers outbound WebSocket: [Cloudflare: Using the WebSockets API](https://developers.cloudflare.com/workers/examples/websockets/), "Write a WebSocket client" (fetch with `Upgrade: websocket`, then `accept()`).

## Live measurements (2026-10-27)

Run with `scripts/gnani_smoke.py` from a cloud sandbox in the US (Cloudflare
edge PDX), **straight to `api.vachana.ai`, not through the Worker**. The
sandbox cannot host the Worker, so the Worker path is covered only by mocked
tests and by a local `wrangler dev` check (below). Voice Varuni, model
timbre-v2.5. All phrases are synthetic; STT input is Gnani's own TTS output
(a round trip), so accuracy here is an upper bound, not a measure of real
microphones. Latency from India is likely lower than from the US. About 25
API requests were spent in total, 2 of them rejected unauthenticated.

**TTS**

| Call | Phrase | First byte / first chunk | Total | Audio length |
| --- | --- | --- | --- | --- |
| REST (16 kHz WAV) | plain Telugu | 0.96 s | 1.66 s | 3.5 s |
| REST | numbers as words | 0.63 s | 0.86 s | 2.2 s |
| REST | Tenglish | 0.85 s | 1.08 s | 2.7 s |
| REST | question | 0.63 s | 0.63 s | 1.9 s |
| **SSE** (24 kHz raw PCM) | plain Telugu | **0.41 s** first audio chunk | 0.77 s | 3.5 s |

SSE starts audio about 2x sooner than REST on the same sentence, which is
why Telugu mode streams.

**STT REST (`language_code=te-IN`)**, round trip on the TTS clips

| Phrase | Sent to TTS | Transcript | Time | CER | WER |
| --- | --- | --- | --- | --- | --- |
| plain Telugu | నమస్తే, ఈ రోజు వాతావరణం చాలా బాగుంది. | నమస్తే ఈరోజు వాతావరణం చాలా బాగుంది | 0.90 s | 0.00 | 0.15 (only "ఈ రోజు" vs "ఈరోజు") |
| numbers | మీటింగ్ సాయంత్రం ఐదు గంటలకు ఉంది. | మీటింగ్ సాయంత్రం ఐదు గంటలకు ఉంది | 0.58 s | 0.00 | 0.00 |
| Tenglish | నా laptop లో Chrome browser open చేయండి. | నా లాప్టాప్ లో క్రోమ్ బ్రౌజర్ ఓపెన్ చేయండి | 0.60 s | n/a (script) | n/a |
| question | నా ఫైల్స్ ఎక్కడ సేవ్ అయ్యాయి? | నా ఫైల్స్ ఎక్కడ సేవ్ అయ్యాయి | 0.59 s | 0.00 | 0.00 |

Telugu came back exact, apart from one word-joining difference and the
punctuation, which STT omits. In `te-IN`, **English words come back
transliterated into Telugu script** (laptop → లాప్టాప్). The words are
right, so CER against the Latin reference is meaningless. Claude reads
these fine; the system prompt still asks it to answer with English terms in
Latin script.

**STT REST with `multi_lang_codes=te-IN,en-IN`** (Tenglish clip, 0.84 s):
the response's `transcript` is an **object, not a string**:
`{"en-IN": "naa laptop lo chrome browser open cha", "te-IN": "నా లాప్టాప్ లో క్రోమ్ బ్రౌజర్ ఓపెన్ చేయండి"}`.
The schema of this response is not documented on the saved page, so HeyBuddy
does not send `multi_lang_codes`. Its REST client accepts both shapes and
takes the `te-IN` text.

**STT realtime (WSS): not measured, blocked in the test sandbox.** The
handshake works live: Gnani answered `101` and sent `connected`, echoing
`language_code: te-IN`, `sample_rate: 16000` and our VAD/partials headers.
But no `processing`, `transcript`, partial or error ever came back, even
for a deliberately malformed 1,000-byte frame, after 5 sessions (clip plus
up to 2.5 s of real-time silence, VAD threshold 0.4, `x-max-speech-s: 5`).
One run ended with the client's writes timing out because the far end
stopped reading. The sandbox reaches Gnani through a header-injecting
egress proxy, and this pattern says binary frames are not being relayed
after the upgrade. So the realtime numbers must come from a run on a normal
connection:

```powershell
$env:GNANI_API_KEY = "..."      # your key; never commit it
python scripts/gnani_smoke.py --steps realtime --clips-dir $env:TEMP\gnani-clips
```

The realtime step drives HeyBuddy's own `GnaniStreamingSession` (pointed
straight at Gnani), so it tests the shipped framing, pacing, release padding
and segment joining. Until it has been confirmed live, Telugu voice turns are
protected by the REST fallback: an empty realtime transcript with audible
mic input is re-sent once to `/gnani/stt`, which was verified live above at
about 0.6 s.

## What is mocked vs live

| Part | Status |
| --- | --- |
| Gnani TTS REST + SSE (direct) | **live** (table above) |
| Gnani STT REST, `te-IN` and multi-language (direct) | **live** |
| Gnani STT realtime (direct) | handshake **live**; transcripts not observed (sandbox proxy, see above) |
| Worker routes → Gnani | built with `wrangler deploy --dry-run`; `wrangler dev` locally with a dummy key returned 405/426/400 for bad method, upgrade and language, and Gnani's own `401 INVALID_API_KEY` for both `/gnani/tts-stream` and the `/gnani/stt-stream` upgrade, so URLs and header names reach Gnani. Not run with a real key, because the sandbox cannot host the Worker. |
| App ↔ Worker (WebSocket, SSE, REST, toggle, streaming, fallback) | **mocked**: `tests/mock_gnani.py` is a local RFC 6455 WebSocket + SSE stand-in for the Worker |

## Latency design

* **STT**: mic blocks are re-cut into exact 1,024-byte frames and paced at
  32 ms. Audio captured before the socket opened is sent at 2x real time to
  catch up. On release HeyBuddy sends `min_silence_ms + 200 ms` of silence
  (Gnani has no documented "flush now" frame), waits for every `processing`
  to get its `transcript`, then closes the socket in the background so the
  reply never waits for the close handshake.
* **LLM → TTS**: `StreamingReplyChunker` releases a `say()` line and its
  marks the moment the next `say(` starts streaming (prose: at each
  sentence end), so TTS for line 1 runs while Claude writes line 2. Two
  lines are fetched ahead, as before.
* **TTS**: SSE PCM chunks go straight into a `sounddevice` stream; playback
  starts on the first chunk, with no MP3 decoding.
* **Timings** go to `%APPDATA%\HeyBuddy\logs\latency.log`, one line per
  turn and never any content:
  `turn=7 mode=telugu release->stt_final=…ms stt_final->first_token=…ms first_token->first_chunk=…ms first_token->first_audio=…ms release->first_audio=…ms`.

## Limits

* **Realtime STT is single-language per session** (Gnani). Telugu mode opens
  `te-IN`. Code-mixed English words still come through, transliterated into
  Telugu script. Pure-English questions are better asked in English mode.
* Realtime sessions are capped at 15 minutes by Gnani (push-to-talk turns
  are far shorter). REST STT takes ≤ 60 s; the fallback sends at most the
  newest 55 s.
* Telugu number words are produced on the client, and Gnani's server-side
  normaliser runs again on what remains. The client normaliser handles
  integers up to 10¹², decimals, ₹ (rupees and paise, lakh and crore),
  percentages, `HH:MM` times with or without AM/PM, `DD/MM/YYYY` dates,
  1st–10th, ranges and version numbers. Runs of 6 or more digits (OTPs,
  phone numbers) are read digit by digit.
* The voice and model are Worker settings; changing them means a redeploy,
  like the ElevenLabs voice.
* Settings dialog: the language is switched from the tray. The `telugu`
  block is edited in `settings.json`.

## Challenge checklist

- [x] Uses Gnani models: Prisma v2.5 STT (realtime + REST fallback) and Timbre v2.5 TTS (SSE + REST), through the Worker.
- [x] Clean-room code; no HeyClicky code, prompts or assets.
- [x] Synthetic data only, in tests and in the smoke test; no user recordings stored (`save_recordings` stays off by default).
- [x] No keys in the app or the repo: `GNANI_API_KEY` lives only as a Worker secret, or in your shell for the smoke test.
- [ ] Confirm realtime STT live on a normal connection (`scripts/gnani_smoke.py --steps realtime`) and add the numbers above.
- [ ] Deploy the Worker with `GNANI_API_KEY`, then try a Telugu turn end to end on Windows.
- [ ] Record a demo video: a Tenglish question, the buddy drawing on screen, a Telugu answer, plus the tray toggle.
- [ ] Post on LinkedIn / X with the demo, tagging Gnani, with **#GnaniAI #GreatIndianAIInternshipChallenge**.
- [ ] Submit before **10 Nov 2026, 23:59 IST**.
