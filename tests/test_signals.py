"""Signal-rule tests.

The rules are pure functions over a daily frame, so these run without a broker,
a network, or a clock. That matters: a signal you cannot test is a hunch with
extra steps.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from atr.signals.engine import format_report
from atr.signals.models import EntryRules, ExitRules, ScanResult, Signal
from atr.signals.rules import append_live_bar, eval_entry, eval_exit, primary_exit


def frame(closes, volumes=None, spread: float = 0.005) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * (1 + spread),
            "low": closes * (1 - spread),
            "close": closes,
            "volume": np.asarray(volumes if volumes is not None else np.full(len(closes), 1000.0),
                                 dtype=float),
        }
    )


def _rules(name: str):
    return {"stop_loss": name} and ExitRules(stop_loss_pct=15.0, take_profit_pct=None,
                                             trailing_stop_pct=None, trend_sma=0,
                                             rsi_overbought=None)


# --------------------------------------------------------------------------
# exits
# --------------------------------------------------------------------------
def test_stop_loss_fires_when_the_loss_reaches_the_threshold():
    f = frame(np.linspace(100, 80, 60))
    signals = eval_exit("TEST", f, avg_price=100.0, rules=_rules("stop_loss"))
    assert [s.rule for s in signals] == ["stop_loss"]
    assert signals[0].action == "SELL"
    assert signals[0].detail["pnl_pct"] == pytest.approx(-20.0, abs=0.01)


def test_stop_loss_does_not_fire_inside_the_threshold():
    f = frame(np.linspace(100, 95, 60))
    assert eval_exit("TEST", f, avg_price=100.0, rules=_rules("stop_loss")) == []


def test_take_profit_fires_only_when_enabled():
    f = frame(np.linspace(100, 140, 60))
    rules = ExitRules(stop_loss_pct=None, take_profit_pct=30.0, trailing_stop_pct=None,
                      trend_sma=0, rsi_overbought=None)
    signals = eval_exit("TEST", f, avg_price=100.0, rules=rules)
    assert [s.rule for s in signals] == ["take_profit"]
    assert signals[0].detail["pnl_pct"] == pytest.approx(40.0, abs=0.01)


def test_trailing_stop_measures_from_the_recent_high():
    # Ran up to 200, now back at 150 -> 25% off the peak.
    closes = np.concatenate([np.linspace(100, 200, 60), np.linspace(199, 150, 10)])
    rules = ExitRules(stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=20.0,
                      trend_sma=0, rsi_overbought=None)
    signals = eval_exit("TEST", frame(closes), avg_price=100.0, rules=rules)
    assert [s.rule for s in signals] == ["trailing_stop"]
    assert signals[0].detail["off_peak_pct"] == pytest.approx(-25.0, abs=1.0)


def test_trend_break_fires_below_the_moving_average():
    closes = np.concatenate([np.linspace(100, 160, 120), np.linspace(159, 120, 20)])
    rules = ExitRules(stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=None,
                      trend_sma=50, rsi_overbought=None)
    signals = eval_exit("TEST", frame(closes), avg_price=100.0, rules=rules)
    assert "trend_break" in [s.rule for s in signals]


def test_rsi_overbought_fires_on_a_relentless_rise():
    rules = ExitRules(stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=None,
                      trend_sma=0, rsi_overbought=80.0)
    signals = eval_exit("TEST", frame(np.linspace(100, 200, 60)), avg_price=100.0, rules=rules)
    assert "rsi_overbought" in [s.rule for s in signals]


def test_exits_are_skipped_without_usable_inputs():
    rules = ExitRules()
    assert eval_exit("TEST", pd.DataFrame(), 100.0, rules) == []
    assert eval_exit("TEST", frame([100.0] * 30), 0.0, rules) == []
    assert eval_exit("TEST", frame([100.0] * 30), float("nan"), rules) == []


def test_primary_exit_prefers_stop_loss_over_take_profit():
    a = Signal("T", "SELL", "take_profit", "", 1.0)
    b = Signal("T", "SELL", "stop_loss", "", 1.0)
    assert primary_exit([a, b]).rule == "stop_loss"
    assert primary_exit([a]).rule == "take_profit"
    assert primary_exit([]) is None


def test_quantity_and_value_are_reported():
    signals = eval_exit("TEST", frame(np.linspace(100, 80, 40)), 100.0,
                        _rules("stop_loss"), quantity=50)
    assert signals[0].detail["quantity"] == 50
    assert signals[0].detail["value"] == pytest.approx(50 * 80.0)


# --------------------------------------------------------------------------
# entries
# --------------------------------------------------------------------------
def _entry_rules(**kw) -> EntryRules:
    base = dict(
        trend_fast_sma=20, trend_slow_sma=50, pullback_rsi_low=35.0, pullback_rsi_high=65.0,
        breakout_lookback=63, breakout_proximity_pct=2.0, volume_multiple=1.5,
        volume_lookback=20, oversold_rsi=30.0, long_sma=200, min_history_bars=210,
    )
    base.update(kw)
    return EntryRules(**base)


def test_breakout_fires_on_a_new_high_with_volume():
    closes = np.concatenate([np.linspace(100, 120, 100), [121.5]])
    volumes = np.full(len(closes), 1000.0)
    volumes[-1] = 4000.0
    signals = eval_entry("TEST", frame(closes, volumes), _entry_rules())
    assert "breakout" in [s.rule for s in signals]


def test_breakout_does_not_fire_without_volume():
    closes = np.concatenate([np.linspace(100, 120, 100), [121.5]])
    volumes = np.full(len(closes), 1000.0)
    signals = eval_entry("TEST", frame(closes, volumes), _entry_rules())
    assert "breakout" not in [s.rule for s in signals]


def test_trend_pullback_fires_in_an_uptrend_that_stalls():
    up = np.linspace(100, 150, 200)
    wiggle = 150 + np.sin(np.arange(40)) * 1.5
    signals = eval_entry("TEST", frame(np.concatenate([up, wiggle])), _entry_rules())
    assert "trend_pullback" in [s.rule for s in signals]


def test_entries_are_marked_unvalidated():
    closes = np.concatenate([np.linspace(100, 120, 100), [121.5]])
    volumes = np.full(len(closes), 1000.0)
    volumes[-1] = 4000.0
    signals = eval_entry("TEST", frame(closes, volumes), _entry_rules())
    assert signals and all(not s.validated for s in signals)


def test_entry_rules_need_history():
    assert eval_entry("TEST", frame([100.0] * 10), _entry_rules()) == []
    assert eval_entry("TEST", pd.DataFrame(), _entry_rules()) == []


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------
def test_append_live_bar_adds_the_forming_candle():
    f = frame([100.0, 101.0, 102.0])
    out = append_live_bar(f, 105.0)
    assert len(out) == len(f) + 1
    assert float(out["close"].iloc[-1]) == 105.0
    assert float(out["high"].iloc[-1]) == 105.0


def test_append_live_bar_ignores_a_bad_price():
    f = frame([100.0, 101.0])
    assert len(append_live_bar(f, float("nan"))) == 2
    assert len(append_live_bar(f, 0.0)) == 2


def test_report_flags_unvalidated_entry_rules():
    result = ScanResult(
        buys=[Signal("AAA", "BUY", "breakout", "new high", 100.0)],
        sells=[Signal("BBB", "SELL", "stop_loss", "down 20%", 80.0, {"pnl_pct": -20.0})],
    )
    title, body = format_report(result)
    assert "SELL (1)" in body and "BUY (1)" in body
    assert "AAA" in body and "BBB" in body
    assert "NOT passed out-of-sample validation" in body


def test_report_with_no_signals():
    title, body = format_report(ScanResult())
    assert body == "No signals."


def test_row_access_survives_hostile_column_names():
    """Row lookup must cope with columns namedtuple cannot use directly.

    namedtuple rejects field names starting with an underscore, so a frame
    carrying a precomputed ``_mom`` column would raise on every lookup. Keyword
    names and names shadowing tuple methods are also invalid or destructive.
    """
    from atr.strategy.base import StrategyContext

    frame = pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [1.0, 2.0],
            "_mom126_skip21": [0.5, 0.75],
            "class": [7.0, 8.0],
            "count": [9.0, 10.0],
        }
    )
    ctx = StrategyContext(
        instruments={}, frames={"X": frame}, submit=lambda o: o, portfolio_getter=lambda: None
    )
    ctx.index = 1
    row = ctx.row("X")
    assert float(row["_mom126_skip21"]) == 0.75
    assert float(row["class"]) == 8.0
    assert float(row["count"]) == 10.0
    assert float(row["close"]) == 2.0


def test_cross_sectional_momentum_runs_in_the_backtest_engine():
    from datetime import datetime

    from atr.backtest.engine import BacktestConfig, BacktestEngine
    from atr.data.synthetic import SyntheticConfig, SyntheticFeed
    from atr.signals.cross_sectional import CrossSectionalMomentum

    feed = SyntheticFeed(
        SyntheticConfig(symbols=("AAPL", "MSFT", "GOOG"),
                        start=datetime(2024, 1, 1, 9, 30), end=datetime(2024, 4, 1, 15, 59))
    )
    result = BacktestEngine(
        feed,
        CrossSectionalMomentum(lookback=100, skip=20, top_n=2, rebalance_days=50),
        BacktestConfig(initial_cash=1_000_000.0),
    ).run()
    assert len(result.equity) > 100
    assert not result.equity.isna().any()
    # A ranking strategy should actually put capital to work.
    assert result.metrics.num_trades > 0


def test_signal_config_round_trips(tmp_path):
    from atr.signals.models import SignalConfig

    cfg = SignalConfig()
    cfg.exits.stop_loss_pct = 12.5
    cfg.universe = ["AAA", "BBB"]
    path = tmp_path / "config.json"
    cfg.save(path)
    loaded = SignalConfig.load(path)
    assert loaded.exits.stop_loss_pct == 12.5
    assert loaded.universe == ["AAA", "BBB"]


def test_missing_config_falls_back_to_defaults(tmp_path):
    from atr.signals.models import SignalConfig

    assert SignalConfig.load(tmp_path / "nope.json").exits.stop_loss_pct == 15.0


# --------------------------------------------------------------------------
def test_entry_strategy_runs_in_the_backtest_engine():
    """The strategy must execute end to end — it is what gets validated."""
    from datetime import datetime

    from atr.backtest.engine import BacktestConfig, BacktestEngine
    from atr.data.synthetic import SyntheticConfig, SyntheticFeed
    from atr.signals.strategy import SignalEntryStrategy

    feed = SyntheticFeed(
        SyntheticConfig(symbols=("AAPL",), start=datetime(2024, 1, 1, 9, 30),
                        end=datetime(2024, 3, 1, 15, 59))
    )
    result = BacktestEngine(
        feed, SignalEntryStrategy(min_history_bars=50, lookback=60, allocation=0.2),
        BacktestConfig(initial_cash=1_000_000.0),
    ).run()
    assert len(result.equity) > 100
    assert not result.equity.isna().any()


# --------------------------------------------------------------------------
# triple RSI
# --------------------------------------------------------------------------
def _uptrend_then(tail):
    up = np.linspace(100, 200, 260) + np.sin(np.arange(260)) * 1.5
    return frame(np.concatenate([up, tail]))


def _names(signals):
    return [s.rule for s in signals]


def test_triple_rsi_fires_on_a_third_falling_day_below_30_in_an_uptrend():
    rules = _entry_rules(triple_rsi_trend_sma=200)
    assert "triple_rsi" in _names(eval_entry("X", _uptrend_then([198, 195, 191, 186, 180]), rules))


def test_triple_rsi_needs_only_two_falling_days_to_be_too_early():
    rules = _entry_rules(triple_rsi_trend_sma=200)
    assert "triple_rsi" not in _names(eval_entry("X", _uptrend_then([198, 195]), rules))


def test_triple_rsi_stays_out_of_a_downtrend():
    down = frame(np.concatenate([np.linspace(200, 100, 260), [98, 95, 91, 86, 80]]))
    assert "triple_rsi" not in _names(eval_entry("X", down, _entry_rules(triple_rsi_trend_sma=200)))


def test_setup_limits_a_strategy_to_its_own_rule():
    rules = _entry_rules(triple_rsi_trend_sma=200, setup="triple_rsi")
    assert _names(eval_entry("X", _uptrend_then([198, 195, 191, 186, 180]), rules)) == ["triple_rsi"]


def test_the_rsi_exit_reads_the_period_it_is_given():
    exits = ExitRules(stop_loss_pct=50.0, take_profit_pct=None, trailing_stop_pct=None,
                      trend_sma=0, rsi_overbought=50.0, rsi_period=5)
    bounce = frame(np.concatenate([np.linspace(100, 60, 60), [70, 80]]))
    fired = eval_exit("X", bounce, avg_price=60.0, rules=exits, quantity=1)
    assert "rsi_overbought" in _names(fired)
