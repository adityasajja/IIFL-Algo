"""In-process rate limiting.

A token bucket per key, guarded by a lock. In-process is the right scope here:
this is a single-process modular monolith, and a Redis-backed limiter would add
an operational dependency to protect one operator's own API.

The important property is that the limiter **fails open on internal error** and
**fails closed on a limit breach** — a bug in the limiter must not take the
trading API down, but a genuine flood must still be refused.

Buckets are pruned lazily: an attacker cycling source IPs would otherwise grow
the dict without bound, which turns a rate limiter into a memory-exhaustion
vector.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float = field(default_factory=time.monotonic)


class RateLimiter:
    """Token buckets keyed by an opaque string (``scope:identity``)."""

    def __init__(self, *, default_per_minute: int = 240, max_keys: int = 10_000) -> None:
        self.default_per_minute = max(1, default_per_minute)
        self.max_keys = max(64, max_keys)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ core
    def check(
        self,
        key: str,
        *,
        per_minute: int | None = None,
        cost: float = 1.0,
    ) -> tuple[bool, float]:
        """Consume ``cost`` tokens. Returns ``(allowed, retry_after_seconds)``.

        ``retry_after`` is 0.0 when allowed.
        """
        rate = max(1, per_minute or self.default_per_minute)
        capacity = float(rate)
        refill_per_second = rate / 60.0
        now = time.monotonic()

        try:
            with self._lock:
                self._prune(now)
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = _Bucket(tokens=capacity, updated=now)
                    self._buckets[key] = bucket

                elapsed = max(0.0, now - bucket.updated)
                bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_per_second)
                bucket.updated = now

                if bucket.tokens >= cost:
                    bucket.tokens -= cost
                    return True, 0.0

                deficit = cost - bucket.tokens
                retry_after = deficit / refill_per_second if refill_per_second > 0 else 60.0
                return False, max(0.001, min(retry_after, 3600.0))
        except Exception:  # noqa: BLE001 - a limiter bug must not stop trading
            return True, 0.0

    # ----------------------------------------------------------------- admin
    def peek(self, key: str) -> float:
        """Remaining tokens, for diagnostics. Does not consume."""
        with self._lock:
            bucket = self._buckets.get(key)
            return float(bucket.tokens) if bucket else 0.0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def _prune(self, now: float) -> None:
        """Drop buckets that have fully refilled and gone quiet.

        Caller holds the lock. A full bucket is indistinguishable from a missing
        one, so discarding it loses nothing.
        """
        if len(self._buckets) <= self.max_keys:
            return
        cutoff = now - 600.0
        stale = [k for k, b in self._buckets.items() if b.updated < cutoff]
        for k in stale:
            del self._buckets[k]
        if len(self._buckets) > self.max_keys:
            # Still over budget (a flood within the last 10 minutes). Drop the
            # oldest half by last-seen rather than refusing to serve.
            ordered = sorted(self._buckets.items(), key=lambda kv: kv[1].updated)
            for k, _ in ordered[: len(ordered) // 2]:
                del self._buckets[k]


_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                from atr.config.settings import get_settings

                _limiter = RateLimiter(default_per_minute=get_settings().rate_limit_per_minute)
    return _limiter


def reset_rate_limiter() -> None:
    """Drop the singleton. Tests use this."""
    global _limiter
    with _limiter_lock:
        _limiter = None


__all__ = ["RateLimiter", "get_rate_limiter", "reset_rate_limiter"]
