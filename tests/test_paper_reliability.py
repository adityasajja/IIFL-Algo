"""Acceptance tests for genuine forward paper-trading data reliability.

Verifies:
1. Live price freshness:
   - Stale tick -> rejection / no fill
   - No tick -> no fill (never silently uses old-session prices)
   - Max acceptable tick age is configurable
   - Fill records tick timestamp and price age used
2. Market calendar:
   - Respects trading days, weekends, exchange holidays, and session hours
   - Never assumes absent ticks mean market is open
3. Paper runner observability:
   - Reports deployment states: WAITING_FOR_MARKET, WAITING_FOR_TICKS, RUNNING, PAUSED, ERROR
   - Tracks last tick time, tick age, evaluations, orders, and skipped reasons
4. Order idempotency:
   - Restarts and repeated passes do not create duplicate orders
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from atr.market_calendar import MarketCalendar, NSEMarketCalendar
from atr.services.orders import OrderDraft
from atr.services.paper import (
    PaperLedger,
    PaperVenue,
    TickPrice,
    fixed_price_source,
)
from atr.services.runner import (
    DeploymentLoop,
    PaperRunner,
    RunnerConfig,
    RunnerDeploymentState,
)
from tests.test_paper_runner import SYMBOL, _loop, deployment, wired

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN_TIME = datetime(2026, 9, 14, 10, 0, tzinfo=IST)  # Monday 10:00 IST


class MockClockFeed:
    """A price feed returning configurable TickPrice instances."""

    def __init__(self, prices: dict[str, float] | None = None, now: datetime | None = None):
        self.prices = prices or {}
        self.timestamps: dict[str, datetime] = {}
        self.current_time = now or SESSION_OPEN_TIME

    def set_tick(self, symbol: str, price: float, ts: datetime | None = None):
        self.prices[symbol] = price
        self.timestamps[symbol] = ts or self.current_time

    def __call__(self, symbol: str, exchange: str = "NSEEQ") -> TickPrice | None:
        p = self.prices.get(symbol)
        if p is None:
            return None
        ts = self.timestamps.get(symbol, self.current_time)
        return TickPrice(price=p, timestamp=ts)


def test_market_calendar_session_hours_and_holidays():
    """Verify market calendar correctly identifies holidays, weekends, and trading hours."""
    cal = NSEMarketCalendar()

    # Weekend
    saturday = datetime(2026, 9, 12, 11, 0, tzinfo=IST)
    assert not cal.is_market_open(saturday)
    assert "weekend" in (cal.reason_closed(saturday) or "").lower()

    # Pre-market
    pre_market = datetime(2026, 9, 14, 8, 30, tzinfo=IST)
    assert not cal.is_market_open(pre_market)
    assert "pre-market" in (cal.reason_closed(pre_market) or "").lower()

    # Regular session
    session = datetime(2026, 9, 14, 11, 30, tzinfo=IST)
    assert cal.is_market_open(session)
    assert cal.reason_closed(session) is None

    # Post-market
    post_market = datetime(2026, 9, 14, 16, 0, tzinfo=IST)
    assert not cal.is_market_open(post_market)
    assert "post-market" in (cal.reason_closed(post_market) or "").lower()

    # Republic Day holiday
    republic_day = datetime(2026, 1, 26, 11, 0, tzinfo=IST)
    assert not cal.is_market_open(republic_day)
    assert "holiday" in (cal.reason_closed(republic_day) or "").lower()


def test_paper_venue_rejects_stale_ticks_and_records_price_age():
    """Verify PaperVenue rejects orders when tick age exceeds max_tick_age_seconds,
    and records price_timestamp and price_age_seconds upon fill."""
    from atr.execution.oms import OrderDraft
    from atr.services.execution import VENUE_FILLED, VENUE_REJECTED

    clock_now = datetime(2026, 9, 14, 10, 0, 0, tzinfo=IST)
    feed = MockClockFeed(now=clock_now)

    # Tick from 120 seconds ago
    feed.set_tick("INFY", 1500.0, ts=clock_now - timedelta(seconds=120))

    venue = PaperVenue(
        prices=feed,
        clock=lambda: clock_now,
        max_tick_age_seconds=60.0,
    )

    draft = OrderDraft(
        user_id="u1",
        symbol="INFY",
        side="BUY",
        quantity=10,
        mode="PAPER",
        requested_price=1500.0,
    )

    # Stale tick should be REJECTED
    outcome_stale = venue.submit(draft)
    assert outcome_stale.status == VENUE_REJECTED
    assert "stale" in outcome_stale.reject_reason.lower()

    # Fresh tick from 5 seconds ago
    feed.set_tick("INFY", 1500.0, ts=clock_now - timedelta(seconds=5))
    outcome_fresh = venue.submit(draft)
    assert outcome_fresh.status == VENUE_FILLED
    assert outcome_fresh.raw["price_timestamp"] is not None
    assert outcome_fresh.raw["price_age_seconds"] is not None
    assert round(outcome_fresh.raw["price_age_seconds"]) == 5


def test_paper_venue_no_tick_rejects_without_filling():
    """Verify venue rejects orders when no price is available rather than using old data."""
    from atr.execution.oms import OrderDraft
    from atr.services.execution import VENUE_REJECTED

    feed = MockClockFeed()
    venue = PaperVenue(prices=feed)

    draft = OrderDraft(
        user_id="u1",
        symbol="UNKNOWN",
        side="BUY",
        quantity=10,
        mode="PAPER",
        requested_price=100.0,
    )
    outcome = venue.submit(draft)
    assert outcome.status == VENUE_REJECTED
    assert "cannot be priced" in outcome.reject_reason.lower()


def test_runner_deployment_observability_states(wired, deployment):
    """Verify deployment transitions through observability states:
    WAITING_FOR_MARKET, WAITING_FOR_TICKS, RUNNING."""
    runner, feed = wired()
    loop = _loop(runner, deployment)

    # 1. Outside market hours -> WAITING_FOR_MARKET
    night_time = datetime(2026, 9, 14, 21, 0, tzinfo=IST)
    runner.pass_once(now=night_time)

    status = runner.status()
    dep_status = status["deployments"][0]
    assert dep_status["state"] == RunnerDeploymentState.WAITING_FOR_MARKET
    assert "closed" in (dep_status["skipped_reason"] or "").lower()

    # 2. Inside market hours but clear feed so no ticks -> WAITING_FOR_TICKS
    feed.prices.clear()
    runner.pass_once(now=SESSION_OPEN_TIME)
    dep_status = runner.status()["deployments"][0]
    assert dep_status["state"] == RunnerDeploymentState.WAITING_FOR_TICKS
    assert "tick" in (dep_status["skipped_reason"] or "").lower()
    assert dep_status["skipped_evaluations_count"] > 0

    # 3. Fresh ticks provided -> RUNNING
    feed.prices[SYMBOL] = 1000.0
    runner.pass_once(now=SESSION_OPEN_TIME)
    dep_status = runner.status()["deployments"][0]
    assert dep_status["state"] == RunnerDeploymentState.RUNNING
    assert dep_status["last_tick_time"] is not None or dep_status["last_strategy_evaluation"] is not None


def test_order_idempotency_prevents_duplicate_orders(wired, deployment):
    """Verify that restarting the runner or executing repeated passes does not duplicate orders."""
    runner, feed = wired()
    loop = _loop(runner, deployment)

    # First pass: generates order
    tick1 = runner.pass_once(now=SESSION_OPEN_TIME)
    assert tick1.orders == 1

    # Second pass immediately after: must not place duplicate order
    tick2 = runner.pass_once(now=SESSION_OPEN_TIME + timedelta(seconds=1))
    assert tick2.orders == 0

    # Restart runner (simulate server restart)
    runner2, _ = wired(feed=feed)
    _loop(runner2, deployment)
    tick3 = runner2.pass_once(now=SESSION_OPEN_TIME + timedelta(seconds=2))
    assert tick3.orders == 0, "Restart should not place duplicate order for the same daily session"
