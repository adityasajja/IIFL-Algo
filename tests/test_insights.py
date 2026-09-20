"""The insights digest: market scenario, buy ideas, holdings watch, and their honesty rules."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from atr.insights import evidence
from atr.insights.holdings import load_holdings, normalize
from atr.insights.ideas import buy_ideas, classify
from atr.insights.market import changes_since, describe_market, signature
from atr.insights.service import InsightsService, InsightsSettings
from atr.insights.watch import watch_holding


def _summary(regime="BEARISH_TREND", breadth=34.0, vs50=-3.0, month_ago=66.0):
    close = 23000.0
    hist = [{"d": f"2026-08-{i + 1:02d}", "pct": month_ago} for i in range(21)] + [{"d": "2026-09-10", "pct": breadth}]
    return {
        "regime": {"regime": regime},
        "breadth_above_ema50_pct": breadth,
        "benchmark_series": [{"d": "2026-09-18", "c": close, "s50": close / (1 + vs50 / 100), "s200": close * 1.05}],
        "breadth_history": hist,
        "nifty_close": close,
        "nifty_change_1d_pct": 0.3,
        "stocks_as_of": "2026-09-11",
    }


# ------------------------------------------------------------------------- market scenario
def test_the_market_is_described_in_plain_words():
    m = describe_market(_summary())
    assert m["word"] == "Falling" and m["tone"] == "bad"
    assert "34% of stocks are above their 50-day average, down from 66% a month ago" in m["headline"]
    assert m["vs_50d_pct"] == -3.0 and m["vs_200d_pct"] < 0


def test_weak_breadth_is_not_presented_as_a_sell_signal():
    """The measured history says very weak breadth was followed by above-average months."""
    text = evidence.breadth_context(20)
    assert "not been a reason to sell" in text
    assert "above-average" in text


def test_nothing_is_reported_as_changed_on_the_first_run():
    assert changes_since(None, signature(describe_market(_summary()))) == []


def test_a_change_of_scenario_is_reported_once():
    before = signature(describe_market(_summary(regime="BULLISH_TREND", breadth=58.0, vs50=2.0)))
    after = signature(describe_market(_summary(regime="BEARISH_TREND", breadth=34.0, vs50=-3.0)))
    said = changes_since(before, after)
    assert any("turned falling" in s for s in said)
    assert any("below its 50-day" in s for s in said)
    assert any("Breadth moved" in s for s in said)
    assert changes_since(after, after) == []


# ------------------------------------------------------------------------- buy ideas
def _ctx(symbol, rs, *, above50=True, off_high=-20.0, chg=0.5, atr=2.0, close=100.0):
    return SimpleNamespace(
        symbol=symbol, relative_strength_nifty_20d=rs, above_ema50=above50, from_52w_high_pct=off_high,
        change_1d_pct=chg, atr_pct=atr, close=close, sector="X",
    )


def test_only_setups_with_historical_evidence_are_offered():
    assert classify(_ctx("A-EQ", 30), 0.0)[0] == "strong_rs"
    assert classify(_ctx("B-EQ", -18), -5.0)[0] == "oversold"  # down 23% in 20 days
    assert classify(_ctx("C-EQ", 12, off_high=-8, chg=-1), 0.0)[0] == "resting_leader"
    # Breakouts and "leaders at highs" were inconsistent in the study, so they are not ideas.
    assert classify(_ctx("D-EQ", 12, off_high=-1, chg=2.5), 0.0) is None
    assert classify(_ctx("E-EQ", 3), 0.0) is None


def test_ideas_are_capped_and_spread_across_setups():
    ctxs = {f"S{i}-EQ": _ctx(f"S{i}-EQ", 30 + i) for i in range(6)}
    ctxs.update({f"O{i}-EQ": _ctx(f"O{i}-EQ", -20 - i) for i in range(4)})
    ideas = buy_ideas(ctxs, nifty_1m_pct=-3.0, regime="SIDEWAYS", limit=5, per_setup=2)
    assert len(ideas) == 4  # two per setup that has candidates
    assert {i["setup"] for i in ideas} == {"strong_rs", "oversold"}
    assert all(i["stop_price"] < i["price"] for i in ideas)


def test_a_falling_market_adds_a_caution_to_every_idea():
    ideas = buy_ideas({"A-EQ": _ctx("A-EQ", 40)}, nifty_1m_pct=0.0, regime="BEARISH_TREND")
    assert "Keep the size small" in ideas[0]["market_note"]


def test_no_idea_makes_a_forecast():
    for setup in evidence.BUY_SETUPS.values():
        assert "will" not in setup["evidence"].lower()


# ------------------------------------------------------------------------- holdings watch
def _bars(closes, *, spread=1.0):
    ts = pd.bdate_range(end="2026-09-11", periods=len(closes))
    c = np.array(closes, dtype=float)
    s = np.broadcast_to(np.asarray(spread, dtype=float), c.shape)
    return pd.DataFrame(
        {"ts": ts, "open": c, "high": c + s, "low": c - s, "close": c, "volume": 1000.0}
    )


def test_a_big_gain_names_a_protective_level():
    closes = list(np.linspace(100, 130, 60))  # steadily up
    row = watch_holding({"symbol": "X-EQ", "qty": 10, "avg_price": 100.0}, _bars(closes))
    flag = next(f for f in row["flags"] if f["kind"] in ("protect", "slipped"))
    assert flag["kind"] == "protect" and flag["level"] < row["last"]
    assert "not a forecast" in flag["note"]


def test_slipping_under_the_protective_level_is_flagged():
    closes = list(np.linspace(100, 140, 55)) + [128, 125, 123, 122, 121]  # 13% off the peak
    row = watch_holding({"symbol": "X-EQ", "qty": 10, "avg_price": 100.0}, _bars(closes))
    assert any(f["kind"] == "slipped" for f in row["flags"])


def test_a_quiet_stock_near_its_high_is_described_not_called_a_sell():
    closes = list(np.linspace(100, 130, 55)) + [130.1, 130.0, 130.2, 130.1, 130.0]
    row = watch_holding({"symbol": "X-EQ", "qty": 10, "avg_price": 125.0}, _bars(closes, spread=[1.0] * 55 + [0.05] * 5))  # the daily range has shrunk
    quiet = next(f for f in row["flags"] if f["kind"] == "quiet")
    assert "not a reason to sell" in quiet["note"]


def test_a_holding_with_no_price_history_is_reported_without_flags():
    row = watch_holding({"symbol": "X-EQ", "qty": 1, "avg_price": 10.0}, None)
    assert row["flags"] == [] and row["last"] is None


def test_lagging_carries_the_honest_note():
    closes = list(np.linspace(120, 100, 60))
    row = watch_holding({"symbol": "X-EQ", "qty": 5, "avg_price": 120.0}, _bars(closes), rs_20d=-20.0)
    lag = next(f for f in row["flags"] if f["kind"] == "lagging")
    assert "bounced" in lag["note"]


# ------------------------------------------------------------------------- holdings source
def test_holdings_fall_back_to_the_saved_snapshot_without_a_session(tmp_path):
    class Live:
        def holdings(self):
            return [{"symbol": "INFY", "qty": 10, "avg_price": 1000}]

    live = load_holdings(tmp_path, Live())
    assert live["source"] == "live" and live["holdings"][0]["symbol"] == "INFY-EQ"

    later = load_holdings(tmp_path, None)  # session gone
    assert later["source"] == "snapshot" and later["holdings"][0]["symbol"] == "INFY-EQ"
    assert load_holdings(tmp_path / "empty", None)["source"] == "none"


def test_a_broker_failure_uses_the_snapshot_instead_of_silently_watching_nothing(tmp_path):
    class Live:
        def holdings(self):
            return [{"symbol": "TCS", "qty": 5, "avg_price": 3000}]

    load_holdings(tmp_path, Live())

    class Broken:
        def holdings(self):
            raise RuntimeError("401")

    assert load_holdings(tmp_path, Broken())["source"] == "snapshot"


def test_normalize_handles_iifl_field_names():
    rows = normalize({"result": [{"TradingSymbol": "SBIN", "TotalQty": "12", "BuyAvgRate": "800.5"}, {"symbol": ""}]})
    assert rows == [{"symbol": "SBIN-EQ", "qty": 12, "avg_price": 800.5}]


# ------------------------------------------------------------------------- the message
def test_the_message_states_its_data_date_and_limits(tmp_path):
    svc = InsightsService(data_root=tmp_path)
    digest = {
        "market": describe_market(_summary()),
        "changes": ["The market turned falling (it was rising)."],
        "ideas": [{
            "symbol": "ABC", "is_new": True, "setup": "strong_rs", "setup_label": "Beating the market", "vs_nifty_20d_pct": 31.0, "move_20d_pct": 30.0,
            "stop_price": 95.0, "stop_pct": 5.0,
        }],
        "holdings": {"source": "snapshot", "as_of": None, "items": [
            {"symbol": "INFY", "flags": [{"title": "Big gain to protect", "detail": "Up 22%."}]},
        ]},
        "data_as_of": "2026-09-11", "stale": True, "stale_days": 8, "disclaimer": evidence.DISCLAIMER,
    }
    title, body = svc.render(digest, InsightsSettings())
    assert title == "Today's read"
    assert "Market: Falling" in body and "What changed" in body
    assert "ABC (new)" in body and "INFY" in body
    assert "11 Sep (8 days old)" in body
    assert "before costs" in body


def test_switched_off_sections_are_left_out_of_the_message(tmp_path):
    svc = InsightsService(data_root=tmp_path)
    digest = {
        "market": describe_market(_summary()), "changes": ["x"], "ideas": [{"symbol": "ABC", "setup": "strong_rs", "setup_label": "S", "vs_nifty_20d_pct": 1.0, "move_20d_pct": 1.0, "stop_price": 1.0, "stop_pct": 1.0}],
        "holdings": {"source": "none", "as_of": None, "items": []}, "data_as_of": None, "stale": False, "stale_days": None, "disclaimer": "",
    }
    _, body = svc.render(digest, InsightsSettings(market_changes=False, buy_ideas=False))
    assert "What changed" not in body and "Buy ideas" not in body


def test_settings_round_trip(tmp_path):
    svc = InsightsService(data_root=tmp_path)
    assert svc.settings().enabled is True
    svc.save_settings(InsightsSettings(buy_ideas=False, max_buy_ideas=3))
    again = InsightsService(data_root=tmp_path).settings()
    assert again.buy_ideas is False and again.max_buy_ideas == 3


def test_the_daily_send_waits_until_after_the_close_and_only_happens_once(tmp_path, monkeypatch):
    from datetime import datetime

    import atr.insights.service as mod

    svc = InsightsService(data_root=tmp_path)
    sent = []
    monkeypatch.setattr(svc, "send", lambda **k: sent.append(1) or {"sent": True})

    class Clock(datetime):
        current = None

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(mod, "datetime", Clock)
    Clock.current = Clock(2026, 9, 18, 10, 0, tzinfo=mod.IST)  # Friday morning
    assert svc.maybe_send_daily() is None
    Clock.current = Clock(2026, 9, 18, 16, 30, tzinfo=mod.IST)  # after the close
    svc.maybe_send_daily()
    assert sent == [1]
    svc._save_state({"last_sent_date": "2026-09-18"})
    svc.maybe_send_daily()
    assert sent == [1]  # not twice in a day
    Clock.current = Clock(2026, 9, 19, 17, 0, tzinfo=mod.IST)  # Saturday
    assert svc.maybe_send_daily() is None


# ------------------------------------------------------------------------- auth bookkeeping
def test_a_locked_database_cannot_fail_the_login_check(auth_client, monkeypatch):
    """The last-seen stamp used to run inside the read transaction. SQLite refuses a
    read-to-write upgrade instantly when anything else committed in between, so two
    parallel requests from one session (a minute after the last) made one of them
    fail authentication with "database is locked". The stamp is now separate and
    best effort: a failure there must not fail the request."""
    from datetime import timedelta
    from sqlite3 import OperationalError as Locked

    from sqlalchemy.exc import OperationalError

    import atr.auth.service as auth_service

    real = auth_service.utcnow
    monkeypatch.setattr(auth_service, "utcnow", lambda: real() + timedelta(minutes=5))  # stamp is now stale

    def locked(*args, **kwargs):
        raise OperationalError("UPDATE sessions", {}, Locked("database is locked"))

    monkeypatch.setattr(auth_service.SessionRepository, "touch", staticmethod(locked))
    assert auth_client.get("/api/v1/auth/me").status_code == 200


def test_normalize_reads_the_field_names_the_broker_actually_uses():
    """The broker sends nseTradingSymbol/totalQuantity/averageTradedPrice; missing these
    once saved an empty snapshot and the page said 'no holdings on record'."""
    rows = [{"nseTradingSymbol": "HINDALCO-EQ", "totalQuantity": 15, "averageTradedPrice": 647.72}]

    assert normalize(rows) == [{"symbol": "HINDALCO-EQ", "qty": 15, "avg_price": 647.72}]


def test_an_empty_snapshot_falls_back_to_the_saved_holdings_export(tmp_path):
    (tmp_path / "insights").mkdir()
    (tmp_path / "insights" / "holdings.json").write_text('{"as_of": "2026-09-19T00:00:00+00:00", "holdings": []}')
    (tmp_path / "portfolio").mkdir()
    (tmp_path / "portfolio" / "holdings.csv").write_text(
        "nseTradingSymbol,totalQuantity,averageTradedPrice\nTCS-EQ,4,3100.5\n"
    )

    got = load_holdings(tmp_path)

    assert got["source"] == "export"
    assert got["holdings"] == [{"symbol": "TCS-EQ", "qty": 4, "avg_price": 3100.5}]
