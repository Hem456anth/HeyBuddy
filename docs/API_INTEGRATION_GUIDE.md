# API integration guide (HeyBuddy backend)

The Android app never holds a provider key. It talks only to **your HeyBuddy
backend** (`backend/`, FastAPI), which holds the keys and calls three
providers:

| Concern | Provider | Backend module | Enabled when |
|---|---|---|---|
| Language model (vision + tools, streaming) | Vercel AI Gateway | `heybuddy_backend/providers/llm.py` | `AI_GATEWAY_API_KEY` set |
| Text-to-speech, optional speech-to-text | Gradium | `heybuddy_backend/providers/voice.py` | `GRADIUM_API_KEY` set (STT also needs `GRADIUM_STT_ENABLED=true`) |
| Connected apps (Gmail, Calendar, ...) | Composio REST v3.1 | `heybuddy_backend/providers/composio.py` | `COMPOSIO_API_KEY` + `COMPOSIO_AUTH_CONFIGS` set |

Every provider is optional except the LLM. With no Gradium key the phone
speaks with the Android TTS voice and listens with the Android
`SpeechRecognizer`; with no Composio key the Apps tab says integrations are
off.

> **Status of verification.** No real provider key existed while this was
> built. All provider code is exercised only against mocked HTTP
> (`respx`) in `backend/tests/` and against a mock backend in the Android
> tests. Endpoint paths, headers and payload shapes below were taken from the
> providers' official docs, accessed **2026-10-25**. They have **not** been
> verified against the live services. Do a smoke test with your own keys
> before relying on them (see "First live check" at the end).

---

## 1. Device-facing API (what the phone calls)

All endpoints except `/healthz` and `/v1/pair` need
`Authorization: Bearer <device token>`.

| Method + path | Purpose |
|---|---|
| `GET /healthz` | liveness (`{"ok": true, "version": ...}`) |
| `POST /v1/pair` | `{"pairing_secret", "device_name"}` -> `{"token", "device_id"}`. Rate-limited per IP; constant-time compare. |
| `DELETE /v1/device` | revoke this phone's token |
| `GET /v1/config` | which providers are on, allowed models, default model, toolkits |
| `POST /v1/chat` | OpenAI-style messages + local tool specs -> **SSE** stream of `text`, `tool_call`, `error`, `done` events |
| `POST /v1/tts` | `{"text", "voice"?}` -> audio bytes (`audio/wav`) |
| `POST /v1/stt` | raw `audio/wav|pcm|ogg|opus` body -> `{"text"}` (size-capped) |
| `GET /v1/integrations` | enabled toolkits + this user's connections and their status |
| `POST /v1/integrations/connect` | `{"toolkit"}` -> Composio hosted sign-in `redirect_url` (opened in a Custom Tab) |
| `DELETE /v1/integrations/{id}` | disconnect (also deletes the Composio connected account) |
| `POST /v1/integrations/execute` | `{"proposal_id", "arguments", "confirmed"}` -> result (see approval flow) |
| `GET/DELETE /v1/integrations/logs[/{id}]` | action log, user-deletable |

SSE `tool_call` events carry `kind`:
`local` (the phone runs it through its own approval policy),
`integration` (server-validated Composio proposal: `proposal_id`, `risk`, `preview`),
or `rejected` (unknown tool or arguments that failed the tool's JSON schema).

### Approval flow for integrations (unauthorized-execution defence)

1. The model may only *propose* a Composio call. The backend validates the
   arguments against the tool's JSON schema, classifies risk
   (`read` / `write` / `irreversible`, unknown = `write`), stores a
   **single-use proposal** bound to the user, tool and a hash of the
   arguments, and streams it to the phone.
2. The phone decides with `ApprovalPolicy.decideIntegration`: reads may run
   automatically if the user allowed it; **writes and irreversible actions
   always show a preview dialog** and need an explicit "Allow".
3. `POST /v1/integrations/execute` refuses unless the proposal exists, belongs
   to this user, is unexpired and unused, the arguments hash matches exactly,
   the toolkit is still enabled, and (for non-read) `confirmed=true`.
4. Results are redacted, truncated and returned to the model wrapped as
   untrusted data (`frame_untrusted`), never as instructions.

---

## 2. Vercel AI Gateway (LLM)

Docs: <https://vercel.com/docs/ai-gateway>,
OpenAI compatibility <https://vercel.com/docs/ai-gateway/openai-compat>,
streaming <https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions/streaming>,
tool calling <https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions/tool-calling>,
images <https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions/images>,
authentication <https://vercel.com/docs/ai-gateway/authentication>,
model allowlist <https://vercel.com/docs/ai-gateway/security-and-compliance/model-allowlist>,
pricing <https://vercel.com/docs/ai-gateway/pricing> (all accessed 2026-10-25).

| Item | Value used by the backend |
|---|---|
| Base URL | `https://ai-gateway.vercel.sh/v1` (`AI_GATEWAY_BASE_URL`) |
| Auth | `Authorization: Bearer $AI_GATEWAY_API_KEY` |
| Endpoint | `POST /chat/completions`, `stream: true` |
| Request | OpenAI Chat Completions: `model`, `messages` (user content may include `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}`), `tools`, `max_tokens` |
| Stream | `data: {chat.completion.chunk}` lines, ending `data: [DONE]`; tool-call deltas are reassembled server-side |
| Model ids | `creator/model`, e.g. `anthropic/claude-sonnet-5.5` (default, `HEYBUDDY_DEFAULT_MODEL`) |
| Allowlist | `HEYBUDDY_ALLOWED_MODELS` (the phone can only pick from it); you can also set an allowlist in the Vercel dashboard |
| Token cap | `HEYBUDDY_MAX_TOKENS_CAP` (default 4096) clamps whatever the phone asks for |

**Pricing notes (accessed 2026-10-25, check before relying on them):** the
pricing page states the Gateway adds *no markup and no platform fee* on tokens
(you pay the provider's list price, pay-as-you-go from AI Gateway Credits).
Each Vercel team gets a free tier with **$5/month** of credits limited to
"Free Tier eligible models" and lower per-model rate limits; buying credits
moves the team to the paid tier (all models, higher limits, BYOK). A HeyBuddy
turn sends one screenshot (longest edge <= 1280 px) plus text, so image input
tokens dominate cost; we did not measure real token counts.

---

## 3. Gradium (voice)

Docs: <https://docs.gradium.ai> (TTS POST, STT POST, voices), pricing
<https://gradium.ai/pricing> (accessed 2026-10-25).

| Item | Value used by the backend |
|---|---|
| Base URL | `https://api.gradium.ai/api` (`GRADIUM_BASE_URL`) |
| Auth | `x-api-key: $GRADIUM_API_KEY` |
| TTS | `POST /post/speech/tts` with JSON (text, voice id, output format) -> audio; the backend returns `audio/wav` to the phone |
| Voice | `GRADIUM_VOICE_ID` (default is one of the flagship voices listed in Gradium's docs); the phone may pass another id |
| STT | `POST /post/speech/asr` with the raw audio body and a content type; off unless `GRADIUM_STT_ENABLED=true` |

**Pricing notes (accessed 2026-10-25):** credit-based plans. Free $0/month
with 45k credits; XS $12/month 225k; S $39/month 900k; M $312/month 9M;
L $1,480/month 45M. TTS costs **1 credit per character** (~45k characters
~= 1 hour of audio); STT costs **3 credits per second**. The Free plan lists
2 concurrent TTS streams and no commercial use (commercial use starts at XS).
HeyBuddy speaks short beats, so a typical reply is a few hundred characters;
not measured on real traffic.

Latency and voice quality were **not** evaluated (no key). The app falls back
to the on-device Android voice whenever `/v1/tts` fails or is disabled.

---

## 4. Composio (connected apps)

Docs: API reference v3.1 <https://docs.composio.dev/reference>,
authentication <https://docs.composio.dev/docs/authentication>,
rate limits <https://docs.composio.dev/reference/rate-limits>,
pricing <https://composio.dev/pricing> (accessed 2026-10-25).

| Item | Value used by the backend |
|---|---|
| Base URL | `https://backend.composio.dev/api/v3.1` (`COMPOSIO_BASE_URL`) |
| Auth | `x-api-key: $COMPOSIO_API_KEY` |
| Connect | `POST /connected_accounts/link` with an auth config id + `user_id` (+ optional callback) -> hosted `redirect_url` |
| List connections | `GET /connected_accounts?user_ids=<user>` |
| Disconnect | `DELETE /connected_accounts/{id}` |
| Tool catalogue | `GET /tools?toolkit_slug=<toolkit>` -> slug, description, `input_parameters` JSON schema, tags |
| Execute | `POST /tools/execute/{tool_slug}` with `user_id`, `connected_account_id`, `arguments` |
| Which toolkits | `COMPOSIO_AUTH_CONFIGS=gmail:ac_...,googlecalendar:ac_...` (auth configs you create in the Composio dashboard) |

`user_id` is HeyBuddy's internal per-user id (random, created at first pairing),
never an email address.

**Pricing notes (accessed 2026-10-25; the page says new pricing applies to
sign-ups from 2026-08-15):** Hobby $0 with 100,000 tool calls/month (hard
cap), Pro $29/month with a usage credit, then $0.0003 per tool call. HeyBuddy
calls `/tools/execute` directly (not inside a Composio "Session"); the
pricing page lists "direct execution outside a Session" with 10K/month free
on Hobby, then +$0.0001 per call on Pro. Composio-managed OAuth apps have
lower allowances than your own OAuth app. Rate limits are per organisation
per minute (2,000/min Hobby, 10,000/min Pro per the pricing page).

---

## 5. Swapping a provider

Each provider sits behind a small `Protocol` in `providers/`:

```python
class LLMProvider(Protocol):          # providers/llm.py
    async def stream(self, model, messages, tools, max_tokens) -> AsyncIterator[LLMEvent]: ...

class TTSProvider(Protocol):          # providers/voice.py
    enabled: bool; media_type: str
    async def synthesize(self, text: str, voice: str | None = None) -> bytes: ...

class STTProvider(Protocol):
    enabled: bool
    async def transcribe(self, audio: bytes, content_type: str) -> str: ...
```

The integrations client exposes `create_link`, `list_accounts`,
`delete_account`, `tools`, `execute`.

To swap:

1. Write a class with the same methods (e.g. any OpenAI-compatible endpoint
   works for `LLMProvider` by pointing `AI_GATEWAY_BASE_URL` at it; a
   different TTS vendor needs a new `TTSProvider`).
2. Build it in `get_providers()` (`heybuddy_backend/app.py`) when its env vars
   are present.
3. Keep the device API unchanged: the phone only knows `/v1/*`, so no app
   update is needed.
4. Add `respx` tests in `backend/tests/` mirroring `test_chat.py` /
   `test_voice.py`.

Never move a provider call into the app: that would put a key on the phone.

## 6. First live check (do this once you have keys)

```bash
cd backend && cp .env.example .env   # fill keys locally, never commit .env
uvicorn heybuddy_backend.main:app --port 8080
curl -s localhost:8080/healthz
TOKEN=$(curl -s -XPOST localhost:8080/v1/pair -H 'content-type: application/json' \
  -d '{"pairing_secret":"<yours>","device_name":"curl"}' | jq -r .token)
curl -s localhost:8080/v1/config -H "authorization: Bearer $TOKEN"
curl -N localhost:8080/v1/chat -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"say hi"}],"system":"","tools":[]}'
curl -s -XPOST localhost:8080/v1/tts -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"text":"hello"}' -o hello.wav
```

If any of these fail with a 4xx from the provider, compare the request with
the provider's current docs; payload details are the most likely thing to
have drifted.
