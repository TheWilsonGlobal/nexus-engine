"""Thread-safe sliding window rate limiter with endpoint tiering and HTTP 429 headers."""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

# Default rate limit tiers: tier_name -> (max_requests, window_seconds)
RATE_LIMIT_TIERS: dict[str, tuple[int, int]] = {
    "auth": (5, 60),
    "agent": (15, 60),
    "ingestion": (10, 60),
    "general": (120, 60),
}


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    tier: str
    limit: int
    remaining: int
    reset_after_s: int
    reset_at: int

    @property
    def retry_after(self) -> int:
        return self.reset_after_s

    def headers(self) -> dict[str, str]:
        return SlidingWindowRateLimiter.headers(self)


class SlidingWindowRateLimiter:
    """In-memory sliding window rate limiter scoped per client key and endpoint tier."""

    def __init__(
        self,
        enabled: bool = True,
        tiers: Optional[dict[str, tuple[int, int]]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.enabled = enabled
        self.tiers = dict(tiers or RATE_LIMIT_TIERS)
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = {}
        self._last_cleanup: float = 0.0

    def check(
        self,
        client_key: str,
        tier: str = "general",
        custom_limit: Optional[int] = None,
        window_s: Optional[int] = None,
    ) -> RateLimitResult:
        default_limit, default_win = self.tiers.get(tier, RATE_LIMIT_TIERS["general"])
        limit = custom_limit if custom_limit is not None else default_limit
        win = window_s if window_s is not None else default_win
        now = self._clock()

        if not self.enabled:
            return RateLimitResult(
                allowed=True,
                tier=tier,
                limit=limit,
                remaining=limit,
                reset_after_s=win,
                reset_at=int(now + win),
            )

        bucket_key = f"{tier}:{client_key}"
        cutoff = now - win

        with self._lock:
            if now - self._last_cleanup > 120.0:
                self._cleanup_locked(now)
                self._last_cleanup = now

            q = self._windows.get(bucket_key)
            if q is None:
                q = deque()
                self._windows[bucket_key] = q

            while q and q[0] <= cutoff:
                q.popleft()

            if len(q) >= limit:
                oldest = q[0]
                retry_after = max(1, int(math.ceil((oldest + win) - now)))
                return RateLimitResult(
                    allowed=False,
                    tier=tier,
                    limit=limit,
                    remaining=0,
                    reset_after_s=retry_after,
                    reset_at=int(now + retry_after),
                )

            q.append(now)
            remaining = max(0, limit - len(q))
            oldest = q[0]
            reset_after = max(1, int(math.ceil((oldest + win) - now)))
            return RateLimitResult(
                allowed=True,
                tier=tier,
                limit=limit,
                remaining=remaining,
                reset_after_s=reset_after,
                reset_at=int(now + reset_after),
            )

    @staticmethod
    def headers(res: RateLimitResult) -> dict[str, str]:
        hdrs = {
            "X-RateLimit-Limit": str(res.limit),
            "X-RateLimit-Remaining": str(res.remaining),
            "X-RateLimit-Reset": str(res.reset_at),
        }
        if not res.allowed:
            hdrs["Retry-After"] = str(res.reset_after_s)
        return hdrs

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()

    def _cleanup_locked(self, now: float) -> None:
        max_win = max((w for _, w in self.tiers.values()), default=60)
        cutoff = now - max_win
        empty_keys = []
        for k, q in self._windows.items():
            while q and q[0] <= cutoff:
                q.popleft()
            if not q:
                empty_keys.append(k)
        for k in empty_keys:
            self._windows.pop(k, None)
