"""Charts from the local cache: daily bars need no broker login, intraday still does."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atr.api.main import app
from atr.services.chart_data import cached_candles

LOOPBACK = ("127.0.0.1", 51234)


def test_daily_candles_come_from_the_cache_in_range(master, market_cache):
    out = cached_candles("RELIANCE-EQ", "NSEEQ", "1d", "01-Mar-2024", "31-Mar-2024", cache_root=market_cache)
    assert out is not None and out["source"] == "local_cache"
    days = [c["ts"][:10] for c in out["candles"]]
    assert days == sorted(days) and days[0] >= "2024-03-01" and days[-1] <= "2024-03-31"
    assert set(out["candles"][0]) == {"ts", "open", "high", "low", "close", "volume"}
    assert out["as_of"] >= days[-1]


def test_weekly_and_monthly_are_resampled_from_daily(master, market_cache):
    daily = cached_candles("TCS-EQ", "NSEEQ", "1d", "01-Jan-2024", "31-Mar-2024", cache_root=market_cache)
    weekly = cached_candles("TCS-EQ", "NSEEQ", "1w", "01-Jan-2024", "31-Mar-2024", cache_root=market_cache)
    monthly = cached_candles("TCS-EQ", "NSEEQ", "1mo", "01-Jan-2024", "31-Mar-2024", cache_root=market_cache)
    assert len(monthly["candles"]) == 3 and len(weekly["candles"]) < len(daily["candles"])
    # a month's high is the highest high of its days, its close the last close
    jan = [c for c in daily["candles"] if c["ts"].startswith("2024-01")]
    assert monthly["candles"][0]["high"] == pytest.approx(max(c["high"] for c in jan))
    assert monthly["candles"][0]["close"] == pytest.approx(jan[-1]["close"])


def test_intraday_and_unknown_symbols_cannot_be_answered(master, market_cache):
    assert cached_candles("TCS-EQ", "NSEEQ", "5m", None, None, cache_root=market_cache) is None
    assert cached_candles("NOPE-EQ", "NSEEQ", "1d", None, None, cache_root=market_cache) is None


def test_an_empty_window_is_none_so_the_broker_is_asked_not_an_empty_chart(master, market_cache):
    assert cached_candles("TCS-EQ", "NSEEQ", "1d", "01-Jan-2001", "31-Jan-2001", cache_root=market_cache) is None


def test_the_route_serves_daily_bars_without_a_broker_session(fresh_env, master):
    client = TestClient(app, client=LOOPBACK)
    r = client.get("/candles", params={"symbol": "RELIANCE-EQ", "interval": "1d", "from_date": "01-Mar-2024", "to_date": "31-Mar-2024"})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "local_cache" and r.json()["candles"]


def test_the_route_still_needs_the_broker_for_intraday(fresh_env, master):
    client = TestClient(app, client=LOOPBACK)
    assert client.get("/candles", params={"symbol": "RELIANCE-EQ", "interval": "5m"}).status_code >= 400
