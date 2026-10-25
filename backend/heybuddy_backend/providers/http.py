"""Shared upstream helpers: timeouts and retry with exponential backoff (before any byte is streamed)."""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class UpstreamError(Exception):
    def __init__(self, provider: str, status: int, message: str) -> None:
        super().__init__(f"{provider} error {status}: {message}")
        self.provider = provider
        self.status = status
        self.message = message


def timeout(seconds: float) -> httpx.Timeout:
    return httpx.Timeout(seconds, connect=10.0)


async def with_retries(
    attempt: Callable[[], Awaitable[httpx.Response]],
    retries: int = 2,
    base_delay: float = 0.5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> httpx.Response:
    """Run `attempt` and retry on network errors or retryable status codes. The caller owns the response."""
    last_error: Exception | None = None
    for index in range(retries + 1):
        try:
            response = await attempt()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as error:
            last_error = error
        else:
            if response.status_code not in RETRYABLE_STATUS or index == retries:
                return response
            retry_after = response.headers.get("retry-after")
            await response.aclose()
            if retry_after and retry_after.isdigit():
                await sleep(min(float(retry_after), 10.0))
                continue
        if index < retries:
            await sleep(base_delay * (2 ** index) + random.uniform(0, base_delay / 2))  # noqa: S311 (jitter)
    assert last_error is not None
    raise last_error
