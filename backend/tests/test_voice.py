from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from fastapi.testclient import TestClient

from heybuddy_backend.app import create_app
from heybuddy_backend.store import Store

from .conftest import GRADIUM, make_settings

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24


@respx.mock
def test_tts_uses_documented_gradium_rest_call(client, auth):
    route = respx.post(f"{GRADIUM}/post/speech/tts").mock(return_value=httpx.Response(200, content=WAV,
                                                                                     headers={"content-type": "audio/wav"}))
    response = client.post("/v1/tts", headers=auth, json={"text": "hello there"})
    assert response.status_code == 200 and response.content == WAV
    assert response.headers["content-type"] == "audio/wav"
    request = route.calls[0].request
    assert request.headers["x-api-key"] == "test-gradium-key"
    assert json.loads(request.content) == {"text": "hello there", "voice_id": "YTpq7expH9539ERJ",
                                           "output_format": "wav", "only_audio": True}


@respx.mock
def test_tts_upstream_error_is_502_and_redacted(client, auth):
    respx.post(f"{GRADIUM}/post/speech/tts").mock(return_value=httpx.Response(
        500, text="error from server 1008: API key is revoked or expired"))
    response = client.post("/v1/tts", headers=auth, json={"text": "x"})
    assert response.status_code == 502 and "revoked" in response.json()["error"]


def test_tts_disabled_without_key():
    settings = make_settings(gradium_api_key="")
    with TestClient(create_app(settings, store=Store(":memory:", "p"))) as c:
        token = c.post("/v1/pair", json={"pairing_secret": "pair-secret-for-tests-123456"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert c.post("/v1/tts", headers=headers, json={"text": "x"}).status_code == 503
        assert c.post("/v1/stt", headers={**headers, "content-type": "audio/wav"}, content=WAV).status_code == 503
        assert c.get("/v1/config", headers=headers).json()["tts"] is False


def test_tts_input_limits(client, auth):
    assert client.post("/v1/tts", headers=auth, json={"text": "x" * 2001}).status_code == 422
    assert client.post("/v1/tts", headers=auth, json={"text": "x", "voice": "../../etc"}).status_code == 422


@respx.mock
def test_stt_uses_documented_gradium_asr_call(client, auth):
    ndjson = '{"type":"text","text":"where is","start_s":0.1,"stream_id":0}\n{"type":"end_text","stop_s":0.9}\n' \
             '{"type":"text","text":"bluetooth","start_s":1.0,"stream_id":0}\n'
    route = respx.post(f"{GRADIUM}/post/speech/asr").mock(return_value=httpx.Response(200, text=ndjson))
    response = client.post("/v1/stt", headers={**auth, "content-type": "audio/wav"}, content=WAV)
    assert response.json() == {"text": "where is bluetooth"}
    request = route.calls[0].request
    assert request.headers["x-api-key"] == "test-gradium-key"
    assert request.headers["content-type"] == "audio/wav"
    assert json.loads(parse_qs(urlparse(str(request.url)).query)["json_config"][0]) == {"language": "en"}


def test_stt_rejects_unknown_media(client, auth):
    assert client.post("/v1/stt", headers={**auth, "content-type": "video/mp4"}, content=b"x").status_code == 415
