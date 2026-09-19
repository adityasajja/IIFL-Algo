"""Alert rule evaluation — pure logic, no broker calls."""

import pandas as pd

from atr.alerts.channels import LogChannel, TelegramChannel, escape_markdown
from atr.alerts.engine import evaluate
from atr.alerts.models import AlertRule
from atr.alerts.store import AlertStore


def _frame(n=80, start=100.0, step=1.0):
    closes = [start + i * step for i in range(n)]
    return pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=n),
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1000] * n,
    })


def test_price_below_fires_when_falling():
    rule = AlertRule(symbol="X-EQ", kind="price_below", threshold=95.0)
    hit, msg = evaluate(rule, {"ltp": 90.0, "day_chg": -5.0}, None)
    assert hit
    assert "90.0" in msg


def test_price_below_quiet_above_level():
    rule = AlertRule(symbol="X-EQ", kind="price_below", threshold=95.0)
    hit, _ = evaluate(rule, {"ltp": 100.0, "day_chg": 0.0}, None)
    assert not hit


def test_day_drop_fires():
    rule = AlertRule(symbol="X-EQ", kind="day_drop_pct", threshold=3.0)
    hit, msg = evaluate(rule, {"ltp": 90.0, "day_chg": -4.2}, None)
    assert hit
    assert "-4.2%" in msg


def test_rsi_below_fires_on_sink():
    rule = AlertRule(symbol="X-EQ", kind="rsi_below", threshold=30.0)
    hit, _ = evaluate(rule, {"ltp": 50.0, "day_chg": -9.0}, _frame(step=-1.0))
    assert hit


def test_rsi_quiet_on_rally():
    rule = AlertRule(symbol="X-EQ", kind="rsi_below", threshold=30.0)
    hit, _ = evaluate(rule, {"ltp": 180.0, "day_chg": 9.0}, _frame(step=1.0))
    assert not hit


def test_store_roundtrip(tmp_path):
    store = AlertStore(tmp_path)
    rule = AlertRule(symbol="RELIANCE-EQ", kind="price_below", threshold=1250.0)
    store.upsert(rule)
    assert store.rules()[0].threshold == 1250.0
    assert store.remove(rule.id)
    assert store.rules() == []


def test_channels():
    assert LogChannel().send("t", "b") is True
    assert TelegramChannel("", "").send("t", "b") is False


def test_escape_markdown_neutralises_rule_names():
    # Rule names carry underscores; Telegram reads "_" as an italic delimiter.
    assert escape_markdown("trend_break") == "trend\\_break"
    assert escape_markdown("a*b`c[d") == "a\\*b\\`c\\[d"


def test_telegram_send_escapes_report_body(monkeypatch):
    """Regression: an odd underscore count used to make Telegram return 400
    and drop the whole report (2026-09-18 12:03 run)."""
    captured = {}

    class _Resp:
        status_code = 200
        text = "ok"

    def _fake_post(url, json=None, timeout=None):
        captured.update(json)
        return _Resp()

    monkeypatch.setattr("atr.alerts.channels.httpx.post", _fake_post)
    ch = TelegramChannel("tok", "123")
    assert ch.send("ATR signals", "  HINDALCO-EQ | trend_break: x") is True
    assert captured["parse_mode"] == "Markdown"
    assert captured["text"].count("\\_") == 1
    assert "trend\\_break" in captured["text"]


def test_telegram_send_falls_back_to_plain_text(monkeypatch):
    """If Telegram rejects the Markdown payload, the report must still land."""
    calls = []

    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "Bad Request: can't parse entities"

    def _fake_post(url, json=None, timeout=None):
        calls.append(json)
        return _Resp(400 if len(calls) == 1 else 200)

    monkeypatch.setattr("atr.alerts.channels.httpx.post", _fake_post)
    ch = TelegramChannel("tok", "123")
    assert ch.send("t", "b") is True
    assert len(calls) == 2
    assert "parse_mode" not in calls[1]
