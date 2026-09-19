"""Market calendar abstraction for Indian equities (NSE/BSE).

Provides trading day detection, market holiday schedules, session hours,
and query helpers so the execution loop and venue never treat a closed session
as trading or assume absent ticks indicate an offline cache should be used.
"""

from __future__ import annotations

from datetime import date, datetime, time as clock_time
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Regular equity cash market session
MARKET_OPEN = clock_time(9, 15)
MARKET_CLOSE = clock_time(15, 30)

# Known NSE trading holidays (format YYYY-MM-DD)
# Kept backend-driven. Weekends (Sat/Sun) are automatically non-trading days.
NSE_HOLIDAYS: set[date] = {
    # 2024
    date(2024, 1, 22),   # Special Holiday
    date(2024, 1, 26),   # Republic Day
    date(2024, 3, 8),    # Mahashivratri
    date(2024, 3, 25),   # Holi
    date(2024, 3, 29),   # Good Friday
    date(2024, 4, 11),   # Id-Ul-Fitr
    date(2024, 4, 17),   # Shri Ram Navami
    date(2024, 5, 1),    # Maharashtra Day
    date(2024, 5, 20),   # General Parliamentary Elections
    date(2024, 6, 17),   # Bakri Id
    date(2024, 7, 17),   # Moharram
    date(2024, 8, 15),   # Independence Day
    date(2024, 10, 2),   # Mahatma Gandhi Jayanti
    date(2024, 11, 1),   # Diwali Laxmi Pujan (Muhurat trading separate)
    date(2024, 11, 15),  # Guru Nanak Jayanti
    date(2024, 11, 20),  # Maharashtra Assembly Elections
    date(2024, 12, 25),  # Christmas
    # 2025
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr
    date(2025, 4, 10),   # Shri Mahavir Jayanti
    date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Mahatma Gandhi Jayanti / Dussehra
    date(2025, 10, 21),  # Diwali Laxmi Pujan
    date(2025, 10, 22),  # Diwali Balipratipada
    date(2025, 11, 5),   # Prakash Gurpurb Sri Guru Nanak Dev
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 17),   # Mahashivratri
    date(2026, 3, 3),    # Holi
    date(2026, 3, 20),   # Id-Ul-Fitr
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Id
    date(2026, 6, 26),   # Muharram
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 9),   # Diwali Laxmi Pujan
    date(2026, 11, 10),  # Diwali Balipratipada
    date(2026, 11, 24),  # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
}


@runtime_checkable
class MarketCalendar(Protocol):
    """Protocol for exchange calendars."""

    def is_trading_day(self, dt: date | datetime) -> bool: ...
    def is_holiday(self, dt: date | datetime) -> bool: ...
    def is_market_open(self, dt: datetime | None = None) -> bool: ...
    def session_bounds(self, dt: date | datetime) -> tuple[datetime, datetime]: ...
    def reason_closed(self, dt: datetime | None = None) -> str | None: ...


class NSEMarketCalendar:
    """The National Stock Exchange of India (NSE) cash market calendar.

    Handles:
    - Monday through Friday trading days
    - Gazetted exchange holidays
    - 09:15 to 15:30 IST session windows
    """

    def __init__(
        self,
        holidays: set[date] | None = None,
        market_open: clock_time = MARKET_OPEN,
        market_close: clock_time = MARKET_CLOSE,
    ) -> None:
        self.holidays: set[date] = set(holidays) if holidays is not None else set(NSE_HOLIDAYS)
        self.market_open = market_open
        self.market_close = market_close

    def add_holiday(self, holiday: date | str) -> None:
        """Add a holiday date."""
        if isinstance(holiday, str):
            self.holidays.add(date.fromisoformat(holiday))
        else:
            self.holidays.add(holiday)

    def is_holiday(self, dt: date | datetime) -> bool:
        """Whether the date is an exchange holiday (or weekend)."""
        d = dt.date() if isinstance(dt, datetime) else dt
        if d.weekday() >= 5:
            return True
        return d in self.holidays

    def is_trading_day(self, dt: date | datetime) -> bool:
        """Whether trading takes place on this calendar date."""
        d = dt.date() if isinstance(dt, datetime) else dt
        if d.weekday() >= 5:
            return False
        return d not in self.holidays

    def session_bounds(self, dt: date | datetime) -> tuple[datetime, datetime]:
        """Return the (open, close) timezone-aware IST datetimes for a date."""
        d = dt.date() if isinstance(dt, datetime) else dt
        start = datetime.combine(d, self.market_open, tzinfo=IST)
        end = datetime.combine(d, self.market_close, tzinfo=IST)
        return start, end

    def is_market_open(self, dt: datetime | None = None) -> bool:
        """Whether the market is actively open at `dt` (defaults to current time)."""
        if dt is None:
            now = datetime.now(IST)
        elif dt.tzinfo is None:
            now = dt.replace(tzinfo=IST)
        else:
            now = dt.astimezone(IST)
        if not self.is_trading_day(now.date()):
            return False
        return self.market_open <= now.time() <= self.market_close

    def reason_closed(self, dt: datetime | None = None) -> str | None:
        """Human-readable explanation of why the market is closed, or None if open."""
        if dt is None:
            now = datetime.now(IST)
        elif dt.tzinfo is None:
            now = dt.replace(tzinfo=IST)
        else:
            now = dt.astimezone(IST)
        d = now.date()
        if d.weekday() >= 5:
            day_name = now.strftime("%A")
            return f"weekend ({day_name})"
        if d in self.holidays:
            return f"exchange holiday ({d.isoformat()})"
        t = now.time()
        if t < self.market_open:
            return f"pre-market (opens at {self.market_open.strftime('%H:%M')} IST)"
        if t > self.market_close:
            return f"post-market (closed at {self.market_close.strftime('%H:%M')} IST)"
        return None


# Global singleton calendar
_DEFAULT_CALENDAR: MarketCalendar = NSEMarketCalendar()


def get_market_calendar() -> MarketCalendar:
    """Retrieve the standard market calendar."""
    return _DEFAULT_CALENDAR


def is_market_open(now: datetime | None = None) -> bool:
    """Convenience helper checking whether the cash market is currently open."""
    return _DEFAULT_CALENDAR.is_market_open(now)
