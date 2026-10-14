/**
 * HeyBuddy / Clicky Proxy Worker
 *
 * Proxies requests to Claude, ElevenLabs and AssemblyAI so the desktop app
 * never ships with raw API keys. Keys are stored as Cloudflare secrets.
 *
 * Routes (all POST):
 *   /chat              → Anthropic Messages API (streaming)
 *   /tts               → ElevenLabs TTS API
 *   /transcribe-token  → short-lived AssemblyAI realtime token
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
}

const MAX_CHAT_BODY_BYTES = 12 * 1024 * 1024; // a few 1280px JPEGs + history
const MAX_TTS_TEXT_CHARS = 5000;

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

    if (request.method !== "POST") {
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
