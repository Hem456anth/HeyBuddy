"""HeyBuddy backend API.

Routes (all JSON unless noted; everything except /healthz and /v1/pair needs `Authorization: Bearer <device token>`):
  GET    /healthz
  POST   /v1/pair                      pairing secret -> per-device token (hashed at rest)
  DELETE /v1/device                    revoke this device's token
  GET    /v1/config                    models, enabled providers
  POST   /v1/chat                      SSE stream of text / tool_call / done / error events
  POST   /v1/tts                       {text} -> audio/wav (Gradium)            503 when not configured
  POST   /v1/stt                       audio/wav body -> {text} (Gradium)       503 when not configured
  GET    /v1/integrations              enabled toolkits + this user's connections
  POST   /v1/integrations/connect      {toolkit} -> {redirect_url} (open in a Custom Tab)
  DELETE /v1/integrations/{id}         disconnect (also deletes the Composio connected account)
  POST   /v1/integrations/execute      {proposal_id, arguments, confirmed}
  GET    /v1/integrations/logs         redacted action log;  DELETE /v1/integrations/logs[/{id}]
"""
from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__
from .config import Settings
from .logging_setup import log, redact
from .models import ChatRequest, ConnectRequest, ExecuteRequest, PairRequest, TTSRequest
from .providers.composio import ComposioProvider, DisabledIntegrations, IntegrationProvider
from .providers.http import UpstreamError
from .providers.llm import LLMProvider, VercelGatewayProvider
from .providers.voice import DisabledSTT, DisabledTTS, GradiumSTT, GradiumTTS, STTProvider, TTSProvider
from .ratelimit import RateLimiter
from .safety import READ, SERVER_RULES, classify_risk, frame_untrusted, preview, validate_arguments
from .store import Device, Store, args_hash

INTEGRATION_PREFIX = "cx_"
MAX_INTEGRATION_TOOLS = 60


class Providers:
    def __init__(self, llm: LLMProvider | None, tts: TTSProvider, stt: STTProvider, integrations: IntegrationProvider):
        self.llm = llm
        self.tts = tts
        self.stt = stt
        self.integrations = integrations


def build_providers(settings: Settings, client: httpx.AsyncClient) -> Providers:
    llm = (VercelGatewayProvider(settings.gateway_api_key, settings.gateway_base_url, client,
                                 settings.upstream_timeout_seconds) if settings.llm_enabled else None)
    tts: TTSProvider = (GradiumTTS(settings.gradium_api_key, settings.gradium_base_url, settings.gradium_voice_id, client)
                        if settings.tts_enabled else DisabledTTS())
    stt: STTProvider = (GradiumSTT(settings.gradium_api_key, settings.gradium_base_url, settings.gradium_stt_language,
                                   client) if settings.stt_enabled else DisabledSTT())
    integrations: IntegrationProvider = (ComposioProvider(settings.composio_api_key, settings.composio_base_url, client)
                                         if settings.integrations_enabled else DisabledIntegrations())
    return Providers(llm, tts, stt, integrations)


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(settings: Settings | None = None, providers: Providers | None = None,
               store: Store | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = httpx.AsyncClient(follow_redirects=False)
        state["client"] = client
        state["providers"] = providers or build_providers(settings, client)
        state["store"] = store or Store(settings.db_path, settings.token_pepper)
        yield
        await client.aclose()

    # No CORS middleware on purpose: the only client is the Android app, never a browser.
    app = FastAPI(title="HeyBuddy backend", version=__version__, lifespan=lifespan,
                  docs_url=None if settings.production else "/docs", redoc_url=None, openapi_url=None
                  if settings.production else "/openapi.json")
    limiter = RateLimiter(settings.rate_limit_per_minute)
    pair_limiter = RateLimiter(5)

    def get_store() -> Store:
        return state["store"]

    def get_providers() -> Providers:
        return state["providers"]

    # ----- transport security, size limits, redacted access log -----
    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if settings.production and request.url.path != "/healthz":
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            if scheme != "https":
                return JSONResponse({"error": "HTTPS is required"}, status_code=400)
        limit = settings.max_stt_body_bytes if request.url.path == "/v1/stt" else settings.max_chat_body_bytes
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            return JSONResponse({"error": "request too large"}, status_code=413)
        response = await call_next(request)
        if settings.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        log.info("%s %s -> %s", request.method, request.url.path, response.status_code)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
        # Never echo the input back (it can hold screenshots or private text).
        details = [{"loc": [str(part) for part in item.get("loc", [])], "msg": item.get("msg", "")[:200]}
                   for item in error.errors()[:10]]
        return JSONResponse({"error": "invalid request", "details": details}, status_code=422)

    @app.exception_handler(UpstreamError)
    async def upstream_error(_: Request, error: UpstreamError) -> JSONResponse:
        log.warning("upstream %s failed with %s", error.provider, error.status)
        status = 503 if error.status == 503 else 502
        return JSONResponse({"error": redact(error.message)[:300], "provider": error.provider}, status_code=status)

    async def device(request: Request) -> Device:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(401, "missing device token")
        found = get_store().device_for_token(header[7:].strip())
        if found is None:
            raise HTTPException(401, "unknown or revoked device token")
        allowed, retry_after = limiter.allow(found.id)
        if not allowed:
            raise HTTPException(429, "slow down", headers={"Retry-After": str(retry_after)})
        return found

    # ----- health / pairing -----
    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.post("/v1/pair")
    async def pair(body: PairRequest, request: Request) -> dict[str, Any]:
        client_ip = request.client.host if request.client else "unknown"
        allowed, retry_after = pair_limiter.allow("pair:" + client_ip)
        if not allowed:
            raise HTTPException(429, "too many pairing attempts", headers={"Retry-After": str(retry_after)})
        if not settings.pairing_secret:
            raise HTTPException(403, "pairing is disabled on this server")
        if not hmac.compare_digest(body.pairing_secret.encode(), settings.pairing_secret.encode()):
            raise HTTPException(403, "wrong pairing secret")
        # A device that is already paired can add another device to the same user.
        existing = None
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            existing = get_store().device_for_token(header[7:].strip())
        new_device, token = get_store().create_device(body.device_name, existing.user_id if existing else None)
        log.info("paired device %s", new_device.id[:8])
        return {"token": token, "device_id": new_device.id}

    @app.delete("/v1/device")
    async def unpair(current: Device = Depends(device)) -> dict[str, Any]:
        get_store().revoke_device(current.id)
        return {"revoked": True}

    @app.get("/v1/config")
    async def config(current: Device = Depends(device)) -> dict[str, Any]:
        p = get_providers()
        return {
            "version": __version__,
            "llm": p.llm is not None,
            "models": settings.allowed_models,
            "default_model": settings.default_model,
            "max_tokens_cap": settings.max_tokens_cap,
            "tts": p.tts.enabled,
            "stt": p.stt.enabled,
            "integrations": p.integrations.enabled,
            "toolkits": sorted(settings.composio_auth_configs) if p.integrations.enabled else [],
        }

    # ----- chat -----
    async def integration_tools(user_id: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        p = get_providers()
        if not p.integrations.enabled:
            return [], {}
        specs: list[dict[str, Any]] = []
        index: dict[str, dict[str, Any]] = {}
        for connection in get_store().connections(user_id):
            toolkit = connection["toolkit"]
            if toolkit not in settings.composio_auth_configs:
                continue  # toolkit no longer enabled by the server owner
            try:
                tools = await p.integrations.tools(toolkit)
            except UpstreamError:
                continue
            for tool in tools:
                name = INTEGRATION_PREFIX + tool["slug"]
                if len(name) > 64 or len(specs) >= MAX_INTEGRATION_TOOLS:
                    continue
                risk = classify_risk(tool["slug"], toolkit, tool.get("tags"))
                specs.append({"type": "function", "function": {
                    "name": name,
                    "description": f"[{toolkit} integration, {risk}] {tool['description']}"[:1000],
                    "parameters": tool["input_parameters"],
                }})
                index[name] = {**tool, "risk": risk, "toolkit": toolkit}
        return specs, index

    def to_upstream_messages(body: ChatRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": SERVER_RULES + "\n" + body.system}]
        for message in body.messages:
            item = message.model_dump(exclude_none=True)
            if message.role == "tool":
                content = message.content if isinstance(message.content, str) else json.dumps(item.get("content"))
                item["content"] = frame_untrusted("tool_result", content or "")
            messages.append(item)
        return messages

    @app.post("/v1/chat")
    async def chat(body: ChatRequest, current: Device = Depends(device)) -> StreamingResponse:
        p = get_providers()
        if p.llm is None:
            raise HTTPException(503, "the language model is not configured on this server")
        model = body.model or settings.default_model
        if model not in settings.allowed_models:
            raise HTTPException(400, f"model not allowed: {model}")
        tools = [tool.model_dump() for tool in body.tools]
        extra, index = await integration_tools(current.user_id) if body.integrations else ([], {})
        upstream_messages = to_upstream_messages(body)
        max_tokens = min(body.max_tokens, settings.max_tokens_cap)
        local_names = {tool["function"]["name"] for tool in tools}
        llm = p.llm
        store_ = get_store()

        async def events() -> AsyncIterator[str]:
            try:
                async for event in llm.stream(model, upstream_messages, tools + extra, max_tokens):
                    if event.kind == "text":
                        yield sse("text", {"text": event.text})
                    elif event.kind == "tool_call":
                        call = event.tool_call
                        name = call["name"]
                        if name in local_names:
                            yield sse("tool_call", {"id": call["id"], "name": name, "arguments": call["arguments"],
                                                    "kind": "local"})
                        elif name in index:
                            tool = index[name]
                            problem = validate_arguments(tool["input_parameters"], call["arguments"])
                            if problem:
                                yield sse("tool_call", {"id": call["id"], "name": name, "arguments": call["arguments"],
                                                        "kind": "rejected", "error": problem})
                                continue
                            proposal = store_.add_proposal(current.user_id, tool["slug"], tool["toolkit"],
                                                           call["arguments"], tool["risk"])
                            yield sse("tool_call", {
                                "id": call["id"], "name": name, "arguments": call["arguments"], "kind": "integration",
                                "toolkit": tool["toolkit"], "risk": tool["risk"], "proposal_id": proposal,
                                "title": tool["name"], "preview": preview(tool["name"], call["arguments"]),
                            })
                        else:
                            yield sse("tool_call", {"id": call["id"], "name": name, "arguments": call["arguments"],
                                                    "kind": "rejected", "error": "unknown tool"})
                    elif event.kind == "done":
                        yield sse("done", {"finish_reason": event.finish_reason})
            except UpstreamError as error:
                log.warning("chat upstream error %s", error.status)
                yield sse("error", {"message": redact(error.message)[:300], "status": error.status})
            except (httpx.HTTPError, OSError):
                yield sse("error", {"message": "the language model could not be reached"})

        # Starlette cancels this generator when the client disconnects; the provider's `finally`
        # then closes the upstream response, which aborts generation.
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})

    # ----- voice -----
    @app.post("/v1/tts")
    async def tts(body: TTSRequest, current: Device = Depends(device)) -> Response:
        provider = get_providers().tts
        if not provider.enabled:
            raise HTTPException(503, "server text-to-speech is not configured; use the device voice")
        audio = await provider.synthesize(body.text, body.voice)
        return Response(audio, media_type=provider.media_type)

    @app.post("/v1/stt")
    async def stt(request: Request, current: Device = Depends(device)) -> dict[str, Any]:
        provider = get_providers().stt
        if not provider.enabled:
            raise HTTPException(503, "server speech-to-text is not configured; use on-device recognition")
        content_type = request.headers.get("content-type", "audio/wav").split(";")[0].strip()
        if content_type not in {"audio/wav", "audio/pcm", "audio/ogg", "audio/opus"}:
            raise HTTPException(415, "send audio/wav, audio/pcm, audio/ogg or audio/opus")
        audio = bytearray()
        async for chunk in request.stream():
            audio.extend(chunk)
            if len(audio) > settings.max_stt_body_bytes:
                raise HTTPException(413, "audio too long")
        if not audio:
            raise HTTPException(400, "empty audio")
        return {"text": await provider.transcribe(bytes(audio), content_type)}

    # ----- integrations -----
    @app.get("/v1/integrations")
    async def integrations(current: Device = Depends(device)) -> dict[str, Any]:
        p = get_providers()
        connections = get_store().connections(current.user_id)
        statuses: dict[str, str] = {}
        if p.integrations.enabled and connections:
            try:
                statuses = {account["id"]: account["status"] for account in await p.integrations.list_accounts(current.user_id)}
            except UpstreamError:
                statuses = {}
        return {
            "enabled": p.integrations.enabled,
            "toolkits": sorted(settings.composio_auth_configs) if p.integrations.enabled else [],
            "connections": [{"id": c["id"], "toolkit": c["toolkit"],
                             "status": statuses.get(c["connected_account_id"], "UNKNOWN")} for c in connections],
        }

    @app.post("/v1/integrations/connect")
    async def connect(body: ConnectRequest, current: Device = Depends(device)) -> dict[str, Any]:
        p = get_providers()
        auth_config = settings.composio_auth_configs.get(body.toolkit)
        if not p.integrations.enabled or not auth_config:
            raise HTTPException(400, f"{body.toolkit} is not enabled on this server")
        link = await p.integrations.create_link(auth_config, current.user_id, settings.composio_callback_url or None)
        connection_id = get_store().add_connection(current.user_id, body.toolkit, link["connected_account_id"])
        return {"redirect_url": link["redirect_url"], "connection_id": connection_id, "expires_at": link.get("expires_at")}

    @app.delete("/v1/integrations/{connection_id}")
    async def disconnect(connection_id: str, current: Device = Depends(device)) -> dict[str, Any]:
        removed = get_store().remove_connection(current.user_id, connection_id)
        if removed is None:
            raise HTTPException(404, "no such connection")
        try:
            await get_providers().integrations.delete_account(removed["connected_account_id"])
        except UpstreamError:
            log.warning("composio delete failed; local link removed anyway")
        return {"removed": True}

    @app.post("/v1/integrations/execute")
    async def execute(body: ExecuteRequest, current: Device = Depends(device)) -> dict[str, Any]:
        p = get_providers()
        store_ = get_store()
        proposal = store_.take_proposal(current.user_id, body.proposal_id)
        if proposal is None:
            raise HTTPException(404, "this action expired or was already done; ask again")
        if args_hash(body.arguments) != proposal["args_hash"]:
            raise HTTPException(409, "arguments differ from what the model proposed")
        if proposal["risk"] != READ and not body.confirmed:
            raise HTTPException(409, "this action needs your confirmation")
        if proposal["toolkit"] not in settings.composio_auth_configs:
            raise HTTPException(403, "this integration is no longer enabled")
        connection = store_.connection_for(current.user_id, proposal["toolkit"])
        if connection is None:
            raise HTTPException(400, f"connect {proposal['toolkit']} first")
        try:
            result = await p.integrations.execute(proposal["tool_slug"], current.user_id,
                                                  connection["connected_account_id"], body.arguments)
        except UpstreamError as error:
            store_.log_action(current.user_id, proposal["tool_slug"], proposal["risk"], "error", redact(error.message)[:200])
            raise
        status = "ok" if result["successful"] else "failed"
        if result["successful"]:
            store_.mark_proposal_used(proposal["id"])  # failed attempts stay retryable until the proposal expires
        detail = f": {redact(str(result['error']))[:160]}" if result["error"] else ""
        store_.log_action(current.user_id, proposal["tool_slug"], proposal["risk"], status,
                          f"{proposal['tool_slug']} {status}{detail}")
        data = json.dumps(result["data"], ensure_ascii=False, default=str)
        return {"successful": result["successful"], "error": result["error"],
                "data": redact(data[:8000]) + ("..." if len(data) > 8000 else "")}

    @app.get("/v1/integrations/logs")
    async def logs(current: Device = Depends(device)) -> dict[str, Any]:
        return {"logs": get_store().logs(current.user_id)}

    @app.delete("/v1/integrations/logs")
    async def delete_logs(current: Device = Depends(device)) -> dict[str, Any]:
        return {"deleted": get_store().delete_logs(current.user_id)}

    @app.delete("/v1/integrations/logs/{log_id}")
    async def delete_log(log_id: str, current: Device = Depends(device)) -> dict[str, Any]:
        return {"deleted": get_store().delete_logs(current.user_id, log_id)}

    return app
