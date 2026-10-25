# Security & privacy

## What leaves the machine

| Data | When | Goes to |
| --- | --- | --- |
| Microphone audio (PCM16) | only while the push-to-talk chord is held | your Worker → AssemblyAI (token), then streamed directly to `streaming.assemblyai.com` |
| Screenshots of every monitor (JPEG, ≤1280 px) | once per question, if "Send screenshot" is on | your Worker → Anthropic |
| Transcript + last 10 exchanges (text) | once per question | your Worker → Anthropic |
| Reply text | once per spoken line, if voice is on | your Worker → ElevenLabs |
| Focused window title + app name, local time | once per question | your Worker → Anthropic (system prompt) |
| Tool results (command output, file text, fetched pages) | only when a tool runs | your Worker → Anthropic |
| `memory.md` facts | once per question, if memory is on | your Worker → Anthropic |
| Job briefs and job tool results | while a background job runs | your Worker → Anthropic |
| Pages you or a job fetch | when `web_fetch` runs | the website itself (direct) |

Nothing is uploaded while idle. HeyBuddy's own windows (the buddy overlay and
the panel) are excluded from capture (`WDA_EXCLUDEFROMCAPTURE`).

## What is stored locally (`%APPDATA%\HeyBuddy`)

* `config\settings.json` — Worker URL, preferences, optional Worker access key.
* `logs\heybuddy.log` — rotating 1 MB × 3. Logs states, sizes and errors, not
  transcripts or replies.
* `recordings\*.wav` — **only** if "Keep a .wav of each question" is enabled
  (off by default).
* `memory.md` — facts you asked it to remember. Plain text; open, edit or
  "Forget all" from Settings → Abilities. Things that look like passwords,
  keys, codes or card numbers are refused.
* `jobs\<name>-<id>\` and `jobs.json` — background job folders and results.
* `workspace\` — scratch folder for files the buddy writes without asking.

No analytics, telemetry or crash reports are collected or sent.

## What the buddy may do on your PC

| Action | Default |
| --- | --- |
| Open http(s) links, copy to clipboard, type at your caret, read the focused window's title | allowed (you asked for it) |
| List folders and read text files | allowed, except protected locations |
| Write files inside its own `workspace` or a job folder | allowed |
| Write files anywhere else | **asks** (Settings: ask / always / never) |
| Run PowerShell commands | **asks** (Settings: ask / always / never) |
| Launch programs or scripts via `open_path` | **asks** |
| Fetch private-network addresses (localhost, 10.x, 192.168.x) | refused |

Protected (never read or written): `.ssh`, `.aws`, `.azure`, `.gnupg`,
`.kube`, Windows credential and DPAPI stores, Chrome/Edge/Firefox/Brave/Opera
profiles, `.env*`, private keys (`*.pem`, `*.pfx`, `id_rsa*`…), password
databases (`*.kdbx`), `auth.json`/`auth.bin`, and HeyBuddy's own settings.
An approval dialog that isn't answered within 120 s counts as "no".

These are guard rails, not a sandbox: a command you approve runs with your
user's rights. Read each prompt before approving.

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
