"""Live runner tests.

The runner is driven by a fake feed and a recording broker, so the loop is
exercised without the live bridge or a real account. The regression these
guard matters: the runner used to call ``strategy.prepare()`` exactly once,
against empty frames, leaving every indicator NaN. A live strategy therefore
never placed a single order — and failed silently rather than loudly.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from atr.brokers.base import Broker
from atr.core.models import Bar, Instrument, MarketSnapshot
from atr.live.runner import LiveConfig, LiveRunner
from atr.strategy.strategies.sma_crossover import SmaCrossover

START = datetime(2024, 1, 1, 9, 15)


def _series(n: int, offset: float = 0.0) -> np.ndarray:
    """A sine wave, so fast/slow crossovers definitely occur."""
    return 100.0 + 10.0 * np.sin(np.arange(n) / 5.0) + offset


def _frames(symbols, n: int) -> dict[str, pd.DataFrame]:
    index = pd.date_range(START, periods=n, freq="1min")
    out = {}
    for i, symbol in enumerate(symbols):
        close = _series(n, offset=i)
        out[symbol] = pd.DataFrame(
            {
                "open": close,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": np.full(n, 1000.0),
            },
            index=index,
        )
    return out


def _snapshots(symbols, start_offset: int, n: int, skip: dict[int, set[str]] | None = None):
    """Continue the same series so crossovers keep firing."""
    skip = skip or {}
    index = pd.date_range(START, periods=start_offset + n, freq="1min")[start_offset:]
    out = []
    for step, ts in enumerate(index):
        bars = {}
        for i, symbol in enumerate(symbols):
            if symbol in skip.get(step, set()):
                continue
            value = float(_series(start_offset + step + 1, offset=i)[-1])
            bars[symbol] = Bar(
                ts=ts, open=value, high=value + 0.5, low=value - 0.5, close=value, volume=1000.0
            )
        out.append(MarketSnapshot(ts=ts, bars=bars))
    return out


def _instruments(symbols) -> dict[str, Instrument]:
    return {s: Instrument(symbol=s, exchange="NSEEQ") for s in symbols}


class FakeFeed:
    def __init__(self, snapshots, on_exhausted=None):
        self._snapshots = list(snapshots)
        self._on_exhausted = on_exhausted
        self.started = False
        self.stopped = False

    def start(self, *args, **kwargs) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def get(self, timeout: float = 1.0):
        if self._snapshots:
            return self._snapshots.pop(0)
        if self._on_exhausted:
            self._on_exhausted()
        return None


class FakeClient:
    """Returns a canned historical-data payload, in the API's own shape."""

    def __init__(self, candles):
        self._candles = candles
        self.calls: list[dict] = []

    def historical_data(self, **kwargs):
        self.calls.append(kwargs)
        return {"result": [{"candles": self._candles}]}


class RecordingBroker(Broker):
    """Records orders instead of sending them anywhere."""

    def __init__(self) -> None:
        self.orders = []

    def place_order(self, order):
        self.orders.append(order)
        order.broker_order_id = f"SIM{len(self.orders)}"
        return order

    def modify_order(self, broker_order_id, **fields):  # pragma: no cover
        raise NotImplementedError

    def cancel_order(self, broker_order_id) -> bool:  # pragma: no cover
        return True

    def positions(self):  # pragma: no cover
        return []

    def open_orders(self):  # pragma: no cover
        return []

    def last_price(self, instruments):  # pragma: no cover
        return {}


def _runner(symbols, warmup_bars, snapshots, *, config=None, frames=None, client=None):
    runner = LiveRunner(
        session=None,
        instruments=_instruments(symbols),
        strategy=SmaCrossover(fast=3, slow=8),
        broker=RecordingBroker(),
        warmup_frames=frames if frames is not None else _frames(symbols, warmup_bars),
        config=config or LiveConfig(warmup_bars=0, poll_timeout=0.01),
        client=client,
        feed=FakeFeed(snapshots),
    )
    runner.feed._on_exhausted = lambda: setattr(runner, "_stop", True)
    return runner


# --------------------------------------------------------------------------
def test_runner_submits_orders_on_live_bars():
    """The regression: a live strategy must actually be able to trade."""
    symbols = ["AAA"]
    runner = _runner(symbols, 60, _snapshots(symbols, 60, 40))
    runner.run()
    assert runner.feed.started and runner.feed.stopped
    assert len(runner.broker.orders) > 0, "no orders placed — indicators are probably NaN"


def test_indicators_are_populated_on_the_latest_bar():
    """prepare() must be re-run as bars arrive, not once at startup."""
    symbols = ["AAA"]
    runner = _runner(symbols, 60, _snapshots(symbols, 60, 40))
    runner.run()
    frame = runner.frames["AAA"]
    assert frame["sma_fast"].notna().any()
    # The newest row is the one the strategy reads — it must carry a value.
    assert not pd.isna(frame["sma_fast"].iloc[-1])
    assert not pd.isna(frame["sma_slow"].iloc[-1])


def test_frames_stay_aligned_when_a_symbol_is_missing_from_a_snapshot():
    """Ragged frames would make a single ctx.index address the wrong bar."""
    symbols = ["AAA", "BBB"]
    snaps = _snapshots(symbols, 60, 30, skip={3: {"BBB"}, 7: {"BBB"}, 11: {"AAA"}})
    runner = _runner(symbols, 60, snaps)
    runner.run()
    lengths = {s: len(f) for s, f in runner.frames.items()}
    assert len(set(lengths.values())) == 1, f"frames drifted apart: {lengths}"


def test_missing_symbol_rows_are_nan_not_dropped():
    symbols = ["AAA", "BBB"]
    snaps = _snapshots(symbols, 60, 5, skip={0: {"BBB"}})
    runner = _runner(symbols, 60, snaps)
    runner.run()
    assert len(runner.frames["BBB"]) == len(runner.frames["AAA"])
    assert pd.isna(runner.frames["BBB"]["close"].iloc[-5])


def test_history_is_bounded_by_max_history_bars():
    symbols = ["AAA"]
    config = LiveConfig(warmup_bars=0, max_history_bars=50, poll_timeout=0.01)
    runner = _runner(symbols, 60, _snapshots(symbols, 60, 40), config=config)
    runner.run()
    assert len(runner.frames["AAA"]) <= 50


def test_warmup_is_skipped_when_frames_are_already_supplied():
    """Pre-loaded warmup frames must not trigger a network fetch."""
    symbols = ["AAA"]
    runner = _runner(symbols, 60, _snapshots(symbols, 60, 5))
    runner.load_warmup()  # must be a no-op, not an exception
    assert len(runner.frames["AAA"]) == 60


def test_warmup_fetches_and_honours_warmup_bars():
    """warmup_bars was declared but never used; it must now cap the fetch."""
    symbols = ["AAA"]
    n = 300
    base = pd.Timestamp("2024-01-01 09:15")
    candles = [
        [
            (base + pd.Timedelta(minutes=i)).isoformat(),
            100.0 + i * 0.01,          # open
            100.5 + i * 0.01,          # high
            99.5 + i * 0.01,           # low
            100.0 + i * 0.01,          # close
            500,
        ]
        for i in range(n)
    ]
    client = FakeClient(candles)
    runner = LiveRunner(
        session=None,
        instruments=_instruments(symbols),
        strategy=SmaCrossover(fast=3, slow=8),
        broker=RecordingBroker(),
        warmup_frames=None,
        config=LiveConfig(warmup_bars=100),
        client=client,
        feed=FakeFeed([]),
    )
    runner.load_warmup()
    assert client.calls, "warmup never called the client"
    assert len(runner.frames["AAA"]) == 100, "warmup_bars did not cap the history"
    assert runner.frames["AAA"]["close"].iloc[-1] == pytest.approx(100.0 + 299 * 0.01)


def test_warmup_disabled_when_zero():
    symbols = ["AAA"]
    runner = LiveRunner(
        session=None,
        instruments=_instruments(symbols),
        strategy=SmaCrossover(),
        broker=RecordingBroker(),
        warmup_frames=None,
        config=LiveConfig(warmup_bars=0),
        client=FakeClient([]),
        feed=FakeFeed([]),
    )
    runner._ensure_frames()
    runner.load_warmup()
    assert all(f.empty for f in runner.frames.values())


def test_warmup_survives_a_failing_client():
    """A dead warmup endpoint must degrade to a cold start, not crash the run."""

    class ExplodingClient:
        def historical_data(self, **kwargs):
            raise RuntimeError("upstream is down")

    runner = LiveRunner(
        session=None,
        instruments=_instruments(["AAA"]),
        strategy=SmaCrossover(),
        broker=RecordingBroker(),
        warmup_frames=None,
        config=LiveConfig(warmup_bars=100),
        client=ExplodingClient(),
        feed=FakeFeed([]),
    )
    runner.load_warmup()  # must not raise
    runner._ensure_frames()
    assert all(f.empty for f in runner.frames.values())


@pytest.mark.parametrize("symbols", [["AAA"], ["AAA", "BBB", "CCC"]])
def test_runner_handles_one_and_many_symbols(symbols):
    runner = _runner(symbols, 60, _snapshots(symbols, 60, 20))
    runner.run()
    for symbol in symbols:
        assert not runner.frames[symbol].empty
