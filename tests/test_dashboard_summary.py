"""Tests for the /dashboard/summary aggregation.

These cover the two classes of bug that actually shipped here and would have
been invisible from a passing build:

1. Reading only one spelling of a polymorphic broker field, so real positions
   summed to zero while `count` still looked right.
2. Reporting an unknown as a zero — an absent prior close silently became
   "no change today".

The second now has a deliberate fallback (the local daily cache) with a sanity
bound, so the tests pin down all three outcomes: broker value, cache value,
and neither.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atr.api import main as M  # noqa: E402


class FakeClient:
    """Minimal stand-in exposing only what the summary endpoint calls."""

    def __init__(self, positions, trades):
        self._positions = positions
        self._trades = trades

    def positions(self):
        return self._positions

    def trades(self):
        return self._trades

    def limits(self):
        return {}

    def close(self):
        pass


@pytest.fixture()
def summary(monkeypatch):
    """Build a summary from supplied broker rows, bypassing the cache."""

    def _run(positions, trades=(), cache_closes=None):
        monkeypatch.setattr(M, "_authed_client", lambda: FakeClient(positions, list(trades)))
        # Stub the daily-cache lookup so tests never depend on local parquets.
        monkeypatch.setattr(M, "_prior_closes", lambda symbols, exchange="NSEEQ": dict(cache_closes or {}))
        monkeypatch.setattr(M, "_SUMMARY_CACHE", {"data": None, "at": 0.0, "exchange": ""})
        return M.dashboard_summary(exchange="NSEEQ")

    return _run


# ── field-name handling ──────────────────────────────────────────────────────

def test_raw_camelcase_rows_are_summed(summary):
    """IIFL's raw wire spelling must be understood.

    Regression: reading only `quantity`/`last_price` made three real positions
    report value 0.0 while `count` read 3 -- a payload that looks fine.
    """
    out = summary([
        {"tradingSymbol": "A", "netQuantity": 10, "ltp": 100.0, "averagePrice": 90.0},
        {"tradingSymbol": "B", "netQuantity": 5, "ltp": 200.0, "averagePrice": 210.0},
    ])["positions"]

    assert out["count"] == 2
    assert out["value"] == pytest.approx(2000.0)      # 10*100 + 5*200
    assert out["invested"] == pytest.approx(1950.0)   # 10*90 + 5*210
    assert out["unrealized_pnl"] == pytest.approx(50.0)


def test_snake_case_rows_also_work(summary):
    """The module's own /positions emits snake_case; both must resolve."""
    out = summary([
        {"symbol": "A", "quantity": 10, "last_price": 100.0, "avg_price": 90.0},
    ])["positions"]

    assert out["count"] == 1
    assert out["value"] == pytest.approx(1000.0)
    assert out["unrealized_pnl"] == pytest.approx(100.0)


def test_zero_quantity_rows_are_not_positions(summary):
    """A squared-off row is returned by IIFL but is not a holding."""
    out = summary([
        {"tradingSymbol": "A", "netQuantity": 10, "ltp": 100.0, "averagePrice": 90.0},
        {"tradingSymbol": "Z", "netQuantity": 0, "ltp": 500.0, "averagePrice": 490.0},
    ])["positions"]

    assert out["count"] == 1
    assert out["value"] == pytest.approx(1000.0)      # the qty-0 row contributes nothing


# ── prior close / day P&L ────────────────────────────────────────────────────

def test_broker_supplied_prior_close_is_used(summary):
    out = summary([
        {"tradingSymbol": "A", "netQuantity": 10, "ltp": 105.0,
         "averagePrice": 90.0, "prev_close": 100.0},
    ])["positions"]

    assert out["day_pnl"] == pytest.approx(50.0)      # 10 * (105 - 100)
    assert out["day_pnl_complete"] is True
    assert out["day_pnl_from_cache"] == 0


def test_cache_supplied_prior_close_is_used(summary):
    """The broker omits prior close on position rows, so the cache fills it."""
    out = summary(
        [{"tradingSymbol": "A", "netQuantity": 10, "ltp": 105.0, "averagePrice": 90.0}],
        cache_closes={"A": 100.0},
    )["positions"]

    assert out["day_pnl"] == pytest.approx(50.0)
    assert out["day_pnl_complete"] is True
    assert out["day_pnl_from_cache"] == 1


def test_no_prior_close_reports_unknown_not_zero(summary):
    """With neither source available, day P&L is unknown -- never 0.

    Reporting 0 reads as "unchanged today", which is a different and false
    claim for a book that may be moving hard.
    """
    out = summary([
        {"tradingSymbol": "A", "netQuantity": 10, "ltp": 105.0, "averagePrice": 90.0},
    ])["positions"]

    assert out["day_pnl_complete"] is False
    assert out["day_pnl"] == 0.0
    # The rest of the card must still be correct.
    assert out["value"] == pytest.approx(1050.0)
    assert out["unrealized_pnl"] == pytest.approx(150.0)


def test_absurd_prior_close_is_rejected(summary):
    """A wildly mismatched baseline must not become a fabricated day P&L.

    A live price of 2941.50 against a cached close of 1257.50 is a 133% move:
    far more likely a bad baseline (wrong symbol, adjusted vs unadjusted) than
    a real session. Produced "+52.99% in one day" during development.
    """
    out = summary(
        [{"tradingSymbol": "A", "netQuantity": 10, "ltp": 2941.5, "averagePrice": 2870.0}],
        cache_closes={"A": 1257.5},
    )["positions"]

    assert out["day_pnl_complete"] is False
    assert out["day_pnl"] == 0.0
    assert out["value"] == pytest.approx(29415.0)     # value is still real


def test_large_but_plausible_move_is_kept(summary):
    """The bound must not reject a genuine big session."""
    out = summary(
        [{"tradingSymbol": "A", "netQuantity": 10, "ltp": 130.0, "averagePrice": 100.0}],
        cache_closes={"A": 100.0},
    )["positions"]

    assert out["day_pnl_complete"] is True
    assert out["day_pnl"] == pytest.approx(300.0)     # 10 * (130 - 100), +30%


# ── empty book ───────────────────────────────────────────────────────────────

def test_empty_book_is_complete_not_missing(summary):
    """An empty book reports day_pnl_complete True.

    "0 change today" is a complete and correct answer when nothing is held;
    False would imply data is missing and mislead a consumer into thinking a
    fetch failed.
    """
    out = summary([])["positions"]

    assert out["count"] == 0
    assert out["day_pnl"] == 0.0
    assert out["day_pnl_complete"] is True
    assert out["day_pnl_from_cache"] == 0


def test_empty_book_does_not_read_the_cache(monkeypatch):
    """No positions means no prior-close lookup at all."""
    calls = []

    def spy(symbols, exchange="NSEEQ"):
        calls.append(list(symbols))
        return {}

    monkeypatch.setattr(M, "_authed_client", lambda: FakeClient([], []))
    monkeypatch.setattr(M, "_prior_closes", spy)
    monkeypatch.setattr(M, "_SUMMARY_CACHE", {"data": None, "at": 0.0, "exchange": ""})

    M.dashboard_summary(exchange="NSEEQ")
    assert calls == [], "cache must not be consulted with an empty book"


# ── performance ──────────────────────────────────────────────────────────────

def test_performance_win_rate_and_drawdown(summary):
    """Win rate over the last 30 closed trades, drawdown from the equity peak."""
    trades = []
    for i in range(36):
        pnl = 820.0 if i % 3 != 2 else -430.0
        trades.append({"realized_pnl": pnl, "ts": f"2026-09-0{(i % 9) + 1}T09:32:00+05:30"})

    perf = summary([], trades)["performance"]

    assert perf["win_rate"] == pytest.approx(66.7, abs=0.05)
    assert perf["wins"] == 20 and perf["losses"] == 10
    assert perf["sample"] == 30
    assert perf["sparkline"], "a dated trade book must produce a series"
    assert len(perf["sparkline"]) <= 7, "sparkline is 7 sessions, not 7 fills"


def test_unreadable_pnl_is_skipped_not_scored_a_loss(summary):
    """A trade with no recognisable P&L field is skipped, not counted as a loss.

    Counting an unknown as a loss would understate the win rate and mislead in
    the opposite direction.
    """
    perf = summary([], [
        {"realized_pnl": 100.0, "ts": "2026-09-01T09:32:00+05:30"},
        {"pnl": 100.0, "ts": "2026-09-02T09:32:00+05:30"},
        {"no_such_field": 1, "ts": "2026-09-03T09:32:00+05:30"},
    ])["performance"]

    assert perf["wins"] == 2
    assert perf["losses"] == 0
    assert perf["win_rate"] == pytest.approx(100.0)


def test_empty_book_yields_null_win_rate(summary):
    perf = summary([], [])["performance"]
    assert perf["win_rate"] is None
    assert perf["sparkline"] == []
    assert perf["current_drawdown_pct"] == 0.0
