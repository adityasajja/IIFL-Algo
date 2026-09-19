"""The forward test only means something if nothing can be edited after the fact."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from atr.research import forward_tracker as ft


def _frame(closes, start="2026-08-03"):
    ts = pd.bdate_range(start, periods=len(closes)) + pd.Timedelta(hours=9, minutes=15)
    return pd.DataFrame({"ts": ts, "close": np.asarray(closes, dtype=float)})


def _falling_jumpy(n=60):
    # A steady slide with big daily swings: oversold and volatile at the last bar.
    rng = np.random.default_rng(1)
    return 200 * np.cumprod(1 + rng.normal(-0.012, 0.03, n))


def _calm(n=60):
    return 100 * np.cumprod(1 + np.random.default_rng(2).normal(0.0004, 0.004, n))


def test_only_oversold_and_jumpy_stocks_are_flagged():
    frames = {"JUMPY": _frame(_falling_jumpy())}
    frames.update({f"CALM{i}": _frame(_calm()) for i in range(20)})

    _, signals = ft.compute_signals(frames)

    assert [s["symbol"] for s in signals] == ["JUMPY"]


def test_a_stock_with_a_stale_last_bar_is_not_treated_as_today():
    fresh = _frame(_falling_jumpy())
    stale = _frame(_falling_jumpy(50))  # ends earlier
    frames = {"FRESH": fresh, "STALE": stale, **{f"C{i}": _frame(_calm()) for i in range(20)}}

    as_of, signals = ft.compute_signals(frames)

    assert as_of == fresh["ts"].iloc[-1].date()
    assert "STALE" not in {s["symbol"] for s in signals}


def test_signals_are_only_written_down_on_fridays_and_only_once(tmp_path):
    store = ft.Store(tmp_path / "t.json")
    sig = [{"symbol": "A", "entry_close": 100.0, "rsi": 20.0, "vol20_pct": 4.0}]

    assert ft.record(store, date(2026, 9, 17), sig) == 0  # a Thursday
    assert ft.record(store, date(2026, 9, 18), sig) == 1  # a Friday
    assert ft.record(store, date(2026, 9, 18), sig) == 0  # the same Friday again
    assert len(store.read()["signals"]) == 1


def test_a_signal_is_graded_only_once_a_full_week_has_passed(tmp_path):
    store = ft.Store(tmp_path / "t.json")
    frame = _frame([100.0] * 10 + [100, 101, 102, 103, 104, 110, 111], start="2026-09-01")
    entry = frame["ts"].iloc[9].date()  # the last flat bar
    store.write({"rule": ft.RULE, "signals": [{"symbol": "A", "entry_date": entry.isoformat(), "entry_close": 100.0, "status": "open"}]})

    assert ft.grade(store, {"A": frame.iloc[:14]}) == 0  # only 4 bars after entry
    assert ft.grade(store, {"A": frame}) == 1  # now 5 bars after: exit close is 104

    graded = store.read()["signals"][0]
    assert graded["status"] == "closed"
    assert graded["exit_close"] == 104.0
    assert graded["ret_pct"] == 4.0
    assert graded["hit"] is True


def test_a_closed_signal_is_never_regraded(tmp_path):
    store = ft.Store(tmp_path / "t.json")
    store.write({"rule": ft.RULE, "signals": [{"symbol": "A", "entry_date": "2026-09-04", "entry_close": 100.0,
                                               "status": "closed", "hit": False, "ret_pct": -3.0, "exit_close": 97.0}]})
    frame = _frame([100.0] * 30, start="2026-08-20")

    assert ft.grade(store, {"A": frame}) == 0
    assert store.read()["signals"][0]["ret_pct"] == -3.0


def test_the_summary_refuses_a_verdict_until_there_is_enough_to_judge(tmp_path):
    store = ft.Store(tmp_path / "t.json")
    store.write({"rule": ft.RULE, "signals": [
        {"symbol": str(i), "entry_date": "2026-09-04", "status": "closed", "hit": True, "ret_pct": 5.0} for i in range(10)
    ]})

    s = ft.summary(store)

    assert s["state"] == "collecting"  # ten straight wins still prove nothing
    assert s["hit_rate_pct"] == 100.0
    assert s["range_pct"][0] < 100  # and the range admits it


def test_a_clear_result_gets_a_clear_verdict(tmp_path):
    def run(hits, n):
        store = ft.Store(tmp_path / f"t{hits}.json")
        store.write({"rule": ft.RULE, "signals": [
            {"symbol": str(i), "entry_date": "d", "status": "closed", "hit": i < hits, "ret_pct": 3.0 if i < hits else -2.0}
            for i in range(n)
        ]})
        return ft.summary(store)["state"]

    assert run(150, 200) == "working"  # 75%: far above the ordinary 32%
    assert run(40, 200) == "not_working"  # 20%: well under the claimed 45%


def test_wilson_range_narrows_with_more_data():
    narrow = ft.wilson(450, 1000)
    wide = ft.wilson(9, 20)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])
    assert ft.wilson(0, 0) == (0.0, 1.0)
