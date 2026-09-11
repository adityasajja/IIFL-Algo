"""Validation-harness tests.

The point of these is to pin down the *statistics*: that significance rises
with sample size, that trying more parameters raises the bar, and that
walk-forward never lets a test window leak into parameter selection.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from atr.backtest.engine import BacktestConfig
from atr.data.synthetic import SyntheticConfig, SyntheticFeed
from atr.research.validate import (
    ValidationConfig,
    WalkForwardConfig,
    buy_and_hold_equity,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    per_bar_sharpe,
    probabilistic_sharpe_ratio,
    walk_forward,
)
from atr.strategy.strategies.sma_crossover import SmaCrossover


def _feed(days: int = 7, symbols=("AAPL",)):
    return SyntheticFeed(
        SyntheticConfig(
            symbols=symbols,
            start=datetime(2024, 1, 1, 9, 30),
            end=datetime(2024, 1, 1 + days, 15, 59),
        )
    )


# --------------------------------------------------------------------------
def test_zero_sharpe_is_a_coin_flip():
    """PSR of an estimated zero Sharpe is 0.5 — no evidence either way."""
    assert probabilistic_sharpe_ratio(0.0, n_obs=1000) == pytest.approx(0.5, abs=1e-9)


def test_significance_rises_with_sample_size():
    small = probabilistic_sharpe_ratio(0.05, n_obs=100)
    large = probabilistic_sharpe_ratio(0.05, n_obs=50_000)
    assert 0.5 < small < large <= 1.0


def test_fat_tails_reduce_confidence():
    """Negative skew / fat tails make the same Sharpe less believable."""
    thin = probabilistic_sharpe_ratio(0.05, n_obs=2000, skew=0.0, kurtosis=3.0)
    fat = probabilistic_sharpe_ratio(0.05, n_obs=2000, skew=-1.0, kurtosis=8.0)
    assert fat < thin


def test_more_trials_raise_the_hurdle():
    rng = np.random.default_rng(0)
    values = rng.normal(0.0, 0.01, 200)
    assert expected_max_sharpe(values, n_trials=10) > 0.0
    assert expected_max_sharpe(values, n_trials=10) < expected_max_sharpe(values, n_trials=1000)


def test_deflating_lowers_confidence():
    rng = np.random.default_rng(1)
    values = rng.normal(0.0, 0.01, 300)
    dsr, hurdle = deflated_sharpe_ratio(values, n_obs=2000)
    raw = probabilistic_sharpe_ratio(float(np.max(values)), n_obs=2000)
    assert hurdle > 0.0
    assert dsr < raw


def test_deflated_sharpe_handles_empty_and_flat():
    assert deflated_sharpe_ratio([], n_obs=100) == (0.0, 0.0)
    # Identical results: no variance, so nothing to correct for.
    assert deflated_sharpe_ratio([0.01] * 50, n_obs=100)[1] == 0.0


def test_per_bar_sharpe_matches_definition():
    idx = pd.date_range("2024-01-01", periods=500)
    rng = np.random.default_rng(2)
    returns = rng.normal(0.001, 0.01, 500)
    equity = pd.Series(100 * np.cumprod(1 + returns), index=idx)
    r = equity.pct_change().dropna()
    assert per_bar_sharpe(equity) == pytest.approx(float(r.mean() / r.std(ddof=1)), abs=1e-9)


# --------------------------------------------------------------------------
def test_buy_and_hold_starts_at_initial_cash():
    feed = _feed(days=5)
    equity = buy_and_hold_equity(feed.load(), 1_000_000.0)
    assert len(equity) > 1
    assert equity.iloc[0] == pytest.approx(1_000_000.0, rel=1e-9)


def test_buy_and_hold_tracks_prices():
    """With one symbol the benchmark is just the scaled price series."""
    feed = _feed(days=4)
    snaps = feed.load()
    equity = buy_and_hold_equity(snaps, 100_000.0)
    closes = pd.Series([s.bars["AAPL"].close for s in snaps], index=equity.index)
    assert np.allclose(equity.values, 100_000.0 * closes.values / closes.iloc[0])


def test_buy_and_hold_carries_price_through_missing_bars():
    """A symbol missing from a snapshot must not be valued at zero."""
    from atr.core.models import Bar, MarketSnapshot

    ts = [datetime(2024, 1, 1, 9, 30), datetime(2024, 1, 1, 9, 31)]
    snaps = [
        MarketSnapshot(ts=ts[0], bars={"A": Bar(ts=ts[0], open=10, high=10, low=10, close=10, volume=1)}),
        MarketSnapshot(ts=ts[1], bars={}),  # A absent
    ]
    equity = buy_and_hold_equity(snaps, 1_000.0)
    assert equity.iloc[1] == pytest.approx(1_000.0)


# --------------------------------------------------------------------------
def test_walk_forward_needs_enough_data():
    with pytest.raises(ValueError, match="at least"):
        walk_forward(
            _feed(days=2),
            SmaCrossover,
            config=WalkForwardConfig(train_bars=5_000, test_bars=1_000),
        )


def test_walk_forward_rejects_degenerate_windows():
    with pytest.raises(ValueError, match="must both be >= 2"):
        walk_forward(
            _feed(days=5),
            SmaCrossover,
            config=WalkForwardConfig(train_bars=1, test_bars=100),
        )


def test_walk_forward_test_window_always_follows_train():
    """The core guarantee: no fold is scored on data used to pick its params."""
    feed = _feed(days=7)
    result = walk_forward(
        feed,
        SmaCrossover,
        {"fast": [5, 10], "slow": [20, 30]},
        config=WalkForwardConfig(train_bars=1_000, test_bars=400, step_bars=400),
        backtest=BacktestConfig(initial_cash=1_000_000.0),
        validation=ValidationConfig(min_folds=1, min_trades=0),
    )
    assert len(result.folds) >= 3
    for fold in result.folds:
        assert fold.train_end < fold.test_start, "test window overlaps training data"
    # Windows advance monotonically.
    starts = [f.test_start for f in result.folds]
    assert starts == sorted(starts)


def test_walk_forward_reports_every_trial_not_just_the_winner():
    feed = _feed(days=7)
    result = walk_forward(
        feed,
        SmaCrossover,
        {"fast": [5, 10], "slow": [20, 30]},
        config=WalkForwardConfig(train_bars=1_000, test_bars=400, step_bars=400),
        validation=ValidationConfig(min_folds=1, min_trades=0),
    )
    combos = 4
    assert result.n_trials == combos * len(result.folds)
    for fold in result.folds:
        assert len(fold.trial_sharpes) == combos


def test_walk_forward_stitches_a_continuous_oos_curve():
    feed = _feed(days=7)
    result = walk_forward(
        feed,
        SmaCrossover,
        {"fast": [5, 10], "slow": [20, 30]},
        config=WalkForwardConfig(train_bars=1_000, test_bars=400, step_bars=400),
        backtest=BacktestConfig(initial_cash=500_000.0),
        validation=ValidationConfig(min_folds=1, min_trades=0),
    )
    expected_bars = sum(f.test_bars for f in result.folds)
    assert len(result.oos_equity) == expected_bars
    assert result.oos_equity.iloc[0] == pytest.approx(500_000.0, rel=1e-6)
    assert not result.oos_equity.isna().any()
    # Monotonic index: folds are stitched in time order.
    assert result.oos_equity.index.is_monotonic_increasing


def test_verdict_fails_when_there_are_too_few_trades():
    feed = _feed(days=7)
    result = walk_forward(
        feed,
        SmaCrossover,
        {"fast": [5], "slow": [20]},
        config=WalkForwardConfig(train_bars=1_000, test_bars=400, step_bars=400),
        validation=ValidationConfig(min_folds=99, min_trades=10_000),
    )
    assert not result.verdict.passed
    names = [c[0] for c in result.verdict.checks]
    assert any("folds" in n for n in names)
    assert any("trades" in n for n in names)
    assert "FAIL" in result.verdict.summary()


def test_verdict_can_pass_on_strong_evidence():
    """Positive control: the harness must be capable of saying yes.

    Without this, a harness that always says FAIL would look like it works.
    """
    from atr.backtest.metrics import compute_metrics
    from atr.research.validate import _verdict

    idx = pd.date_range("2024-01-01", periods=1000, freq="D")
    rng = np.random.default_rng(3)
    good_equity = pd.Series(
        1_000_000 * np.cumprod(1 + rng.normal(0.002, 0.004, 1000)), index=idx
    )
    flat_equity = pd.Series(np.full(1000, 1_000_000.0), index=idx)

    good = compute_metrics(good_equity, pd.DataFrame())
    bench = compute_metrics(flat_equity, pd.DataFrame())

    verdict = _verdict(
        [object()] * 3,
        good,
        bench,
        dsr=0.99,
        hurdle=0.02,
        cfg=ValidationConfig(min_folds=3, min_trades=0),
    )
    assert verdict.passed
    assert "PASS" in verdict.summary()


def test_warmup_lets_a_slow_strategy_trade_in_a_short_window():
    """Without a warmup prefix, a strategy needing history never trades at all.

    That failure is silent and looks identical to "the strategy has no edge",
    which makes it the most dangerous kind of bug in a validation harness.
    """
    from atr.signals.strategy import SignalEntryStrategy

    feed = _feed(days=7)
    strategy_kwargs = {"min_history_bars": 110, "lookback": 120, "allocation": 0.2}
    common = dict(
        config=WalkForwardConfig(train_bars=400, test_bars=60, step_bars=60),
        backtest=BacktestConfig(initial_cash=1_000_000.0),
        validation=ValidationConfig(min_folds=1, min_trades=0),
    )

    grid = {k: [v] for k, v in strategy_kwargs.items()}
    cold = walk_forward(feed, SignalEntryStrategy, grid, **common)
    warm = walk_forward(
        feed,
        SignalEntryStrategy,
        grid,
        config=WalkForwardConfig(train_bars=400, test_bars=60, step_bars=60, warmup_bars=300),
        backtest=common["backtest"],
        validation=common["validation"],
    )

    assert sum(f.test_metrics.num_trades for f in cold.folds) == 0
    assert sum(f.test_metrics.num_trades for f in warm.folds) > 0


def test_warmup_bars_are_not_scored():
    """Scored bars must be exactly the test window, warmup excluded."""
    feed = _feed(days=7)
    result = walk_forward(
        feed,
        SmaCrossover,
        {"fast": [5], "slow": [20]},
        config=WalkForwardConfig(train_bars=400, test_bars=60, step_bars=60, warmup_bars=200),
        validation=ValidationConfig(min_folds=1, min_trades=0),
    )
    for fold in result.folds:
        assert fold.test_bars == 60
        assert fold.test_equity.index[0] >= fold.test_start
        assert fold.test_equity.index[-1] <= fold.test_end


def test_to_frame_has_one_row_per_fold():
    feed = _feed(days=7)
    result = walk_forward(
        feed,
        SmaCrossover,
        {"fast": [5, 10], "slow": [20, 30]},
        config=WalkForwardConfig(train_bars=1_000, test_bars=400, step_bars=400),
        validation=ValidationConfig(min_folds=1, min_trades=0),
    )
    frame = result.to_frame()
    assert len(frame) == len(result.folds)
    assert "param_fast" in frame.columns and "param_slow" in frame.columns
    assert "test_sharpe" in frame.columns
