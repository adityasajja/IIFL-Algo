"""Per-endpoint rate limits for the IIFL API, applied before every request.

Source: developers.iiflcapital.com -> Annexure -> Rate Limits, "Session level rate limiting (OPS)",
read on 2026-09-21. Each API has its own limit per session, in calls per second, with one figure
for non-registered apps (under 10 orders/second) and one for registered ones. The **non-registered**
figure is used here, because that is what an individual account gets. The docs do not say what IIFL
does when a limit is exceeded; v1 was refused with HTTP 429.

We stay at 80% of each limit (rounded down, at least 1 call a second), so clock skew or a second
process on the same session does not tip us over. A call that would exceed the limit waits.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable

HEADROOM = 0.8

# (method, path pattern) -> (bucket name, documented calls per second, non-registered)
_LIMITS: list[tuple[str, re.Pattern[str], str, int]] = [
    ("POST", re.compile(r"^/getusersession$"), "get_user_session", 3),
    ("POST", re.compile(r"^/profile/logout$"), "logout", 2),
    ("GET", re.compile(r"^/profile$"), "profile", 3),
    ("GET", re.compile(r"^/limits$"), "limits", 10),
    ("POST", re.compile(r"^/preordermargin$"), "preorder_margin", 10),
    ("POST", re.compile(r"^/spanexposure$"), "span_exposure", 10),
    ("POST", re.compile(r"^/orders$"), "place_order", 10),
    ("PUT", re.compile(r"^/orders/[^/]+$"), "modify_order", 20),
    ("DELETE", re.compile(r"^/orders/[^/]+$"), "cancel_order", 20),
    ("DELETE", re.compile(r"^/orders$"), "cancel_all_orders", 3),
    ("GET", re.compile(r"^/orders$"), "order_book", 3),
    ("GET", re.compile(r"^/orders/[^/]+$"), "order_history", 10),
    ("GET", re.compile(r"^/trades$"), "trade_book", 3),
    ("GET", re.compile(r"^/positions$"), "positions", 3),
    ("GET", re.compile(r"^/holdings$"), "holdings", 3),
    ("POST", re.compile(r"^/marketdata/historicaldata$"), "historical_data", 10),
    ("POST", re.compile(r"^/marketdata/marketdepth$"), "market_depth", 10),
    ("POST", re.compile(r"^/marketdata/openinterest$"), "open_interest", 10),
    ("POST", re.compile(r"^/marketdata/marketquotes$"), "market_quotes", 10),
]
_CONTRACT_FILE = re.compile(r"^/contractfiles/([A-Za-z]+)\.(?:json|csv)$")
_CONTRACT_FILE_LIMIT = 2  # each segment's file has its own limit
_UNKNOWN_LIMIT = 3  # an endpoint we have no figure for gets the strictest common one


def allowed(documented: int) -> int:
    """Calls per second we make: the documented limit less headroom, never below one."""
    return max(1, int(documented * HEADROOM))


def bucket_for(method: str, path: str) -> tuple[str, int]:
    """The (bucket name, documented per-second limit) a request counts against."""
    route = path.split("?", 1)[0].rstrip("/") or "/"
    for verb, pattern, name, limit in _LIMITS:
        if verb == method.upper() and pattern.match(route):
            return name, limit
    contract = _CONTRACT_FILE.match(route)
    if contract:
        return f"contract_{contract.group(1).upper()}", _CONTRACT_FILE_LIMIT
    return f"other:{method.upper()} {route}", _UNKNOWN_LIMIT


class RateLimits:
    """A sliding one-second window per bucket. Safe to call from many threads."""

    def __init__(
        self,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._monotonic, self._sleep = monotonic, sleep
        self._calls: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def wait(self, method: str, path: str) -> float:
        """Block until this call is allowed, count it, and return how long it waited."""
        bucket, documented = bucket_for(method, path)
        limit = allowed(documented)
        waited = 0.0
        while True:
            with self._lock:
                now = self._monotonic()
                window = self._calls.setdefault(bucket, deque())
                while window and now - window[0] >= 1.0:
                    window.popleft()
                if len(window) < limit:
                    window.append(now)
                    return waited
                delay = 1.0 - (now - window[0])
            self._sleep(delay)  # outside the lock, so other buckets are not held up
            waited += delay
