# Deploying the HeyBuddy backend

The backend is a small FastAPI app (`heybuddy_backend.main:app`) in a Docker
image (`backend/Dockerfile`, Python 3.12-slim, non-root user, listens on
`$PORT`, default 8080). It keeps its state in one SQLite file, so give it a
**persistent volume** mounted at `/data`.

> Never commit real keys. `backend/.env.example` holds placeholders only.
> Set real values in the platform's secret store.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `HEYBUDDY_ENV` | yes | `production` (image default): enforces HTTPS via `X-Forwarded-Proto` and sends HSTS; refuses weak secrets |
| `HEYBUDDY_PAIRING_SECRET` | yes | long random string; typed once on each phone to pair. Rotate after pairing (existing device tokens keep working) |
| `HEYBUDDY_TOKEN_PEPPER` | yes | long random string; HMAC pepper for device tokens at rest. **Changing it revokes every paired device** |
| `HEYBUDDY_DB_PATH` | yes | `/data/heybuddy.db` (image default) on a persistent volume |
| `AI_GATEWAY_API_KEY` | yes | Vercel AI Gateway key |
| `AI_GATEWAY_BASE_URL` | no | default `https://ai-gateway.vercel.sh/v1` |
| `HEYBUDDY_DEFAULT_MODEL` | no | e.g. `anthropic/claude-sonnet-5.5` |
| `HEYBUDDY_ALLOWED_MODELS` | no | comma-separated allowlist the phone may choose from |
| `HEYBUDDY_MAX_TOKENS_CAP` | no | default 4096 |
| `GRADIUM_API_KEY` | no | enables server TTS (phone falls back to Android TTS without it) |
| `GRADIUM_VOICE_ID`, `GRADIUM_BASE_URL` | no | voice and endpoint |
| `GRADIUM_STT_ENABLED`, `GRADIUM_STT_LANGUAGE` | no | server speech-to-text (off by default) |
| `COMPOSIO_API_KEY` | no | enables connected apps |
| `COMPOSIO_AUTH_CONFIGS` | with Composio | `toolkit:auth_config_id` pairs, e.g. `gmail:ac_123,googlecalendar:ac_456` |
| `COMPOSIO_CALLBACK_URL` | no | an https page you control shown after OAuth |
| `HEYBUDDY_RATE_LIMIT_PER_MINUTE` | no | per device token, default 60 |

Generate secrets with e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## Render

1. New > Web Service > from this repo, **Root directory** `backend`,
   runtime **Docker**.
2. Add a **Disk** mounted at `/data` (1 GB is plenty).
3. Environment: add the variables above as secrets.
4. Health check path: `/healthz`.
5. Render terminates TLS and sets `X-Forwarded-Proto`; the image already runs
   uvicorn with `--proxy-headers`.

## Fly.io

```bash
cd backend
fly launch --no-deploy            # pick a name/region; keep the Dockerfile
fly volumes create heybuddy_data --size 1
# in fly.toml: [mounts] source="heybuddy_data" destination="/data"
#              [http_service] internal_port = 8080, force_https = true
fly secrets set HEYBUDDY_PAIRING_SECRET=... HEYBUDDY_TOKEN_PEPPER=... AI_GATEWAY_API_KEY=...
fly deploy
```

Run a single machine (SQLite is single-writer).

## Railway

1. New project > Deploy from GitHub repo; set the service **root directory**
   to `backend` (Dockerfile is detected).
2. Add a **Volume** mounted at `/data`.
3. Variables: add the table above. Railway provides `PORT`.
4. Generate a public domain (HTTPS).

## Any Docker host

```bash
docker build -t heybuddy-backend backend
docker run -d -p 8080:8080 -v heybuddy-data:/data \
  -e HEYBUDDY_PAIRING_SECRET=... -e HEYBUDDY_TOKEN_PEPPER=... -e AI_GATEWAY_API_KEY=... \
  heybuddy-backend
```

Put it behind a TLS-terminating reverse proxy (Caddy, nginx) that sets
`X-Forwarded-Proto: https`. Don't expose plain HTTP to the internet:
production mode rejects it.

## Pairing a phone

1. Open the app, onboarding step "Connect to your HeyBuddy server" (or
   Settings > Server).
2. Enter `https://your-host` and the pairing secret, tap Connect.
3. The phone receives its own device token (stored encrypted). The secret is
   not stored on the phone.
4. To pair another phone from an already-paired one, pairing reuses the same
   user, so connections are shared.
5. Rotate `HEYBUDDY_PAIRING_SECRET` once your phones are paired.

## Local development with the emulator

```bash
cd backend && cp .env.example .env    # dev values only
HEYBUDDY_ENV=development uvicorn heybuddy_backend.main:app --port 8080
```

In the emulator use `http://10.0.2.2:8080` (or `adb reverse tcp:8080 tcp:8080`
and `http://localhost:8080` on a USB phone). Plain HTTP is allowed only for
these development hosts.

## Verification status

The backend's tests (`pytest`, 39 tests) and `ruff` run in CI on every push
to `android-v1`. Providers are mocked; no deployment to Render/Fly/Railway
was performed while building this, and the steps above have not been run
end-to-end against those platforms.
