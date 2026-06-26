# HeyBuddy Worker

Cloudflare Worker proxy that mediates the three upstream APIs HeyBuddy talks
to — Anthropic, AssemblyAI, ElevenLabs — so the desktop client never ships
with raw API keys.

This source is copied unchanged from
[farzaa/clicky](https://github.com/farzaa/clicky/tree/main/worker), the macOS
project HeyBuddy ports. Only the worker `name` in `wrangler.toml` has been
renamed (`clicky-proxy` → `heybuddy-proxy`) so deploying it doesn't collide
with the upstream worker if both exist on the same Cloudflare account.

## Two wrangler configs (intentional)

There are two `wrangler.toml` files in the repo:

* **`/wrangler.toml`** (repo root) — read by Cloudflare Workers Builds. Its
  `main` is `worker/src/index.ts`. This file exists so Cloudflare correctly
  detects the repo as a Workers project even when the project's
  "Root directory" setting is left at its default (root). Without it,
  Cloudflare sees `requirements-desktop.txt` and tries to install Python.
* **`/worker/wrangler.toml`** — read by local development from inside this
  folder (`cd worker && npx wrangler dev`). Its `main` is `src/index.ts`.

The two configs must stay in sync. If you change `name`, `vars`,
`compatibility_date`, etc., update both.

## Routes

| Route | Purpose | Notes |
| --- | --- | --- |
| `POST /chat` | Forwards to `api.anthropic.com/v1/messages` | Streams SSE through |
| `POST /tts` | Forwards to ElevenLabs TTS for the configured voice | Returns `audio/mpeg` |
| `POST /transcribe-token` | Fetches a short-lived AssemblyAI v3 streaming token | Returns JSON |

The Worker's top-level handler rejects every non-POST request, so **all three
routes must be POST** from the client — even `/transcribe-token`, which is
semantically a fetch.

## Deploy

### One-time setup

```bash
cd worker
npm install

npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put ASSEMBLYAI_API_KEY
npx wrangler secret put ELEVENLABS_API_KEY
```

`ELEVENLABS_VOICE_ID` is a Worker **variable** (not secret) so it lives in
`wrangler.toml`. Replace the default with the ElevenLabs voice you want
HeyBuddy to speak with.

### Deploy

```bash
npx wrangler deploy
```

Wrangler prints the deployed URL — paste it into `config/settings.json` of
the desktop client under `worker_url`.

### Deploy via Cloudflare's GitHub integration

If you want Cloudflare to redeploy on every push:

1. In the Cloudflare dashboard, **Workers & Pages → Create application →
   Pages → Connect to Git** (the GitHub flow also works for Workers).
2. Pick this repo, set the **root directory** to `worker`.
3. Build command: `npm install` (no separate build step — wrangler bundles).
4. Add the three API keys as encrypted environment variables in the
   Cloudflare project settings.

Pushing to `main` will then redeploy the Worker automatically.

## Local dev

```bash
# Put your dev keys in worker/.dev.vars (gitignored)
echo 'ANTHROPIC_API_KEY = "sk-ant-..."' >  .dev.vars
echo 'ASSEMBLYAI_API_KEY = "..."'         >> .dev.vars
echo 'ELEVENLABS_API_KEY = "..."'         >> .dev.vars
echo 'ELEVENLABS_VOICE_ID = "..."'        >> .dev.vars

npx wrangler dev
# Point HeyBuddy's worker_url at http://127.0.0.1:8787 while developing.
```

## License

MIT — same as upstream.
