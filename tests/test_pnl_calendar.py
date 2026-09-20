"""The calendar's numbers must be arithmetic on the record, and honest about what is estimated."""

import json

import pandas as pd
import pytest

from atr.services import pnl_calendar as pc


def _day(d, pnl, **kw):
    return pc.Day(date=d, pnl=pnl, trades=1, **kw)


def test_month_view_totals_and_counts_only_that_month():
    days = {d.date: d for d in [_day("2026-08-31", 500), _day("2026-09-01", 100), _day("2026-09-02", -40), _day("2026-09-03", 60)]}

    v = pc.month_view(days, "2026-09")

    assert (v["total"], v["traded_days"], v["green_days"], v["red_days"]) == (120.0, 3, 2, 1)
    assert v["best_day"] == {"date": "2026-09-01", "pnl": 100.0}
    assert v["worst_day"] == {"date": "2026-09-02", "pnl": -40.0}
    assert v["months_with_data"] == ["2026-08", "2026-09"]


def test_runs_count_consecutive_finishes_across_months_not_just_the_visible_one():
    days = {d.date: d for d in [_day("2026-08-28", 10), _day("2026-08-31", 10), _day("2026-09-01", 10), _day("2026-09-02", -5),
                                _day("2026-09-03", -5), _day("2026-09-04", 7)]}

    v = pc.month_view(days, "2026-09")

    assert v["best_green"] == 3  # 28 Aug, 31 Aug, 1 Sep
    assert v["worst_red"] == 2
    assert v["current_green"] == 1  # the latest day is green after two reds


def test_an_empty_month_is_empty_not_an_error():
    v = pc.month_view({}, "2026-09")
    assert v["days"] == [] and v["total"] == 0 and v["best_day"] is None


def test_paper_ledger_and_live_gap_trades_are_added_but_replay_trades_are_not(tmp_path):
    (tmp_path / "paper_momentum").mkdir()
    (tmp_path / "paper_momentum" / "settlements.jsonl").write_text(
        json.dumps({"exit_window_end": "2026-06-12", "portfolio_return": 0.02}) + "\n"
    )
    (tmp_path / "research").mkdir()
    trades = [
        {"source": "live", "market_ok": True, "status": "closed", "exit_date": "2026-09-22", "net_pct": 3.0},
        {"source": "replay", "market_ok": True, "status": "closed", "exit_date": "2026-09-22", "net_pct": 3.0},
        {"source": "live", "market_ok": False, "status": "closed", "exit_date": "2026-09-22", "net_pct": 3.0},
        {"source": "live", "market_ok": True, "status": "open"},
    ]
    (tmp_path / "research" / "gap_plan.json").write_text(json.dumps({"trades": trades}))

    days = pc.paper_days(tmp_path, db=_NoJournal())

    assert days["2026-06-12"].pnl == pytest.approx(0.02 * pc.VIRTUAL_TICKET)  # a week's equal-weight return
    assert days["2026-09-22"].pnl == pytest.approx(0.03 * pc.VIRTUAL_TICKET)  # only the one live qualifying trade
    assert days["2026-09-22"].trades == 1


class _NoJournal:
    def session(self):
        raise RuntimeError("no database in this test")


def _closes(values, start="2026-09-01"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=idx, dtype=float)


def test_real_day_pnl_is_quantity_times_the_price_change():
    holdings = [{"symbol": "A-EQ", "qty": 10, "avg_price": 1}, {"symbol": "B-EQ", "qty": 5, "avg_price": 1}]
    closes = {"A-EQ": _closes([100, 102, 101]), "B-EQ": _closes([50, 50, 54])}

    out = pc.compute_real(holdings, closes)

    assert out["2026-09-02"] == pytest.approx(10 * 2 + 5 * 0)
    assert out["2026-09-03"] == pytest.approx(10 * -1 + 5 * 4)


def test_a_day_with_too_few_priced_holdings_is_left_out_not_reported_low():
    holdings = [{"symbol": f"S{i}-EQ", "qty": 1, "avg_price": 1} for i in range(10)]
    closes = {f"S{i}-EQ": _closes([100, 101, 102]) for i in range(10)}
    for i in range(5):  # half the book has no price on the last day
        closes[f"S{i}-EQ"] = closes[f"S{i}-EQ"].iloc[:2]

    out = pc.compute_real(holdings, closes)

    assert "2026-09-02" in out
    assert "2026-09-03" not in out


def test_only_the_day_recorded_on_its_own_date_is_exact_and_exact_days_are_never_overwritten(tmp_path, monkeypatch):
    holdings = [{"symbol": "A-EQ", "qty": 10, "avg_price": 1}]
    series = {"A-EQ": _closes([100, 102, 105])}
    monkeypatch.setattr(pc, "_closes", lambda root, sym: series[sym])

    pc.refresh_real(tmp_path, holdings, today=pd.Timestamp("2026-09-03").date())
    first = pc.real_days(tmp_path)
    assert first["2026-09-03"].estimated is False  # recorded on its own date
    assert first["2026-09-02"].estimated is True  # rebuilt afterwards from today's holdings

    # Holdings change later; the exact day must keep the figure recorded when it was true.
    series["A-EQ"] = _closes([100, 102, 200])
    pc.refresh_real(tmp_path, [{"symbol": "A-EQ", "qty": 99, "avg_price": 1}], today=pd.Timestamp("2026-09-04").date())
    after = pc.real_days(tmp_path)
    assert after["2026-09-03"].pnl == first["2026-09-03"].pnl
