# Security & privacy

## What leaves the machine

| Data | When | Goes to |
| --- | --- | --- |
| Microphone audio (PCM16) | only while the push-to-talk chord is held | your Worker → AssemblyAI (token), then streamed directly to `streaming.assemblyai.com` |
| Screenshots of every monitor (JPEG, ≤1280 px) | once per question, if "Send screenshot" is on | your Worker → Anthropic |
| Transcript + last 10 exchanges (text) | once per question | your Worker → Anthropic |
| Reply text | once per answer, if voice is on | your Worker → ElevenLabs |

Nothing is uploaded while idle. HeyBuddy's own windows (the buddy overlay and
the panel) are excluded from capture (`WDA_EXCLUDEFROMCAPTURE`).

## What is stored locally (`%APPDATA%\HeyBuddy`)

* `config\settings.json` — Worker URL, preferences, optional Worker access key.
* `logs\heybuddy.log` — rotating 1 MB × 3. Logs states, sizes and errors, not
  transcripts or replies.
* `recordings\*.wav` — **only** if "Keep a .wav of each question" is enabled
  (off by default).

## API keys

The client contains **no** API keys. Anthropic, AssemblyAI and ElevenLabs
keys live as Cloudflare Worker secrets (`wrangler secret put ...`). The
AssemblyAI key never leaves the Worker; the client gets a 480-second
temporary token.

## Protecting your Worker

A Worker URL is a bearer capability: anyone who learns it can spend your API
credit. Set these on the Worker (all optional, all backwards compatible):

```bash
npx wrangler secret put APP_SHARED_SECRET     # then paste the same value into Settings → Worker access key
# in wrangler.toml [vars]:
ALLOWED_MODELS = "claude-sonnet-5-5,claude-haiku-5-5"
MAX_TOKENS_CAP = "4096"
```

The shared secret is stored in the client's settings file, so treat it as a
speed bump against drive-by abuse, not as strong authentication. For a public
distribution put real per-user auth (or Cloudflare Access) in front of it,
and set spend limits in each provider's console.

## Windows integration

* The global keyboard hook (`WH_KEYBOARD_LL`) only looks at virtual-key codes
  to detect the chord; it never records or transmits keystrokes, and it
  always passes every key on (`CallNextHookEx`).
* Autostart writes a single value under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (per user, no admin).
* No elevation is ever requested.

## Reporting

Open a private security advisory on the GitHub repository.
