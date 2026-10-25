from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from heybuddy_backend.app import create_app
from heybuddy_backend.config import Settings
from heybuddy_backend.store import Store

GATEWAY = "https://gateway.test/v1"
GRADIUM = "https://gradium.test/api"
COMPOSIO = "https://composio.test/api/v3.1"


def make_settings(**overrides) -> Settings:
    base = dict(
        env="development",
        pairing_secret="pair-secret-for-tests-123456",
        token_pepper="pepper-for-tests-1234567890",
        db_path=":memory:",
        gateway_api_key="test-gateway-key",
        gateway_base_url=GATEWAY,
        default_model="anthropic/claude-sonnet-5.5",
        allowed_models=["anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-4.5"],
        gradium_api_key="test-gradium-key",
        gradium_base_url=GRADIUM,
        gradium_stt_enabled=True,
        composio_api_key="test-composio-key",
        composio_base_url=COMPOSIO,
        composio_auth_configs={"gmail": "ac_gmail1"},
        rate_limit_per_minute=1000,
    )
    base.update(overrides)
    settings = Settings(**base)
    settings.validate()
    return settings


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def client(settings):
    app = create_app(settings, store=Store(":memory:", settings.token_pepper))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def token(client) -> str:
    response = client.post("/v1/pair", json={"pairing_secret": "pair-secret-for-tests-123456", "device_name": "pixel"})
    assert response.status_code == 200, response.text
    return response.json()["token"]


@pytest.fixture
def auth(token) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
