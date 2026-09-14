"""Episodic Pivot tests.

The three things that must not silently break:

1. The trigger keys off the right columns — a gap, a volume ratio, a quiet tape.
2. The protective stop is a **resting STOP order that fills at the stop price**
   (or at the open when the bar gaps through it), not a close-price rule. A
   trailing stop implemented as "sell at the close when the trail is breached"
   looks identical in a trade log and is a materially worse fill.
3. The stop can never fire *before* the entry that it protects. Submitting the
   stop on the signal bar lets the broker process it first on the following
   bar, producing a phantom short followed by a wash buy.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from atr.backtest.costs import SlippageModel
from atr.backtest.engine import BacktestConfig, BacktestEngine
from atr.core.enums import AssetClass, OrderStatus, OrderType, Side
from atr.core.models import Bar, Instrument, MarketSnapshot
from atr.data.base import ListFeed
from atr.strategy.strategies.episodic_pivot import (
    EpisodicPivot9M,
    EpisodicPivotBase,
    EpisodicPivotDay1,
    EpisodicPivotDelayed,
    _is_red_to_green,
    _prep,
)

SYMBOL = "TEST"
START = datetime(2024, 1, 1, 9, 30)


def frame(rows: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """(open, high, low, close, volume) tuples → the frame shape the engine builds."""
    return pd.DataFrame(
        {
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [r[4] for r in rows],
        },
        index=pd.DatetimeIndex([START + timedelta(days=i) for i in range(len(rows))]),
    )


def quiet_rows(n: int, price: float = 100.0, volume: float = 100_000) -> list:
    """``n`` sessions that did very little — the playbook's neglect."""
    return [(price, price + 0.5, price - 0.5, price, volume)] * n


def snapshots(rows, symbol: str = SYMBOL) -> list[MarketSnapshot]:
    out = []
    for i, (open_, high, low, close, volume) in enumerate(rows):
        ts = START + timedelta(days=i)
        out.append(
            MarketSnapshot(
                ts=ts,
                bars={
                    symbol: Bar(
                        ts=ts, open=open_, high=high, low=low, close=close,
                        volume=volume,
                    )
                },
            )
        )
    return out


def instrument(symbol: str = SYMBOL) -> Instrument:
    return Instrument(symbol=symbol, exchange="NSEEQ", asset_class=AssetClass.EQUITY)


# ---------------------------------------------------------------------------
# Indicator preparation
# ---------------------------------------------------------------------------


def test_prep_computes_gap_volume_ratio_and_neglect():
    rows = quiet_rows(80) + [(110.0, 112.0, 105.0, 108.0, 1_000_000.0)]
    prepared = _prep(frame(rows))
    last = prepared.iloc[-1]

    assert last["ep_gap_pct"] == pytest.approx(10.0, abs=1e-6)
    assert last["ep_vol_ratio"] == pytest.approx(10.0, rel=1e-6)
    # 80 flat closes then one 8% move: the 120-session vol is dominated by it,
    # but with min_periods=60 it is defined well before the gap.
    assert last["ep_vol_120"] == last["ep_vol_120"]
    assert last["ep_signal_low"] == pytest.approx(105.0)


def test_volume_average_excludes_the_day_being_tested():
    """A 10x volume spike must not raise the baseline it is measured against."""
    rows = quiet_rows(80) + [(100.0, 101.0, 99.0, 100.0, 10_000_000.0)]
    prepared = _prep(frame(rows))
    assert prepared.iloc[-1]["ep_vol_ratio"] == pytest.approx(100.0, rel=1e-6)


def test_zero_volume_is_treated_as_missing_not_as_a_trade():
    """The feed writes volume 0 for 'no bar today'. Averaging those in drags the
    baseline down and manufactures a volume ratio out of nothing."""
    rows = quiet_rows(80) + [(100.0, 100.0, 100.0, 100.0, 0.0)] * 5
    rows += [(100.0, 100.0, 100.0, 100.0, 100_000.0)]
    prepared = _prep(frame(rows))
    assert prepared.iloc[-1]["ep_vol_ratio"] == pytest.approx(1.0, rel=1e-6)


def test_red_to_green_detection():
    prepared = _prep(
        frame([(100.0, 100.0, 100.0, 100.0, 1e5)] * 2 + [(98.0, 103.0, 97.0, 101.0, 1e5)])
    )
    assert _is_red_to_green(prepared.iloc[-1])
    prepared = _prep(
        frame([(100.0, 100.0, 100.0, 100.0, 1e5)] * 2 + [(102.0, 103.0, 97.0, 101.0, 1e5)])
    )
    assert not _is_red_to_green(prepared.iloc[-1])


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


class _Recorder(EpisodicPivotBase):
    """Captures every trigger decision without trading."""

    name = "recorder"

    def __init__(self, **params):
        super().__init__(**params)
        self.fired: list[int] = []

    def _trigger(self, row, signal_low):
        return super()._trigger(row, signal_low)

    def on_bar(self, ctx):
        self._index = ctx.index
        super().on_bar(ctx)


def _triggers(cls, rows, **params) -> list[int]:
    prepared = _prep(frame(rows))
    strategy = cls(**params)
    out = []
    for i in range(len(prepared)):
        if strategy._trigger(prepared.iloc[i], float(prepared.iloc[i]["ep_signal_low"])):
            out.append(i)
    return out


def test_day1_fires_on_a_gap_and_stays_quiet_without_one():
    base = quiet_rows(80)
    assert _triggers(EpisodicPivotDay1, base + [(110.0, 112.0, 105.0, 108.0, 1e6)]) == [80]
    assert _triggers(EpisodicPivotDay1, base + [(101.0, 102.0, 100.0, 101.0, 1e6)]) == []


def test_day1_neglect_filter_blocks_a_noisy_tape():
    """A tape swinging 5% a session is the opposite of 'ignored for months'.

    The noise is intraday only — every session opens exactly at the prior
    close — so there is no gap for the trigger to key off and the neglect gate
    is the only thing under test. Building the noise with gaps would make the
    unfiltered run fire on the noise itself, which tests nothing.
    """
    noisy = []
    prev = 100.0
    for i in range(80):
        price = prev * (1.05 if i % 2 else 0.95)
        noisy.append(
            (prev, max(prev, price) * 1.01, min(prev, price) * 0.99, price, 100_000.0)
        )
        prev = price
    rows = noisy + [(prev * 1.10, prev * 1.12, prev * 1.05, prev * 1.08, 1e6)]
    assert _triggers(EpisodicPivotDay1, rows, neglect_max_vol=99.0) == [80]
    assert _triggers(EpisodicPivotDay1, rows, neglect_max_vol=2.0) == []


def test_9m_needs_volume_and_a_turnover_floor():
    rows = quiet_rows(80) + [(100.0, 101.0, 99.0, 100.0, 1_000_000.0)]
    # 1e6 shares x 100 = 1e8 rupees = 10 crore, below a 50-crore floor.
    assert _triggers(EpisodicPivot9M, rows, min_turnover_cr=50.0) == []
    assert _triggers(EpisodicPivot9M, rows, min_turnover_cr=0.0) == [80]


def test_9m_is_pure_volume_by_default_but_can_require_a_gap():
    rows = quiet_rows(80) + [(100.0, 101.0, 99.0, 100.0, 1_000_000.0)]
    assert _triggers(EpisodicPivot9M, rows, min_turnover_cr=0.0) == [80]
    assert _triggers(EpisodicPivot9M, rows, min_turnover_cr=0.0, require_gap=True) == []


# ---------------------------------------------------------------------------
# End-to-end: entry, resting stop, and its fill
# ---------------------------------------------------------------------------


def _run(rows, strategy, **bt_kwargs):
    # Zero slippage: these tests assert exact fill prices, and a 5bps haircut
    # turns a correct fill into a near-miss that reads like an off-by-one.
    bt_kwargs.setdefault("slippage", SlippageModel(bps=0.0))
    feed = ListFeed(snapshots(rows), {SYMBOL: instrument()})
    config = BacktestConfig(initial_cash=1_000_000.0, **bt_kwargs)
    return BacktestEngine(feed, strategy, config).run()


def test_entry_fills_next_open_and_stop_is_a_resting_order():
    rows = quiet_rows(80) + [
        (110.0, 112.0, 105.0, 108.0, 1_000_000.0),   # 80 signal: gap 10%, vol 10x
        (109.0, 110.0, 106.0, 109.0, 200_000.0),     # 81 entry fills at 109
        (104.0, 105.0, 100.0, 101.0, 200_000.0),     # 82 gaps below the 105 stop
    ]
    result = _run(rows, EpisodicPivotDay1(gap_min=5.0, max_weight=1.0))

    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    assert trade["entry_price"] == pytest.approx(109.0)
    # The bar opened at 104, below the 105 stop, so the fill is the *open* —
    # not the stop price. Filling at 105 here would be a fantasy.
    assert trade["exit_price"] == pytest.approx(104.0)

    stops = result.orders[result.orders["type"] == OrderType.STOP.value]
    assert len(stops) == 1
    assert stops.iloc[0]["status"] == OrderStatus.FILLED.value


def test_stop_never_fires_before_the_entry_it_protects():
    """A stop submitted on the signal bar would fill first on bar 81 and leave
    a phantom short. It must be armed only once the position exists."""
    rows = quiet_rows(80) + [
        (110.0, 112.0, 105.0, 108.0, 1_000_000.0),   # 80 signal
        (109.0, 110.0, 104.0, 109.0, 200_000.0),     # 81 entry, dips to 104 < 105
        (110.0, 112.0, 108.0, 111.0, 200_000.0),     # 82 recovers
    ]
    result = _run(rows, EpisodicPivotDay1(gap_min=5.0, max_weight=1.0))

    fills = result.fills
    buys = fills[fills["side"] == Side.BUY.name]
    sells = fills[fills["side"] == Side.SELL.name]
    assert len(buys) == 1
    # The entry bar traded through the stop level (low 104 < 105) before the
    # stop was in the book, so the strategy exits at the next open instead of
    # pretending the stop caught it.
    assert len(sells) == 1
    assert sells.iloc[0]["price"] == pytest.approx(110.0)


def test_trailing_stop_repriced_upward_and_never_down():
    rows = quiet_rows(80) + [
        (110.0, 112.0, 105.0, 108.0, 1_000_000.0),   # 80 signal, stop 105
        (109.0, 110.0, 106.0, 109.0, 2e5),           # 81 entry, arms stop at 105
        (110.0, 120.0, 109.0, 119.0, 2e5),           # 82 rally -> break-even at 109
        (119.0, 125.0, 118.0, 124.0, 2e5),           # 83 rally
        (124.0, 126.0, 100.0, 101.0, 2e5),           # 84 collapses through the trail
    ]
    result = _run(
        rows, EpisodicPivotDay1(gap_min=5.0, trail_bars=2, max_weight=1.0)
    )

    stops = result.orders[result.orders["type"] == OrderType.STOP.value]
    # Two protective orders: the initial 105 and the raised one. A trail that
    # never rises is a fixed stop wearing the word "trail".
    assert len(stops) == 2
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    assert trade["exit_price"] > 105.0
    # Bar 84 opened at 124, well above the raised stop, so the fill is the stop
    # price itself rather than the open.
    assert trade["exit_price"] == pytest.approx(109.0)


def test_time_stop_closes_the_position():
    rows = quiet_rows(80) + [(110.0, 112.0, 105.0, 108.0, 1_000_000.0)]
    rows += [(108.0, 109.0, 107.0, 108.0, 1e5)] * 6
    result = _run(
        rows, EpisodicPivotDay1(gap_min=5.0, max_hold_bars=2, max_weight=1.0)
    )
    assert len(result.trades) == 1


# ---------------------------------------------------------------------------
# Portfolio budget
# ---------------------------------------------------------------------------


def _multi_snapshot(symbols: list[str], rows) -> list[MarketSnapshot]:
    """Every symbol on every bar — the shape the real feed produces."""
    out = []
    for i, (open_, high, low, close, volume) in enumerate(rows):
        ts = START + timedelta(days=i)
        out.append(
            MarketSnapshot(
                ts=ts,
                bars={
                    s: Bar(ts=ts, open=open_, high=high, low=low, close=close,
                           volume=volume)
                    for s in symbols
                },
            )
        )
    return out


def test_gross_exposure_is_capped_by_the_strategy_not_the_broker():
    """The broker funds each order against total equity *in isolation*.

    So N concurrent positions are each individually affordable and the book
    reaches N x equity. On the real Nifty-50 universe this strategy reached
    **3.49x** before the cap existed. The leverage is invisible in the trade
    log; the only symptom was the risk engine reporting a "daily loss" of 750k
    on a 1M account, which is what exposed it.
    """
    symbols = [f"S{i}" for i in range(12)]
    rows = quiet_rows(80) + [(110.0, 112.0, 105.0, 108.0, 1_000_000.0)]
    rows += [(108.0, 109.0, 107.0, 108.0, 200_000.0)] * 3
    snaps = _multi_snapshot(symbols, rows)

    instruments = {s: instrument(s) for s in symbols}
    config = BacktestConfig(initial_cash=1_000_000.0, slippage=SlippageModel(bps=0.0))
    result = BacktestEngine(
        ListFeed(snaps, instruments),
        EpisodicPivotDay1(gap_min=5.0),      # defaults: 25% per name, 1.0x gross
        config,
    ).run()

    assert result.exposure.max() <= 1.05, "book was levered past its cap"
    buys = result.fills[result.fills["side"] == Side.BUY.name]
    # 25% each => four fit inside a 100% budget; the rest must be declined.
    assert 0 < len(buys) <= 5


def test_max_positions_is_enforced():
    symbols = [f"S{i}" for i in range(12)]
    rows = quiet_rows(80) + [(110.0, 112.0, 105.0, 108.0, 1_000_000.0)]
    rows += [(108.0, 109.0, 107.0, 108.0, 200_000.0)] * 3
    snaps = _multi_snapshot(symbols, rows)

    instruments = {s: instrument(s) for s in symbols}
    config = BacktestConfig(initial_cash=1_000_000.0, slippage=SlippageModel(bps=0.0))
    result = BacktestEngine(
        ListFeed(snaps, instruments),
        EpisodicPivotDay1(gap_min=5.0, max_positions=2, max_weight=0.10),
        config,
    ).run()

    buys = result.fills[result.fills["side"] == Side.BUY.name]
    assert len(buys) <= 2


def test_finite_prefers_a_real_number_over_a_nan():
    """`NaN or fallback` returns NaN — NaN is truthy in Python.

    It reached the sizer as `int(nan)` and read like a sizing bug rather than a
    missing price, which is why it is worth pinning down.
    """
    from atr.strategy.strategies.episodic_pivot import _finite

    assert _finite(float("nan"), 12.5) == 12.5
    assert _finite(None, 12.5) == 12.5
    assert _finite(0.0, 12.5) == 12.5
    assert _finite(float("inf"), 12.5) == 12.5
    assert _finite(float("nan"), None) == 0.0
    assert _finite(3.0, 12.5) == 3.0


def test_a_symbol_with_a_nan_price_does_not_crash_the_run():
    """The mid-cap fold died with `cannot convert float NaN to integer`.

    A NaN price in one symbol poisoned the shared portfolio budget, so the
    failure surfaced in an unrelated symbol's sizing.
    """
    symbols = ["GOOD", "NANPRICE"]
    rows = quiet_rows(80) + [(110.0, 112.0, 105.0, 108.0, 1_000_000.0)]
    rows += [(108.0, 109.0, 107.0, 108.0, 200_000.0)] * 3
    snaps = _multi_snapshot(symbols, rows)

    # Blow away NANPRICE's prices from bar 81 on, as a sparse feed would.
    for snap in snaps[81:]:
        bar = snap.bars["NANPRICE"]
        snap.bars["NANPRICE"] = Bar(
            ts=bar.ts, open=float("nan"), high=float("nan"), low=float("nan"),
            close=float("nan"), volume=0.0,
        )

    instruments = {s: instrument(s) for s in symbols}
    config = BacktestConfig(initial_cash=1_000_000.0, slippage=SlippageModel(bps=0.0))
    result = BacktestEngine(
        ListFeed(snaps, instruments), EpisodicPivotDay1(gap_min=5.0), config
    ).run()
    assert result.exposure.max() <= 1.05


# ---------------------------------------------------------------------------
# Delayed reaction
# ---------------------------------------------------------------------------


def test_delayed_variant_waits_for_a_red_to_green_day():
    rows = quiet_rows(80) + [
        (110.0, 112.0, 105.0, 107.0, 1_000_000.0),   # 80 catalyst, closes red
        (107.0, 108.0, 105.0, 106.0, 2e5),           # 81 still weak
        (104.0, 109.0, 103.0, 108.0, 3e5),           # 82 red-to-green
        (108.0, 110.0, 107.0, 109.0, 2e5),           # 83 entry fills at 108
        (100.0, 101.0, 98.0, 99.0, 2e5),             # 84 through the 103 stop
    ]
    result = _run(
        rows,
        EpisodicPivotDelayed(gap_min=5.0, vol_mult=3.0, max_weight=1.0),
    )
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    # Filled at the open of the bar after the red-to-green day.
    assert trade["entry_price"] == pytest.approx(108.0)
    # Stop sits below the *signal* bar's low (103), not the catalyst bar's.
    assert trade["exit_price"] == pytest.approx(100.0)


def test_delayed_variant_does_not_fire_on_the_catalyst_bar_itself():
    rows = quiet_rows(80) + [
        (110.0, 112.0, 105.0, 107.0, 1_000_000.0),   # 80 catalyst
        (107.0, 108.0, 105.0, 106.0, 2e5),
    ]
    result = _run(rows, EpisodicPivotDelayed(gap_min=5.0, vol_mult=3.0, max_weight=1.0))
    assert len(result.trades) == 0
