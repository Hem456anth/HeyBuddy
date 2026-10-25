"""Settings from environment variables only (keys never live in code or the app)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _list(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


@dataclass
class Settings:
    env: str = "development"
    pairing_secret: str = ""
    token_pepper: str = ""
    db_path: str = "./data/heybuddy.db"
    gateway_api_key: str = ""
    gateway_base_url: str = "https://ai-gateway.vercel.sh/v1"
    default_model: str = "anthropic/claude-sonnet-5.5"
    allowed_models: list[str] = field(default_factory=lambda: ["anthropic/claude-sonnet-5.5"])
    max_tokens_cap: int = 4096
    gradium_api_key: str = ""
    gradium_base_url: str = "https://api.gradium.ai/api"
    gradium_voice_id: str = "YTpq7expH9539ERJ"
    gradium_stt_enabled: bool = False
    gradium_stt_language: str = "en"
    composio_api_key: str = ""
    composio_base_url: str = "https://backend.composio.dev/api/v3.1"
    composio_auth_configs: dict[str, str] = field(default_factory=dict)
    composio_callback_url: str = ""
    rate_limit_per_minute: int = 60
    upstream_timeout_seconds: float = 60.0
    max_chat_body_bytes: int = 8 * 1024 * 1024
    max_stt_body_bytes: int = 10 * 1024 * 1024
    max_tts_chars: int = 2000

    @property
    def production(self) -> bool:
        return self.env == "production"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.gateway_api_key)

    @property
    def tts_enabled(self) -> bool:
        return bool(self.gradium_api_key)

    @property
    def stt_enabled(self) -> bool:
        return bool(self.gradium_api_key) and self.gradium_stt_enabled

    @property
    def integrations_enabled(self) -> bool:
        return bool(self.composio_api_key) and bool(self.composio_auth_configs)

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        configs: dict[str, str] = {}
        for pair in _list(env("COMPOSIO_AUTH_CONFIGS")):
            if ":" in pair:
                toolkit, auth_config = pair.split(":", 1)
                if toolkit.strip() and auth_config.strip():
                    configs[toolkit.strip().lower()] = auth_config.strip()
        settings = cls(
            env=(env("HEYBUDDY_ENV") or "development").strip().lower(),
            pairing_secret=env("HEYBUDDY_PAIRING_SECRET", ""),
            token_pepper=env("HEYBUDDY_TOKEN_PEPPER", ""),
            db_path=env("HEYBUDDY_DB_PATH", "./data/heybuddy.db"),
            gateway_api_key=env("AI_GATEWAY_API_KEY", ""),
            gateway_base_url=(env("AI_GATEWAY_BASE_URL") or "https://ai-gateway.vercel.sh/v1").rstrip("/"),
            default_model=env("HEYBUDDY_DEFAULT_MODEL") or "anthropic/claude-sonnet-5.5",
            allowed_models=_list(env("HEYBUDDY_ALLOWED_MODELS")) or ["anthropic/claude-sonnet-5.5"],
            max_tokens_cap=int(env("HEYBUDDY_MAX_TOKENS_CAP") or 4096),
            gradium_api_key=env("GRADIUM_API_KEY", ""),
            gradium_base_url=(env("GRADIUM_BASE_URL") or "https://api.gradium.ai/api").rstrip("/"),
            gradium_voice_id=env("GRADIUM_VOICE_ID") or "YTpq7expH9539ERJ",
            gradium_stt_enabled=_bool(env("GRADIUM_STT_ENABLED")),
            gradium_stt_language=env("GRADIUM_STT_LANGUAGE") or "en",
            composio_api_key=env("COMPOSIO_API_KEY", ""),
            composio_base_url=(env("COMPOSIO_BASE_URL") or "https://backend.composio.dev/api/v3.1").rstrip("/"),
            composio_auth_configs=configs,
            composio_callback_url=env("COMPOSIO_CALLBACK_URL", ""),
            rate_limit_per_minute=int(env("HEYBUDDY_RATE_LIMIT_PER_MINUTE") or 60),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.default_model not in self.allowed_models:
            self.allowed_models.insert(0, self.default_model)
        if self.production:
            weak = [name for name, value in (("HEYBUDDY_PAIRING_SECRET", self.pairing_secret),
                                             ("HEYBUDDY_TOKEN_PEPPER", self.token_pepper))
                    if len(value) < 24 or value.startswith("change-me")]
            if weak:
                raise RuntimeError(f"Set strong values for {', '.join(weak)} in production")
