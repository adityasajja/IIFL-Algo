"""Per-endpoint rate limits for the IIFL API, applied before every request.

Source: developers.iiflcapital.com -> Annexure -> Rate Limits, "Session level rate limiting (OPS)",
read on 2026-09-21. Each API has its own limit per session, in calls per second, with one figure
for non-registered apps (under 10 orders/second) and one for registered ones. The **non-registered**
figure is used here, because that is what an individual account gets. The docs do not say what IIFL
does when a limit is exceeded; v1 was refused with HTTP 429.

We stay at 80% of each limit (rounded down, at least 1 call a second) and a call that would exceed
the limit waits.

The limit belongs to the *session*, not to a Python object, so the calls are counted where every
client of that session can see them: in a small SQLite file next to the session file. The server, a
script run beside it and a second ``IiflClient`` all draw from the same allowance. If that file
cannot be used, counting falls back to this process alone, and says so once: a broken limiter must
not stop a trading call from being made.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

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


class MemoryWindow:
    """Counts calls inside this process only. The fallback, and what tests use."""

    def __init__(self) -> None:
        self._calls: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def try_take(self, key: str, limit: int, clock: Callable[[], float]) -> float:
        """Count a call and return 0.0 if it is allowed, else how long to wait before trying again."""
        with self._lock:
            now = clock()
            window = self._calls.setdefault(key, deque())
            while window and now - window[0] >= 1.0:
                window.popleft()
            if len(window) < limit:
                window.append(now)
                return 0.0
            return 1.0 - (now - window[0])


class SharedWindow:
    """Counts calls in a SQLite file, so every process using the session shares one allowance.

    ``BEGIN IMMEDIATE`` makes count-then-insert atomic across processes and threads.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS calls (key TEXT NOT NULL, ts REAL NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS calls_key_ts ON calls (key, ts)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def try_take(self, key: str, limit: int, clock: Callable[[], float]) -> float:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = clock()  # read only once we hold the lock, so counted times are in commit order
            conn.execute("DELETE FROM calls WHERE ts < ?", (now - 60.0,))  # housekeeping
            (count, oldest) = conn.execute(
                "SELECT COUNT(*), MIN(ts) FROM calls WHERE key = ? AND ts >= ?", (key, now - 1.0)
            ).fetchone()
            if count < limit:
                conn.execute("INSERT INTO calls (key, ts) VALUES (?, ?)", (key, now))
                conn.execute("COMMIT")
                return 0.0
            conn.execute("COMMIT")
            return 1.0 - (now - oldest)
        except BaseException:
            conn.execute("ROLLBACK") if conn.in_transaction else None
            raise
        finally:
            conn.close()


class RateLimits:
    """Waits, before a call, until IIFL's documented limit for that endpoint allows it."""

    def __init__(
        self,
        window: MemoryWindow | SharedWindow | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._window = window or MemoryWindow()
        self._fallback = MemoryWindow()
        self._now, self._sleep = now, sleep
        self._warned = False

    def wait(self, method: str, path: str, scope: str = "") -> float:
        """Block until this call is allowed, count it, and return how long it waited."""
        bucket, documented = bucket_for(method, path)
        key, limit = f"{scope}|{bucket}", allowed(documented)
        waited = 0.0
        while True:
            try:
                delay = self._window.try_take(key, limit, self._now)
            except Exception as error:  # noqa: BLE001 - never let the limiter stop a call
                if not self._warned:
                    log.warning("shared rate limiter unavailable (%s); limiting this process only", error)
                    self._warned = True
                delay = self._fallback.try_take(key, limit, self._now)
            if delay <= 0.0:
                return waited
            self._sleep(delay)
            waited += delay
