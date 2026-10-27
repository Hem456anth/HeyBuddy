/**
 * HeyBuddy / Clicky Proxy Worker
 *
 * Proxies requests to Claude, ElevenLabs and AssemblyAI so the desktop app
 * never ships with raw API keys. Keys are stored as Cloudflare secrets.
 *
 * Routes (English path, unchanged, all POST):
 *   /chat              → Anthropic Messages API (streaming)
 *   /tts               → ElevenLabs TTS API
 *   /transcribe-token  → short-lived AssemblyAI realtime token
 *
 * Telugu mode (HeyBuddy 0.4) — Gnani Vachana, key in secret GNANI_API_KEY:
 *   GET  /gnani/stt-stream  (WebSocket) → wss://api.vachana.ai/stt/v3/stream
 *        client header `x-heybuddy-lang` (default te-IN) becomes Gnani's
 *        `lang_code`; x-sample-rate and VAD headers pass through; the Worker
 *        adds `x-api-key-id`. Frames are relayed both ways untouched.
 *   POST /gnani/stt         → POST https://api.vachana.ai/stt/v3 (REST, ≤ 60 s; fallback)
 *        body: WAV bytes; header `x-heybuddy-lang` (default te-IN) → `language_code`.
 *   POST /gnani/tts         → POST https://api.vachana.ai/api/v1/tts/inference
 *   POST /gnani/tts-stream  → POST https://api.vachana.ai/api/v1/tts/sse (SSE passthrough)
 *        body {text, language?, speed?, audio_config?}; the Worker injects
 *        `voice` (GNANI_TTS_VOICE, default "Varuni") and `model`
 *        (GNANI_TTS_MODEL, default "timbre-v2.5").
 *   Endpoints as documented at https://docs.gnani.ai (read 2026-10-27).
 *
 * Wire-compatible with upstream Clicky. Optional hardening (all off unless
 * configured, so an unchanged deployment behaves exactly like upstream):
 *   APP_SHARED_SECRET  secret: require header `X-HeyBuddy-Key` to match
 *   ALLOWED_MODELS     var: comma-separated model ids /chat may use
 *   MAX_TOKENS_CAP     var: upper bound forced onto `max_tokens` (e.g. 4096)
 */

interface Env {
  ANTHROPIC_API_KEY: string;
  ELEVENLABS_API_KEY: string;
  ELEVENLABS_VOICE_ID: string;
  ASSEMBLYAI_API_KEY: string;
  APP_SHARED_SECRET?: string;
  ALLOWED_MODELS?: string;
  MAX_TOKENS_CAP?: string;
  GNANI_API_KEY?: string;
  GNANI_TTS_VOICE?: string;
  GNANI_TTS_MODEL?: string;
}

const MAX_CHAT_BODY_BYTES = 12 * 1024 * 1024; // a few 1280px JPEGs + history
const MAX_TTS_TEXT_CHARS = 5000;

const GNANI_STT_STREAM_URL = "https://api.vachana.ai/stt/v3/stream"; // fetch() + Upgrade: websocket
const GNANI_STT_REST_URL = "https://api.vachana.ai/stt/v3";
const MAX_STT_AUDIO_BYTES = 4 * 1024 * 1024; // 60 s of 16 kHz PCM16 is ~1.9 MB
const GNANI_TTS_REST_URL = "https://api.vachana.ai/api/v1/tts/inference";
const GNANI_TTS_SSE_URL = "https://api.vachana.ai/api/v1/tts/sse";
const GNANI_DEFAULT_VOICE = "Varuni"; // Timbre v2.5 Telugu voice (Suhana, Lehara, Lavanya, Yukti, Varuni)
const GNANI_DEFAULT_MODEL = "timbre-v2.5";
const GNANI_STT_LANGS = ["bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "ta-IN", "te-IN"];
const GNANI_TTS_LANGS = ["auto", "hi-IN", "en-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "bn-IN", "gu-IN", "hi-en"];
// Upgrade headers the client may set; everything else is dropped.
const GNANI_STT_PASSTHROUGH_HEADERS = [
  "x-sample-rate", "x-format", "x-vad-threshold", "x-min-silence-ms",
  "x-min-speech-ms", "x-min-segment-ms", "x-max-speech-s",
];
const GNANI_SAMPLE_RATES = [8000, 16000, 22050, 24000, 44100, 48000];
const GNANI_CONTAINERS = ["raw", "wav", "mp3"];

function jsonError(status: number, message: string): Response {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return mismatch === 0;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // The Gnani STT WebSocket is the only GET route (a WebSocket upgrade).
    const isWebSocketRoute = url.pathname === "/gnani/stt-stream";
    if (isWebSocketRoute ? request.method !== "GET" : request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    if (env.APP_SHARED_SECRET) {
      const provided = request.headers.get("x-heybuddy-key") || "";
      if (!timingSafeEqual(provided, env.APP_SHARED_SECRET)) {
        return jsonError(401, "Missing or wrong X-HeyBuddy-Key");
      }
    }

    try {
      if (url.pathname === "/chat") {
        return await handleChat(request, env);
      }

      if (url.pathname === "/tts") {
        return await handleTTS(request, env);
      }

      if (url.pathname === "/transcribe-token") {
        return await handleTranscribeToken(env);
      }

      if (url.pathname.startsWith("/gnani/")) {
        if (!env.GNANI_API_KEY) {
          return jsonError(503, "Telugu mode is not configured: set the GNANI_API_KEY secret");
        }
        if (url.pathname === "/gnani/stt-stream") {
          return await handleGnaniSttStream(request, env);
        }
        if (url.pathname === "/gnani/stt") {
          return await handleGnaniSttRest(request, env);
        }
        if (url.pathname === "/gnani/tts") {
          return await handleGnaniTTS(request, env, false);
        }
        if (url.pathname === "/gnani/tts-stream") {
          return await handleGnaniTTS(request, env, true);
        }
      }
    } catch (error) {
      console.error(`[${url.pathname}] Unhandled error:`, error);
      return new Response(
        JSON.stringify({ error: String(error) }),
        { status: 500, headers: { "content-type": "application/json" } }
      );
    }

    return new Response("Not found", { status: 404 });
  },
};

async function handleChat(request: Request, env: Env): Promise<Response> {
  let body = await request.text();
  if (body.length > MAX_CHAT_BODY_BYTES) {
    return jsonError(413, "Request too large");
  }

  if (env.ALLOWED_MODELS || env.MAX_TOKENS_CAP) {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(body);
    } catch {
      return jsonError(400, "Body must be JSON");
    }
    if (env.ALLOWED_MODELS) {
      const allowed = env.ALLOWED_MODELS.split(",").map((m) => m.trim()).filter(Boolean);
      if (!allowed.includes(String(parsed.model))) {
        return jsonError(400, `Model not allowed: ${parsed.model}`);
      }
    }
    const cap = Number(env.MAX_TOKENS_CAP || 0);
    if (cap > 0) {
      const requested = Number(parsed.max_tokens || cap);
      parsed.max_tokens = Math.min(requested, cap);
    }
    body = JSON.stringify(parsed);
  }

  const response = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "x-api-key": env.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
    },
    body,
  });

  if (!response.ok) {
    const errorBody = await response.text();
    console.error(`[/chat] Anthropic API error ${response.status}: ${errorBody}`);
    return new Response(errorBody, {
      status: response.status,
      headers: { "content-type": "application/json" },
    });
  }

  return new Response(response.body, {
    status: response.status,
    headers: {
      "content-type": response.headers.get("content-type") || "text/event-stream",
      "cache-control": "no-cache",
    },
  });
}

async function handleTranscribeToken(env: Env): Promise<Response> {
  const response = await fetch(
    "https://streaming.assemblyai.com/v3/token?expires_in_seconds=480",
    {
      method: "GET",
      headers: {
        authorization: env.ASSEMBLYAI_API_KEY,
      },
    }
  );

  if (!response.ok) {
    const errorBody = await response.text();
    console.error(`[/transcribe-token] AssemblyAI token error ${response.status}: ${errorBody}`);
    return new Response(errorBody, {
      status: response.status,
      headers: { "content-type": "application/json" },
    });
  }

  const data = await response.text();
  return new Response(data, {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

async function handleTTS(request: Request, env: Env): Promise<Response> {
  const body = await request.text();
  const voiceId = env.ELEVENLABS_VOICE_ID;
  try {
    const parsed = JSON.parse(body);
    if (typeof parsed.text !== "string" || parsed.text.length > MAX_TTS_TEXT_CHARS) {
      return jsonError(400, "`text` must be a string of at most 5000 characters");
    }
  } catch {
    return jsonError(400, "Body must be JSON");
  }

  const response = await fetch(
    `https://api.elevenlabs.io/v1/text-to-speech/${voiceId}`,
    {
      method: "POST",
      headers: {
        "xi-api-key": env.ELEVENLABS_API_KEY,
        "content-type": "application/json",
        accept: "audio/mpeg",
      },
      body,
    }
  );

  if (!response.ok) {
    const errorBody = await response.text();
    console.error(`[/tts] ElevenLabs API error ${response.status}: ${errorBody}`);
    return new Response(errorBody, {
      status: response.status,
      headers: { "content-type": "application/json" },
    });
  }

  return new Response(response.body, {
    status: response.status,
    headers: {
      "content-type": response.headers.get("content-type") || "audio/mpeg",
    },
  });
}

// ---------------------------------------------------------------------------
// Gnani Vachana (Telugu mode)
// ---------------------------------------------------------------------------

function safeCloseCode(code: number | undefined): number {
  // 1005/1006/1015 are reserved and may not be sent in a Close frame.
  if (!code || code === 1005 || code === 1006 || code === 1015 || code < 1000 || code > 4999) {
    return 1000;
  }
  return code;
}

async function handleGnaniSttStream(request: Request, env: Env): Promise<Response> {
  if ((request.headers.get("upgrade") || "").toLowerCase() !== "websocket") {
    return new Response("Expected Upgrade: websocket", { status: 426 });
  }
  const lang = request.headers.get("x-heybuddy-lang") || "te-IN";
  if (!GNANI_STT_LANGS.includes(lang)) {
    return jsonError(400, `Unsupported language: ${lang}`);
  }

  const upstreamHeaders = new Headers({
    Upgrade: "websocket",
    "x-api-key-id": env.GNANI_API_KEY as string,
    lang_code: lang,
  });
  for (const name of GNANI_STT_PASSTHROUGH_HEADERS) {
    const value = request.headers.get(name);
    if (value) upstreamHeaders.set(name, value);
  }

  // Workers open an outbound WebSocket with fetch() + `Upgrade: websocket`
  // (https://developers.cloudflare.com/workers/examples/websockets/).
  const upstreamResponse = await fetch(GNANI_STT_STREAM_URL, { headers: upstreamHeaders });
  const upstream = upstreamResponse.webSocket;
  if (!upstream) {
    const errorBody = await upstreamResponse.text();
    console.error(`[/gnani/stt-stream] upstream refused WebSocket ${upstreamResponse.status}: ${errorBody}`);
    return new Response(errorBody || "Gnani STT refused the WebSocket", {
      status: upstreamResponse.status === 101 ? 502 : upstreamResponse.status || 502,
      headers: { "content-type": "application/json" },
    });
  }
  upstream.accept();

  const pair = new WebSocketPair();
  const [client, server] = Object.values(pair);
  server.accept();

  // Relay frames untouched in both directions: binary PCM up, JSON events down.
  server.addEventListener("message", (event: MessageEvent) => {
    try {
      upstream.send(event.data as string | ArrayBuffer);
    } catch (error) {
      console.error("[/gnani/stt-stream] send upstream failed", error);
    }
  });
  upstream.addEventListener("message", (event: MessageEvent) => {
    try {
      server.send(event.data as string | ArrayBuffer);
    } catch (error) {
      console.error("[/gnani/stt-stream] send to client failed", error);
    }
  });
  server.addEventListener("close", (event: CloseEvent) => {
    try { upstream.close(safeCloseCode(event.code), event.reason); } catch { /* already closed */ }
  });
  upstream.addEventListener("close", (event: CloseEvent) => {
    try { server.close(safeCloseCode(event.code), event.reason); } catch { /* already closed */ }
  });
  server.addEventListener("error", () => {
    try { upstream.close(1011, "client error"); } catch { /* already closed */ }
  });
  upstream.addEventListener("error", () => {
    try { server.close(1011, "upstream error"); } catch { /* already closed */ }
  });

  return new Response(null, { status: 101, webSocket: client });
}

async function handleGnaniSttRest(request: Request, env: Env): Promise<Response> {
  const lang = request.headers.get("x-heybuddy-lang") || "te-IN";
  if (!GNANI_STT_LANGS.includes(lang)) {
    return jsonError(400, `Unsupported language: ${lang}`);
  }
  const audio = await request.arrayBuffer();
  if (audio.byteLength === 0 || audio.byteLength > MAX_STT_AUDIO_BYTES) {
    return jsonError(400, "Body must be a WAV clip of at most 60 seconds");
  }
  const form = new FormData();
  form.append("audio_file", new File([audio], "clip.wav", { type: "audio/wav" }));
  form.append("language_code", lang);
  const response = await fetch(GNANI_STT_REST_URL, {
    method: "POST",
    headers: { "X-API-Key-ID": env.GNANI_API_KEY as string },
    body: form,
  });
  const body = await response.text();
  if (!response.ok) {
    console.error(`[/gnani/stt] Gnani API error ${response.status}: ${body}`);
  }
  return new Response(body, {
    status: response.status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function buildGnaniTtsBody(parsed: Record<string, unknown>, env: Env): Record<string, unknown> | string {
  if (typeof parsed.text !== "string" || !parsed.text.trim() || parsed.text.length > MAX_TTS_TEXT_CHARS) {
    return "`text` must be a non-empty string of at most 5000 characters";
  }
  const language = typeof parsed.language === "string" ? parsed.language : "te-IN";
  if (!GNANI_TTS_LANGS.includes(language)) {
    return `Unsupported language: ${language}`;
  }
  let speed = Number(parsed.speed ?? 1.0);
  if (!Number.isFinite(speed)) speed = 1.0;
  speed = Math.min(1.15, Math.max(0.85, speed));

  const requested = (parsed.audio_config && typeof parsed.audio_config === "object")
    ? parsed.audio_config as Record<string, unknown>
    : {};
  const sampleRate = Number(requested.sample_rate ?? 24000);
  const container = String(requested.container ?? "raw");
  if (!GNANI_SAMPLE_RATES.includes(sampleRate)) return `Unsupported sample_rate: ${sampleRate}`;
  if (!GNANI_CONTAINERS.includes(container)) return `Unsupported container: ${container}`;
  const audioConfig: Record<string, unknown> = {
    sample_rate: sampleRate,
    num_channels: 1,
    sample_width: 2,
    container,
  };
  if (container !== "mp3") audioConfig.encoding = "linear_pcm";

  return {
    text: parsed.text,
    voice: env.GNANI_TTS_VOICE || GNANI_DEFAULT_VOICE,
    model: env.GNANI_TTS_MODEL || GNANI_DEFAULT_MODEL,
    language,
    speed,
    audio_config: audioConfig,
  };
}

async function handleGnaniTTS(request: Request, env: Env, streaming: boolean): Promise<Response> {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(await request.text());
  } catch {
    return jsonError(400, "Body must be JSON");
  }
  const body = buildGnaniTtsBody(parsed, env);
  if (typeof body === "string") {
    return jsonError(400, body);
  }

  const route = streaming ? "/gnani/tts-stream" : "/gnani/tts";
  const response = await fetch(streaming ? GNANI_TTS_SSE_URL : GNANI_TTS_REST_URL, {
    method: "POST",
    headers: {
      "X-API-Key-ID": env.GNANI_API_KEY as string,
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const errorBody = await response.text();
    console.error(`[${route}] Gnani API error ${response.status}: ${errorBody}`);
    return new Response(errorBody, {
      status: response.status,
      headers: { "content-type": "application/json" },
    });
  }

  const fallbackType = streaming ? "text/event-stream" : "application/octet-stream";
  const headers: Record<string, string> = {
    "content-type": response.headers.get("content-type") || fallbackType,
  };
  if (streaming) headers["cache-control"] = "no-cache";
  return new Response(response.body, { status: response.status, headers });
}
