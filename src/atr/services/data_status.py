"""Where the numbers come from, and how recent they are.

Every figure in the app is built from a handful of stored data sets. This reports each one's
origin, the date of its latest bar, and how many trading sessions behind the market that is,
so the interface can show "fresh", "a day late" or "stale" instead of leaving a person to
guess whether a chart is describing today.

A bar for a trading day is only final after the close, so "expected" is today's session once
it is past 16:30 IST on a trading day, and the previous trading session before that. A cache
that holds yesterday's bar at 11:00 is therefore *current*, not late.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from atr.market_calendar import IST, NSEMarketCalendar

#: After this IST time on a trading day, that day's daily bar is expected to be in the cache.
BAR_FINAL_AFTER = time(16, 30)
#: Names read to find the stock cache's latest date: liquid names that trade every session.
SAMPLE = ("RELIANCE-EQ", "TCS-EQ", "INFY-EQ", "HDFCBANK-EQ", "ICICIBANK-EQ", "SBIN-EQ")

_ORDER = {"fresh": 0, "late": 1, "stale": 2, "missing": 3}


def _quality(data_root: Path) -> dict[str, Any] | None:
    """The last nightly price-history check, or None if it has never run."""
    from atr.data import quality

    found = quality.read(data_root)
    if found is None:
        return None
    return {
        "checked_at": found.get("checked_at"),
        "symbols": found.get("symbols"),
        "jumps": found.get("jumps", []),
        "gaps": found.get("gaps", []),
        "unreadable": found.get("unreadable", []),
    }


def expected_session(now: datetime, cal: NSEMarketCalendar) -> date:
    """The latest trading date whose daily bar should exist by ``now``."""
    local = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    d = local.date()
    if not (cal.is_trading_day(d) and local.time() >= BAR_FINAL_AFTER):
        d -= timedelta(days=1)
    while not cal.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def sessions_behind(as_of: date, expected: date, cal: NSEMarketCalendar) -> int:
    """Trading sessions after ``as_of`` up to and including ``expected`` (0 when current)."""
    n, d = 0, as_of
    while d < expected:
        d += timedelta(days=1)
        if cal.is_trading_day(d):
            n += 1
    return n


def classify(behind: int | None) -> str:
    if behind is None:
        return "missing"
    return "fresh" if behind == 0 else "late" if behind == 1 else "stale"


def _last_day(path: Path) -> date | None:
    try:
        frame = pd.read_parquet(path, columns=["ts"])
    except Exception as exc:  # noqa: BLE001 - an unreadable file is "no data", not an error page
        logger.debug("data status: could not read {}: {}", path, exc)
        return None
    if frame.empty:
        return None
    return pd.to_datetime(frame["ts"]).max().date()


def _source(path: Path, default: str | None = None) -> str | None:
    try:
        return path.with_suffix(".source").read_text(encoding="utf8").strip() or default
    except OSError:
        return default


def is_demo(data_root: Path) -> bool:
    """True when the data directory was filled by ``scripts/seed_demo.py``.

    Demo data is synthetic. Every surface that shows numbers built from it must say so, so a
    screenshot of the demo can never be mistaken for a result.
    """
    return (Path(data_root) / ".demo").exists()


def build_status(data_root: Path, now: datetime | None = None, cal: NSEMarketCalendar | None = None) -> dict[str, Any]:
    from atr.data.eod_refresh import cache_dir, read_state
    from atr.data.indices import NIFTY_50, index_path

    cal = cal or NSEMarketCalendar()
    now = now or datetime.now(IST)
    expected = expected_session(now, cal)

    # --- stock prices
    root = cache_dir(data_root)
    files = [root / f"{s}.parquet" for s in SAMPLE if (root / f"{s}.parquet").exists()]
    if not files and root.exists():
        files = sorted(root.glob("*.parquet"))[:6]
    days = [d for d in (_last_day(p) for p in files) if d]
    prices_as_of = max(days) if days else None

    # --- the benchmark index
    ipath = index_path(data_root, NIFTY_50)
    index_as_of = _last_day(ipath) if ipath.exists() else None

    def entry(id_: str, label: str, role: str, origin: str, as_of: date | None) -> dict[str, Any]:
        behind = sessions_behind(as_of, expected, cal) if as_of else None
        return {
            "id": id_,
            "label": label,
            "role": role,
            "origin": origin,
            "as_of": as_of.isoformat() if as_of else None,
            "sessions_behind": behind,
            "status": classify(behind),
        }

    state = read_state(data_root)
    sources = [
        entry(
            "prices", "Stock prices", "Charts, scans, backtests and every paper valuation",
            "Daily bars from IIFL's history, topped up from public bars when the broker is not logged in",
            prices_as_of,
        ),
        entry(
            "index", "Nifty 50", "The yardstick every result is compared with",
            _source(ipath, "Daily index bars") or "Daily index bars",
            index_as_of,
        ),
    ]
    topup = {
        "id": "topup",
        "label": "Daily top-up",
        "ran_at": state.get("ran_at"),
        "ran_date_ist": state.get("ran_date_ist"),
        "updated": state.get("ok"),
        "failed": state.get("fail"),
        "names": state.get("names"),
    } if state else None

    worst = max((s["status"] for s in sources), key=_ORDER.__getitem__)
    return {
        "expected_session": expected.isoformat(),
        "checked_at": now.isoformat(timespec="seconds"),
        "market_open": cal.is_market_open(now),
        "overall": worst,
        "sources": sources,
        "topup": topup,
        "samples": len(files),
        "quality": _quality(data_root),
        "demo": is_demo(data_root),
    }
