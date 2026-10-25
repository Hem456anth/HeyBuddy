"""Integration provider interface + Composio REST v3.1 implementation (server-side only).

Verified against https://docs.composio.dev/reference (REST API v3.1, accessed 2026-10-25):
  base URL  https://backend.composio.dev/api/v3.1     auth header  x-api-key: <COMPOSIO_API_KEY>
  POST   /connected_accounts/link        {auth_config_id, user_id, callback_url?} -> {redirect_url, connected_account_id, ...}
  GET    /connected_accounts             ?user_ids=&toolkit_slugs=   -> {items: [{id, status, toolkit{slug}}]}
  DELETE /connected_accounts/{id}
  GET    /tools                          ?toolkit_slug=&limit=       -> {items: [{slug, name, description, toolkit{slug},
                                                                        input_parameters, tags, ...}]}
  POST   /tools/execute/{tool_slug}      {connected_account_id, user_id, arguments} -> {data, error, successful, log_id}
OAuth tokens never reach HeyBuddy: we keep only connected-account ids.
"""
from __future__ import annotations

import time
from typing import Any, Protocol

import httpx

from .http import UpstreamError, timeout, with_retries


class IntegrationProvider(Protocol):
    enabled: bool

    async def create_link(self, auth_config_id: str, user_id: str, callback_url: str | None) -> dict[str, Any]: ...
    async def list_accounts(self, user_id: str) -> list[dict[str, Any]]: ...
    async def delete_account(self, connected_account_id: str) -> None: ...
    async def tools(self, toolkit: str, limit: int = 40) -> list[dict[str, Any]]: ...
    async def execute(self, tool_slug: str, user_id: str, connected_account_id: str,
                      arguments: dict[str, Any]) -> dict[str, Any]: ...


class ComposioProvider:
    enabled = True
    name = "composio"
    TOOL_CACHE_SECONDS = 600

    def __init__(self, api_key: str, base_url: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._client = client
        self._tool_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "Content-Type": "application/json"}

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        async def attempt() -> httpx.Response:
            return await self._client.request(method, f"{self._base}{path}", headers=self._headers(),
                                              timeout=timeout(30), **kwargs)

        response = await with_retries(attempt)
        if response.status_code >= 400:
            raise UpstreamError(self.name, response.status_code, response.text[:300])
        return response.json() if response.content else {}

    async def create_link(self, auth_config_id: str, user_id: str, callback_url: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"auth_config_id": auth_config_id, "user_id": user_id}
        if callback_url:
            body["callback_url"] = callback_url
        data = await self._call("POST", "/connected_accounts/link", json=body)
        return {"redirect_url": data["redirect_url"], "connected_account_id": data["connected_account_id"],
                "expires_at": data.get("expires_at")}

    async def list_accounts(self, user_id: str) -> list[dict[str, Any]]:
        data = await self._call("GET", "/connected_accounts", params={"user_ids": user_id, "limit": 100})
        return [{"id": item.get("id"), "status": item.get("status"), "toolkit": (item.get("toolkit") or {}).get("slug")}
                for item in data.get("items", [])]

    async def delete_account(self, connected_account_id: str) -> None:
        await self._call("DELETE", f"/connected_accounts/{connected_account_id}")

    async def tools(self, toolkit: str, limit: int = 40) -> list[dict[str, Any]]:
        cached = self._tool_cache.get(toolkit)
        if cached and time.monotonic() - cached[0] < self.TOOL_CACHE_SECONDS:
            return cached[1]
        data = await self._call("GET", "/tools", params={"toolkit_slug": toolkit, "limit": limit})
        items = [{"slug": item["slug"], "name": item.get("name", item["slug"]),
                  "description": (item.get("description") or "")[:900],
                  "toolkit": (item.get("toolkit") or {}).get("slug", toolkit),
                  "input_parameters": item.get("input_parameters") or {"type": "object", "properties": {}},
                  "tags": item.get("tags") or []}
                 for item in data.get("items", []) if not item.get("is_deprecated")]
        self._tool_cache[toolkit] = (time.monotonic(), items)
        return items

    async def execute(self, tool_slug: str, user_id: str, connected_account_id: str,
                      arguments: dict[str, Any]) -> dict[str, Any]:
        body = {"connected_account_id": connected_account_id, "user_id": user_id, "arguments": arguments}
        data = await self._call("POST", f"/tools/execute/{tool_slug}", json=body)
        return {"successful": bool(data.get("successful")), "data": data.get("data"), "error": data.get("error"),
                "log_id": data.get("log_id")}


class DisabledIntegrations:
    enabled = False

    async def create_link(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise UpstreamError("integrations", 503, "integrations are not configured")

    async def list_accounts(self, user_id: str) -> list[dict[str, Any]]:
        return []

    async def delete_account(self, connected_account_id: str) -> None:
        return None

    async def tools(self, toolkit: str, limit: int = 40) -> list[dict[str, Any]]:
        return []

    async def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise UpstreamError("integrations", 503, "integrations are not configured")
