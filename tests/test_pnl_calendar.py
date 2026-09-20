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


def test_a_paper_day_lists_each_trade_that_closed_and_they_add_up_to_the_calendar_figure(tmp_path):
    (tmp_path / "paper_momentum").mkdir()
    (tmp_path / "paper_momentum" / "settlements.jsonl").write_text(
        json.dumps({"exit_window_end": "2026-06-12", "portfolio_return": 0.02, "returns": {"AAA": 0.05, "BBB": -0.01}}) + "\n"
    )
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "gap_plan.json").write_text(json.dumps({"trades": [
        {"source": "live", "market_ok": True, "status": "closed", "exit_date": "2026-06-12", "symbol": "CCC", "entry": 100.0,
         "exit_price": 103.0, "net_pct": 2.8, "exit_reason": "target"},
        {"source": "replay", "market_ok": True, "status": "closed", "exit_date": "2026-06-12", "symbol": "ZZZ", "entry": 1.0,
         "exit_price": 1.0, "net_pct": 9.0, "exit_reason": "target"},
    ]}))

    rows = pc.paper_day_detail(tmp_path, "2026-06-12", db=_NoJournal())
    calendar = pc.paper_days(tmp_path, db=_NoJournal())["2026-06-12"]

    assert {r["symbol"] for r in rows} == {"AAA", "BBB", "CCC"}  # the replayed trade is not one the app took
    assert next(r for r in rows if r["symbol"] == "CCC")["note"] == "hit target"
    # Weekly picks split the virtual ticket equally: 50,000 each.
    assert next(r for r in rows if r["symbol"] == "AAA")["pnl"] == pytest.approx(0.05 * pc.VIRTUAL_TICKET / 2)
    assert rows[0]["pnl"] == max(rows, key=lambda r: abs(r["pnl"]))["pnl"]  # biggest mover first
    assert sum(r["pnl"] for r in rows) == pytest.approx(calendar.pnl, abs=0.02)


def test_a_real_day_names_the_holdings_that_moved_it_and_matches_the_day_total(tmp_path, monkeypatch):
    holdings = [{"symbol": "A-EQ", "qty": 10, "avg_price": 1}, {"symbol": "B-EQ", "qty": 5, "avg_price": 1},
                {"symbol": "GONE-EQ", "qty": 3, "avg_price": 1}]
    series = {"A-EQ": _closes([100, 102, 101]), "B-EQ": _closes([50, 50, 54])}
    monkeypatch.setattr(pc, "_closes", lambda root, sym: series.get(sym))

    detail = pc.real_day_detail(tmp_path, holdings, "2026-09-03")

    assert [r["symbol"] for r in detail["rows"]] == ["B-EQ", "A-EQ"]  # +20 outweighs -10
    assert detail["total"] == pytest.approx(10 * -1 + 5 * 4)
    assert (detail["gainers"], detail["losers"]) == (1, 1)
    assert detail["unpriced"] == ["GONE-EQ"]  # said plainly, not folded into the total
    assert detail["total"] == pytest.approx(pc.compute_real(holdings[:2], series)["2026-09-03"])


def test_the_first_day_of_a_history_has_no_change_to_report(tmp_path, monkeypatch):
    holdings = [{"symbol": "A-EQ", "qty": 10, "avg_price": 1}]
    monkeypatch.setattr(pc, "_closes", lambda root, sym: _closes([100, 101]))

    detail = pc.real_day_detail(tmp_path, holdings, "2026-09-01")

    assert detail["rows"] == [] and detail["unpriced"] == ["A-EQ"]


def test_a_holding_missing_a_bar_cannot_credit_a_multi_day_move_to_one_day(tmp_path, monkeypatch):
    """The calendar and its day view once disagreed here: one compared a stock with an older close."""
    holdings = [{"symbol": "A-EQ", "qty": 10, "avg_price": 1}, {"symbol": "B-EQ", "qty": 10, "avg_price": 1}]
    a = _closes([100, 101, 102, 103])
    b = _closes([50, 51, 52, 60]).drop(a.index[2])  # B has no bar on the third session
    series = {"A-EQ": a, "B-EQ": b}
    monkeypatch.setattr(pc, "_closes", lambda root, sym: series.get(sym))
    monkeypatch.setattr(pc, "MIN_COVERAGE", 0.4)

    for day in ("2026-09-02", "2026-09-03", "2026-09-04"):
        detail = pc.real_day_detail(tmp_path, holdings, day)
        assert detail["total"] == pytest.approx(pc.compute_real(holdings, series).get(day, detail["total"]))

    third = pc.real_day_detail(tmp_path, holdings, "2026-09-03")
    assert [r["symbol"] for r in third["rows"]] == ["A-EQ"]  # B had no bar, so it is unpriced that day
    fourth = pc.real_day_detail(tmp_path, holdings, "2026-09-04")
    assert "B-EQ" in fourth["unpriced"]  # and the next: its previous session's bar is missing too


def test_trading_profit_is_added_to_the_day_and_appears_as_its_own_line(tmp_path, monkeypatch):
    holdings = [{"symbol": "A-EQ", "qty": 10, "avg_price": 1}]
    series = {"A-EQ": _closes([100, 102, 105])}
    monkeypatch.setattr(pc, "_closes", lambda root, sym: series[sym])
    pc.refresh_real(tmp_path, holdings, today=pd.Timestamp("2026-09-03").date())

    pc.set_realised(tmp_path, "2026-09-03", 11000, note="intraday")

    day = pc.real_days(tmp_path)["2026-09-03"]
    assert day.realised == 11000 and day.pnl == pytest.approx(30 + 11000)  # 10 shares x Rs 3, plus the trading profit
    detail = pc.real_day_detail(tmp_path, holdings, "2026-09-03")
    assert detail["holdings_total"] == pytest.approx(30)
    assert detail["realised"]["note"] == "intraday"
    assert detail["total"] == pytest.approx(11030)


def test_a_day_with_only_trading_profit_still_appears_on_the_calendar(tmp_path):
    pc.set_realised(tmp_path, "2026-09-18", 11000)  # a Friday with no holdings reading at all

    days = pc.real_days(tmp_path)

    assert days["2026-09-18"].pnl == 11000
    assert pc.month_view(days, "2026-09")["total"] == 11000


def test_the_nightly_refresh_does_not_bake_trading_profit_into_the_stored_series(tmp_path, monkeypatch):
    holdings = [{"symbol": "A-EQ", "qty": 10, "avg_price": 1}]
    monkeypatch.setattr(pc, "_closes", lambda root, sym: _closes([100, 102, 105]))
    today = pd.Timestamp("2026-09-03").date()
    pc.refresh_real(tmp_path, holdings, today=today)
    pc.set_realised(tmp_path, "2026-09-03", 500)

    pc.refresh_real(tmp_path, holdings, today=pd.Timestamp("2026-09-04").date())
    pc.refresh_real(tmp_path, holdings, today=pd.Timestamp("2026-09-04").date())

    assert pc.real_days(tmp_path)["2026-09-03"].pnl == pytest.approx(30 + 500)  # counted once, however often it refreshes
    assert json.loads(pc.store_path(tmp_path).read_text())["days"]["2026-09-03"]["pnl"] == pytest.approx(30)


def test_a_recorded_profit_can_be_replaced_or_removed_and_bad_input_is_refused(tmp_path):
    pc.set_realised(tmp_path, "2026-09-18", 100)
    pc.set_realised(tmp_path, "2026-09-18", 250)
    assert pc.real_days(tmp_path)["2026-09-18"].pnl == 250

    assert pc.clear_realised(tmp_path, "2026-09-18") is True
    assert pc.clear_realised(tmp_path, "2026-09-18") is False
    assert "2026-09-18" not in pc.real_days(tmp_path)

    with pytest.raises(ValueError):
        pc.set_realised(tmp_path, "2026-13-45", 1)
    with pytest.raises(ValueError):
        pc.set_realised(tmp_path, "2026-09-18", float("nan"))
