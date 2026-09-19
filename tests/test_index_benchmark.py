"""The market overview prefers the official Nifty 50 index over the ETF proxy.

The index is downloaded from the broker when a session exists and cached; reading
it never needs a session. These tests use a fake client, so they prove the
plumbing (cache, freshness, preference, fallback) but not IIFL's live response.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from atr.data import indices
from atr.market_intel.models import PROVENANCE_NIFTY50_INDEX
from atr.market_intel.service import MarketIntelService


def _candles(end: date, n: int = 30) -> list[list]:
    days = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    return [[d.strftime("%Y-%m-%d %H:%M:%S"), 25000 + i, 25050 + i, 24950 + i, 25010 + i, 0] for i, d in enumerate(days)]


class FakeClient:
    base_url = "https://example.invalid/v1"

    def __init__(self, end: date | None = None):
        self.end = end or date.today()
        self.calls: list[tuple] = []

    def historical_data(self, exchange, instrument_id, interval, from_date, to_date):
        self.calls.append((exchange, instrument_id, interval))
        return {"result": [{"candles": _candles(self.end)}]}


@pytest.fixture()
def root(tmp_path, monkeypatch):
    # No network: the contract lookup falls back to the known id.
    def no_network(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(indices.httpx, "get", no_network)
    return tmp_path


def test_sync_writes_a_cache_that_loads_without_a_session(root):
    client = FakeClient()
    assert indices.sync_index(client, root) == "ok"
    assert client.calls == [("NSEEQ", "999920000", "1d")]

    frame = indices.load_index(root)  # no client involved
    assert frame is not None and len(frame) == 30


def test_a_second_sync_the_same_day_is_skipped(root):
    client = FakeClient()
    indices.sync_index(client, root)
    assert indices.sync_index(client, root) == "skip"
    assert len(client.calls) == 1


def test_a_failed_sync_keeps_the_previous_file(root):
    indices.sync_index(FakeClient(), root)

    class Broken(FakeClient):
        def historical_data(self, *a, **k):
            raise RuntimeError("session expired")

    assert indices.sync_index(Broken(), root, force=True) == "fail"
    assert indices.load_index(root) is not None


def test_a_stale_cache_is_not_presented_as_the_latest(root):
    indices.sync_index(FakeClient(end=date.today() - timedelta(days=30)), root)
    assert indices.load_index(root) is None


def test_the_overview_prefers_the_official_index(root):
    indices.sync_index(FakeClient(), root)
    frame, provenance = MarketIntelService(data_root=root).get_benchmark_frame_and_provenance()
    assert provenance == PROVENANCE_NIFTY50_INDEX
    assert provenance.is_proxy is False
    assert frame is not None and float(frame["close"].iloc[-1]) > 1000  # an index level, not a ~267 ETF unit


def test_without_an_index_it_falls_back_to_the_etf(root):
    etf_dir = root / "iifl_daily" / "NSEEQ"
    etf_dir.mkdir(parents=True)
    days = pd.bdate_range(end=pd.Timestamp(date.today()), periods=30)
    pd.DataFrame(
        {"ts": days, "open": 267.0, "high": 268.0, "low": 266.0, "close": 267.1, "volume": 1000.0}
    ).to_parquet(etf_dir / "NIFTYBEES-EQ.parquet")

    _, provenance = MarketIntelService(data_root=root).get_benchmark_frame_and_provenance()
    assert provenance.is_proxy is True
    assert provenance.symbol == "NIFTYBEES-EQ"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _chart_payload(n: int = 30) -> dict:
    days = pd.bdate_range(end=pd.Timestamp(date.today()), periods=n)
    # 09:15 IST is 03:45 UTC; the exchange offset is +5:30.
    stamps = [int((d + pd.Timedelta(hours=3, minutes=45)).timestamp()) for d in days]
    close = [23000.0 + i for i in range(n)]
    close[5] = None  # a bar with no price must be dropped, not stored as NaN
    return {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": "^NSEI", "gmtoffset": 19800},
                    "timestamp": stamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": [c and c - 5 for c in close],
                                "high": [c and c + 20 for c in close],
                                "low": [c and c - 20 for c in close],
                                "close": close,
                                "volume": [0] * n,
                            }
                        ]
                    },
                }
            ]
        }
    }


def test_the_public_series_fills_the_same_cache_without_a_session(root, monkeypatch):
    monkeypatch.setattr(indices.httpx, "get", lambda *a, **k: _Resp(_chart_payload()))
    assert indices.sync_index_public(root) == "ok"

    frame = indices.load_index(root)
    assert frame is not None
    assert len(frame) == 29  # the price-less bar was dropped
    assert float(frame["close"].iloc[-1]) > 20000  # a real index level
    assert (frame["ts"].dt.hour == 0).all()  # plain dates, not timestamps
    assert indices.index_path(root).with_suffix(".source").read_text() == "public"


def test_a_public_fetch_failure_keeps_the_previous_file(root, monkeypatch):
    indices.sync_index(FakeClient(), root)

    def offline(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(indices.httpx, "get", offline)
    assert indices.sync_index_public(root, force=True) == "fail"
    assert indices.load_index(root) is not None


def test_no_session_uses_the_public_series_right_away(root, monkeypatch):
    from atr.services import broker_access

    def no_session():
        raise broker_access.BrokerUnavailable("no active IIFL session")

    monkeypatch.setattr(broker_access, "authed_client", no_session)
    calls = []
    monkeypatch.setattr(indices, "sync_index_public", lambda data_root, *a, **k: calls.append(data_root) or "ok")

    service = MarketIntelService(data_root=root)
    service._maybe_refresh_index()
    assert calls == [root]  # inline: the first load already gets the real index
    service._maybe_refresh_index()
    assert calls == [root]  # throttled, so a down network is not hammered


def test_a_failed_broker_download_falls_back_to_the_public_series(root, monkeypatch):
    from atr.services import broker_access

    monkeypatch.setattr(broker_access, "authed_client", lambda: object())
    monkeypatch.setattr(indices, "sync_index", lambda *a, **k: "fail")
    public = []
    monkeypatch.setattr(indices, "sync_index_public", lambda data_root, *a, **k: public.append(1) or "ok")
    # Run the background work on the calling thread so the test can observe it.
    monkeypatch.setattr("threading.Thread.start", lambda self: self.run())

    MarketIntelService(data_root=root)._maybe_refresh_index()
    assert public == [1]
