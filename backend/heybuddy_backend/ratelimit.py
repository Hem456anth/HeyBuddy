"""In-memory sliding-window rate limiter (per device token, and per IP for pairing)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            window = self._hits[key]
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= self.per_minute:
                return False, max(1, int(60 - (now - window[0])) + 1)
            window.append(now)
            return True, 0
