from __future__ import annotations

from fastapi.testclient import TestClient

from heybuddy_backend.app import create_app
from heybuddy_backend.logging_setup import redact
from heybuddy_backend.ratelimit import RateLimiter
from heybuddy_backend.store import Store

from .conftest import make_settings


def test_health_is_public(client):
    assert client.get("/healthz").json()["ok"] is True


def test_pairing_requires_the_secret(client):
    assert client.post("/v1/pair", json={"pairing_secret": "nope"}).status_code == 403
    assert client.post("/v1/pair", json={"pairing_secret": "pair-secret-for-tests-123456"}).status_code == 200


def test_tokens_are_hashed_at_rest_and_revocable(client, token, auth):
    assert client.get("/v1/config", headers=auth).status_code == 200
    assert client.get("/v1/config").status_code == 401
    assert client.get("/v1/config", headers={"Authorization": "Bearer hb_wrong"}).status_code == 401
    assert client.delete("/v1/device", headers=auth).status_code == 200
    assert client.get("/v1/config", headers=auth).status_code == 401


def test_token_hash_not_plaintext():
    store = Store(":memory:", "pepper-x")
    device, token = store.create_device("phone")
    rows = store._exec("SELECT token_hash FROM devices")
    assert rows[0]["token_hash"] != token and token not in rows[0]["token_hash"]
    assert store.device_for_token(token).id == device.id


def test_second_device_joins_same_user(client, auth):
    first = client.get("/v1/integrations", headers=auth)
    assert first.status_code == 200
    response = client.post("/v1/pair", json={"pairing_secret": "pair-secret-for-tests-123456"}, headers=auth)
    assert response.status_code == 200


def test_config_reports_providers(client, auth):
    data = client.get("/v1/config", headers=auth).json()
    assert data["llm"] and data["tts"] and data["stt"] and data["integrations"]
    assert data["default_model"] == "anthropic/claude-sonnet-5.5"
    assert data["toolkits"] == ["gmail"]


def test_per_token_rate_limit():
    settings = make_settings(rate_limit_per_minute=3)
    with TestClient(create_app(settings, store=Store(":memory:", "p"))) as c:
        token = c.post("/v1/pair", json={"pairing_secret": "pair-secret-for-tests-123456"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        codes = [c.get("/v1/config", headers=headers).status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200] and codes[3] == 429


def test_pairing_attempts_are_rate_limited(client):
    codes = [client.post("/v1/pair", json={"pairing_secret": "guess"}).status_code for _ in range(7)]
    assert codes[-1] == 429


def test_limiter_window():
    limiter = RateLimiter(2)
    assert limiter.allow("k", now=0)[0] and limiter.allow("k", now=1)[0]
    assert not limiter.allow("k", now=2)[0]
    assert limiter.allow("k", now=61)[0]


def test_https_enforced_in_production():
    settings = make_settings(env="production")
    with TestClient(create_app(settings, store=Store(":memory:", "p"))) as c:
        assert c.get("/healthz").status_code == 200
        assert c.post("/v1/pair", json={"pairing_secret": "x"}).status_code == 400
        ok = c.post("/v1/pair", json={"pairing_secret": "pair-secret-for-tests-123456"},
                    headers={"X-Forwarded-Proto": "https"})
        assert ok.status_code == 200
        assert "max-age" in ok.headers["strict-transport-security"]


def test_weak_production_secrets_refused():
    import pytest

    with pytest.raises(RuntimeError):
        make_settings(env="production", pairing_secret="change-me")


def test_oversized_body_rejected(client, auth):
    response = client.post("/v1/chat", headers={**auth, "Content-Length": str(50_000_000)}, content=b"{}")
    assert response.status_code == 413


def test_validation_errors_do_not_echo_input(client, auth):
    secret_text = "my private screen text 12345"
    response = client.post("/v1/chat", headers=auth, json={"messages": [{"role": "system", "content": secret_text}]})
    assert response.status_code == 422
    assert secret_text not in response.text


def test_no_cors_headers(client):
    response = client.options("/v1/chat", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_redaction():
    text = redact("Authorization: Bearer hb_abcdefghijklmnop x-api-key=sk-live-1234567890 data:image/jpeg;base64,AAAA")
    assert "hb_abcdefghijklmnop" not in text and "sk-live-1234567890" not in text and "AAAA" not in text
