"""The plan's paper trades must follow mechanically from the bars, with no room for hindsight."""

from datetime import date, timedelta

import numpy as np
import pytest

from atr.research import gap_plan as gp

MON = date(2026, 9, 21)  # a Monday


def _bars(days, opens, highs, lows, closes):
    return gp.Bars(
        dates=np.array([np.datetime64(d, "D") for d in days]),
        o=np.array(opens, float), h=np.array(highs, float), l=np.array(lows, float), c=np.array(closes, float),
    )


def _week(open_, high, low, close, entry=100.0):
    """Five sessions from Monday, each given as (open, high, low, close) lists."""
    days = [MON + timedelta(days=i) for i in range(5)]
    return days, open_, high, low, close


def _trade(entry=100.0):
    return {"entry_date": str(MON), "entry": entry}


def test_a_touch_of_the_target_on_monday_is_a_cheap_day_trade():
    b = _bars(*_week([100] * 5, [103.5, 101, 101, 101, 101], [99] * 5, [102] * 5))

    r = gp.grade_trade(_trade(), b)

    assert (r["exit_reason"], r["exit_price"], r["won"]) == ("target", 103.0, True)
    assert r["net_pct"] == pytest.approx(3.0 - 100 * gp.INTRADAY_ROUND_TRIP, abs=0.01)


def test_the_same_target_a_day_later_pays_the_dearer_delivery_cost():
    b = _bars(*_week([100] * 5, [101, 103.5, 101, 101, 101], [99] * 5, [100] * 5))

    r = gp.grade_trade(_trade(), b)

    assert r["exit_reason"] == "target"
    assert r["net_pct"] == pytest.approx(3.0 - 100 * gp.DELIVERY_ROUND_TRIP, abs=0.01)
    assert gp.DELIVERY_ROUND_TRIP > gp.INTRADAY_ROUND_TRIP


def test_a_bar_touching_both_stop_and_target_counts_as_the_stop():
    b = _bars(*_week([100] * 5, [104, 101, 101, 101, 101], [94, 99, 99, 99, 99], [100] * 5))

    r = gp.grade_trade(_trade(), b)

    assert (r["exit_reason"], r["won"]) == ("stop", False)
    assert r["exit_price"] == 95.0


def test_a_gap_through_the_stop_fills_at_the_worse_open():
    b = _bars(*_week([100, 90, 90, 90, 90], [101, 91, 91, 91, 91], [99, 89, 89, 89, 89], [100, 90, 90, 90, 90]))

    r = gp.grade_trade(_trade(), b)

    assert (r["exit_reason"], r["exit_price"]) == ("stop", 90.0)  # not the 95 stop price
    assert r["net_pct"] < -10


def test_a_gap_up_through_the_target_fills_at_the_better_open():
    b = _bars(*_week([100, 108, 108, 108, 108], [101, 109, 109, 109, 109], [99, 107, 107, 107, 107], [100, 108, 108, 108, 108]))

    r = gp.grade_trade(_trade(), b)

    assert (r["exit_reason"], r["exit_price"]) == ("target", 108.0)


def test_a_trade_that_reaches_neither_is_sold_at_fridays_close():
    b = _bars(*_week([100] * 5, [101] * 5, [99] * 5, [100, 100, 100, 100, 101.5]))

    r = gp.grade_trade(_trade(), b)

    assert (r["exit_reason"], r["exit_price"], r["won"]) == ("friday", 101.5, False)


def test_a_trade_stays_open_until_the_week_can_decide_it():
    days = [MON + timedelta(days=i) for i in range(3)]  # only Monday to Wednesday exist
    b = _bars(days, [100] * 3, [101] * 3, [99] * 3, [100] * 3)

    assert gp.grade_trade(_trade(), b) is None
    # ...but a target already hit on Tuesday is decided immediately, without waiting for Friday.
    b2 = _bars(days, [100, 101, 101], [101, 104, 101], [99, 99, 99], [100, 100, 100])
    assert gp.grade_trade(_trade(), b2)["exit_reason"] == "target"


def _universe(n=60, prior_week=0.02, monday_gap=0.0, gappers=()):
    """A market of ``n`` flat stocks whose prior week rose ``prior_week``, with chosen gap-downs."""
    days = [MON - timedelta(days=d) for d in range(20, 0, -1) if (MON - timedelta(days=d)).weekday() < 5] + [MON]
    bars = {}
    for k in range(n):
        name = f"S{k}"
        prior = 100.0
        base = prior / (1 + prior_week)
        closes = np.full(len(days), base)
        closes[-6:-1] = np.linspace(base, prior, 5)  # the week before Monday rises smoothly
        closes[-1] = prior
        opens = closes.copy()
        gap = -0.03 if name in gappers else monday_gap
        opens[-1] = closes[-2] * (1 + gap)
        bars[name] = _bars(days, opens, opens * 1.001, opens * 0.999, closes)
    return bars


def test_only_stocks_that_gapped_down_over_a_percent_are_entries():
    bars = _universe(gappers={"S1", "S2"}, monday_gap=-0.005)  # everyone else is only 0.5% down

    median, entries = gp.entries_for_week(bars, MON)

    assert {e["symbol"] for e in entries} == {"S1", "S2"}
    assert all(e["gap"] < -0.01 for e in entries)
    assert median > 0.01  # last week rose


def test_a_market_that_did_not_rise_is_flagged_so_it_never_counts_as_the_strategy(tmp_path):
    bars = _universe(prior_week=0.0, gappers={"S1"})
    store = gp.Store(tmp_path / "gp.json")

    gp.update(store, bars, today=MON - timedelta(days=1))

    trade = store.read()["trades"][0]
    assert trade["market_ok"] is False
    assert gp.summary(store, bars)["live"]["graded"] == 0  # and it is not in the strategy's own numbers


def test_recording_twice_does_not_duplicate_and_marks_replay_versus_live(tmp_path):
    bars = _universe(gappers={"S1"})
    store = gp.Store(tmp_path / "gp.json")

    gp.update(store, bars, today=MON - timedelta(days=1))  # switched on the day before Monday
    first = gp.update(store, bars, today=MON)  # a second run finds nothing new
    trades = store.read()["trades"]

    assert first["recorded"] == 0
    assert len(trades) == 1
    assert trades[0]["source"] == "live"  # this Monday started after switch-on
    assert store.read()["live_from"] == str(MON)

    # Switched on the Wednesday after: that Monday is now history, so it is replay.
    other = gp.Store(tmp_path / "late.json")
    gp.update(other, bars, today=MON + timedelta(days=2))
    assert other.read()["trades"][0]["source"] == "replay"


def test_the_summary_refuses_a_verdict_until_there_are_enough_live_trades(tmp_path):
    bars = _universe(gappers={"S1"})
    store = gp.Store(tmp_path / "gp.json")
    gp.update(store, bars, today=MON - timedelta(days=1))

    s = gp.summary(store, bars)

    assert s["verdict"].startswith("Too early")
    assert s["needed"] == gp.MIN_LIVE_TRADES


def test_this_weeks_call_follows_the_markets_prior_week():
    rising, flat = _universe(prior_week=0.03), _universe(prior_week=0.0)

    assert gp.market_now(rising)["status"] == "trade"
    assert gp.market_now(flat)["status"] == "skip"
    assert gp.market_now({})["status"] == "unknown"  # no data is not a green light


def test_next_monday_is_always_in_the_future():
    assert gp.next_monday(date(2026, 9, 20)) == date(2026, 9, 21)  # Sunday
    assert gp.next_monday(date(2026, 9, 21)) == date(2026, 9, 28)  # a Monday means the next one
    assert gp.next_monday(date(2026, 9, 23)) == date(2026, 9, 28)
