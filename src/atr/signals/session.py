"""What the market calendar and the daily cache say about today's session.

A daily frame cannot tell a rule when the day opened, which weekday it is, or how the
market did last week. The runner asks this module and hands the answers to the rules as a
:class:`~atr.signals.models.SessionContext`, so the rules stay pure and a test can pass any
context it likes.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

from atr.market_calendar import IST, MarketCalendar
from atr.signals.models import SessionContext

logger = logging.getLogger("atr.signals.session")

#: The week's last session is sold from this time. The market closes at 15:30, and the
#: last quarter hour leaves room for the order to fill at the close rather than after it.
WEEK_END_EXIT_FROM = time(15, 15)

ROOT = Path(__file__).resolve().parents[3]


def prior_session(today: date, calendar: MarketCalendar) -> date:
    """The trading day before ``today``."""
    day = today - timedelta(days=1)
    while not calendar.is_trading_day(day):
        day -= timedelta(days=1)
    return day


def is_last_session_of_week(today: date, calendar: MarketCalendar) -> bool:
    """True when the next trading day falls in a later week."""
    if not calendar.is_trading_day(today):
        return False
    following = today + timedelta(days=1)
    while not calendar.is_trading_day(following):
        following += timedelta(days=1)
    return following.isocalendar()[:2] != today.isocalendar()[:2]


class MarketWeek:
    """The median stock's gain over the five sessions before a given day, read once a day.

    It is measured over the same long-history stocks the plan was researched on, so the
    number means what it meant in the research. Reading 300-odd files takes seconds, so the
    answer is kept for the day; a runner asks every second.
    """

    def __init__(self, data_root: Path | None = None) -> None:
        self._root = Path(data_root) if data_root else ROOT / "data"
        self._lock = threading.Lock()
        self._cached: tuple[date, float | None] | None = None

    def pct(self, today: date, prior: date) -> float | None:
        """Percent, or None when the cache does not reach the prior session or is too thin."""
        with self._lock:
            if self._cached is not None and self._cached[0] == today:
                return self._cached[1]
            value = self._read(prior)
            self._cached = (today, value)
            return value

    def _read(self, prior: date) -> float | None:
        try:
            from atr.research.gap_plan import load_bars, market_now

            reading = market_now(load_bars(self._root))
        except Exception:  # noqa: BLE001 - no reading means no trade, never a crash
            logger.exception("market week could not be read")
            return None
        if reading.get("median_pct") is None or reading.get("as_of") != str(prior):
            logger.warning(
                "market week not usable: cache reaches %s, need %s", reading.get("as_of"), prior
            )
            return None
        return float(reading["median_pct"])


def week_end_close(now: datetime, calendar: MarketCalendar) -> bool:
    """True on the week's last session from 15:15 IST."""
    local = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    return is_last_session_of_week(local.date(), calendar) and local.time() >= WEEK_END_EXIT_FROM


def near_close(now: datetime) -> bool:
    """True from 15:15 IST: the day's price is close enough to the close to act on."""
    local = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    return local.time() >= WEEK_END_EXIT_FROM


def build_context(
    now: datetime,
    calendar: MarketCalendar,
    market: MarketWeek | None,
    *,
    open_price: float | None,
    minutes_since_open: float | None,
) -> SessionContext:
    local = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    today = local.date()
    prior = prior_session(today, calendar)
    return SessionContext(
        today=today,
        prior_session=prior,
        open_price=open_price,
        minutes_since_open=minutes_since_open,
        market_week_pct=market.pct(today, prior) if market is not None else None,
        week_end_close=week_end_close(now, calendar),
        near_close=near_close(now),
    )
