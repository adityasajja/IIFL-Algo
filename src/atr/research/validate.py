"""Out-of-sample validation: walk-forward, sweeps, and self-scepticism.

Why this module exists
----------------------
A single backtest is not evidence. Fit a strategy on a price series, try
enough parameter combinations, and the best one will look excellent purely by
chance — roughly the statistical equivalent of flipping coins until one comes
up heads ten times and then declaring that coin lucky. This module exists to
make that specific failure mode impossible to miss:

* :func:`walk_forward` — every parameter is chosen using *only* data that
  precedes the window it is scored on. The reported equity curve is stitched
  from windows the selection process never saw.
* :func:`deflated_sharpe_ratio` — adjusts the Sharpe ratio for how many
  combinations were tried. Trying 500 combos and picking the winner demands far
  stronger evidence than testing one idea.
* :func:`buy_and_hold_equity` — the benchmark. A strategy that cannot beat
  doing nothing is not a strategy, it is a way to pay commission.

Deliberately absent: any notion of conviction, narrative, or "the market feels
like". A number either survives out-of-sample testing or it does not.

References
----------
Bailey & López de Prado, *The Deflated Sharpe Ratio* (2014) and
*The Sharpe Ratio Efficient Frontier* (2012).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field, replace
from statistics import NormalDist

import numpy as np
import pandas as pd

from atr.backtest.engine import BacktestConfig, BacktestEngine
from atr.backtest.metrics import Metrics, compute_metrics
from atr.data.base import DataFeed, ListFeed
from atr.strategy.base import Strategy

_NORM = NormalDist()

#: Euler–Mascheroni constant, used in the expected-maximum-Sharpe estimate.
_EULER = 0.5772156649015329


# --------------------------------------------------------------------------
# Significance statistics
# --------------------------------------------------------------------------


def per_bar_sharpe(equity: pd.Series) -> float:
    """Sharpe at the *observation* frequency (not annualised).

    Significance tests must use the Sharpe and the return moments at the same
    frequency — feeding an annualised Sharpe into a formula that expects
    per-bar skew and kurtosis silently inflates the result.
    """
    returns = equity.pct_change().dropna()
    if len(returns) < 2:
        return 0.0
    sd = float(returns.std(ddof=1))
    if sd <= 1e-12:
        return 0.0
    return float(returns.mean() / sd)


def probabilistic_sharpe_ratio(
    sharpe: float,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    benchmark: float = 0.0,
) -> float:
    """P(true Sharpe > ``benchmark``) given an estimated Sharpe.

    ``sharpe`` is per-bar (see :func:`per_bar_sharpe`). ``kurtosis`` is the
    raw fourth moment — 3.0 for a normal, so ``pandas.Series.kurt()`` (which
    returns *excess*) needs 3 added back to it.

    A Sharpe of 1.0 from 20 observations and a Sharpe of 1.0 from 20,000 are
    not the same claim; this is the function that says so.
    """
    if n_obs <= 1:
        return 0.0
    denom_sq = 1.0 - skew * sharpe + (kurtosis - 1.0) / 4.0 * sharpe**2
    if denom_sq <= 0:
        return 0.0
    z = (sharpe - benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom_sq)
    return float(_NORM.cdf(z))


def expected_max_sharpe(trial_sharpes, n_trials: int | None = None) -> float:
    """Sharpe you'd expect from the best of N trials *if nothing worked*.

    This is the hurdle the observed Sharpe has to clear.
    """
    values = np.asarray(list(trial_sharpes), dtype=float)
    values = values[np.isfinite(values)]
    count = n_trials or len(values)
    if count < 2 or len(values) < 2:
        return 0.0
    std = float(values.std(ddof=1))
    if std <= 0:
        return 0.0
    z_single = _NORM.inv_cdf(1.0 - 1.0 / count)
    z_many = _NORM.inv_cdf(1.0 - 1.0 / (count * math.e))
    return std * ((1.0 - _EULER) * z_single + _EULER * z_many)


def deflated_sharpe_ratio(
    trial_sharpes,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> tuple[float, float]:
    """Sharpe adjusted for how many parameter combinations were tried.

    Returns ``(deflated_sharpe, required_sharpe)`` where ``deflated_sharpe``
    is P(true Sharpe > the multiple-testing hurdle) for the *best* trial, and
    ``required_sharpe`` is that hurdle.

    Pass every Sharpe you computed while searching — including the ones you
    rejected. Reporting only the winner is exactly the bias this corrects.
    """
    values = np.asarray(list(trial_sharpes), dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return 0.0, 0.0
    best = float(values.max())
    hurdle = expected_max_sharpe(values, len(values))
    return probabilistic_sharpe_ratio(best, n_obs, skew, kurtosis, benchmark=hurdle), hurdle


# --------------------------------------------------------------------------
# Benchmark
# --------------------------------------------------------------------------


def buy_and_hold_equity(
    snapshots,
    initial_cash: float,
) -> pd.Series:
    """Equal-weight buy-and-hold over the same window — the "do nothing" bar.

    Buys every symbol at the first bar's close and never trades. Costs are
    ignored, which makes the benchmark *generous*; a strategy that can't beat
    it is definitely not earning its commission.
    """
    if not snapshots:
        return pd.Series(dtype=float)

    first = snapshots[0]
    entries = [
        (symbol, bar.close)
        for symbol, bar in first.bars.items()
        if bar is not None and np.isfinite(bar.close) and bar.close > 0
    ]
    if not entries:
        return pd.Series(dtype=float)

    per_symbol = initial_cash / len(entries)
    quantity = {symbol: per_symbol / price for symbol, price in entries}
    last_price = dict(entries)

    index, values = [], []
    for snap in snapshots:
        total = 0.0
        for symbol in quantity:
            bar = snap.bars.get(symbol)
            if bar is not None and np.isfinite(bar.close) and bar.close > 0:
                last_price[symbol] = bar.close
            total += quantity[symbol] * last_price[symbol]
        index.append(snap.ts)
        values.append(total)

    return pd.Series(values, index=pd.DatetimeIndex(index), name="buy_and_hold")


# --------------------------------------------------------------------------
# Walk-forward
# --------------------------------------------------------------------------


@dataclass
class WalkForwardConfig:
    train_bars: int = 5_000
    test_bars: int = 1_250
    #: How far the window slides. Defaults to ``test_bars`` (no overlap).
    step_bars: int | None = None
    #: Keep the training window's start fixed so it grows (anchored), instead
    #: of sliding with a fixed length.
    anchored: bool = False
    #: Bars prepended before each test window purely so indicators can warm up.
    #: They are run but never scored, and no trading is allowed during them.
    #: Set this to at least the strategy's longest indicator lookback.
    warmup_bars: int = 0
    #: Metric used to pick parameters on the training window only.
    selection_metric: str = "sharpe"


@dataclass
class FoldResult:
    fold: int
    params: dict
    train_bars: int
    test_bars: int
    train_start: object
    train_end: object
    test_start: object
    test_end: object
    train_metrics: Metrics
    test_metrics: Metrics
    test_equity: pd.Series
    test_trades: pd.DataFrame
    #: Every train Sharpe seen while searching, not just the winner.
    trial_sharpes: list[float] = field(default_factory=list)


@dataclass
class ValidationConfig:
    min_folds: int = 3
    min_trades: int = 100
    #: Required P(true Sharpe beats the multiple-testing hurdle).
    min_confidence: float = 0.95
    max_drawdown_pct: float = 40.0
    require_beat_benchmark: bool = True


@dataclass
class Verdict:
    passed: bool
    checks: list[tuple[str, bool, str]]

    def summary(self) -> str:
        lines = ["VERDICT: " + ("PASS" if self.passed else "FAIL")]
        for name, ok, detail in self.checks:
            lines.append(f"  [{'x' if ok else ' '}] {name}: {detail}")
        return "\n".join(lines)


@dataclass
class WalkForwardResult:
    folds: list[FoldResult]
    oos_equity: pd.Series
    oos_metrics: Metrics
    benchmark_metrics: Metrics
    deflated_sharpe: float
    required_sharpe: float
    n_trials: int
    verdict: Verdict

    def to_frame(self) -> pd.DataFrame:
        """One row per fold — convenient for CSV and for spotting lucky folds."""
        rows = []
        for f in self.folds:
            rows.append(
                {
                    "fold": f.fold,
                    "train_start": f.train_start,
                    "train_end": f.train_end,
                    "test_start": f.test_start,
                    "test_end": f.test_end,
                    **{f"param_{k}": v for k, v in f.params.items()},
                    "train_sharpe": f.train_metrics.sharpe,
                    "test_sharpe": f.test_metrics.sharpe,
                    "test_return_pct": f.test_metrics.total_return_pct,
                    "test_max_dd_pct": f.test_metrics.max_drawdown_pct,
                    "test_trades": f.test_metrics.num_trades,
                    "trials": len(f.trial_sharpes),
                }
            )
        return pd.DataFrame(rows)

    def summary(self) -> str:
        lines = [
            "=== walk-forward (out-of-sample) ===",
            f"folds               : {len(self.folds)}",
            f"parameters tried    : {self.n_trials}",
            f"OOS total return    : {self.oos_metrics.total_return_pct:.2f}%",
            f"OOS Sharpe          : {self.oos_metrics.sharpe:.2f}",
            f"OOS max drawdown    : {self.oos_metrics.max_drawdown_pct:.2f}%",
            f"OOS trades          : {self.oos_metrics.num_trades}",
            f"buy & hold return   : {self.benchmark_metrics.total_return_pct:.2f}%",
            f"buy & hold Sharpe   : {self.benchmark_metrics.sharpe:.2f}",
            f"required Sharpe     : {self.required_sharpe:.4f} (hurdle for {self.n_trials} trials)",
            f"deflated Sharpe     : {self.deflated_sharpe:.3f}",
            "",
            self.verdict.summary(),
        ]
        return "\n".join(lines)


def _grid(param_grid: dict[str, list] | None) -> list[dict]:
    if not param_grid:
        return [{}]
    keys = list(param_grid)
    return [
        dict(zip(keys, combo, strict=True))
        for combo in itertools.product(*(param_grid[k] for k in keys))
    ]


def _score(metrics: Metrics, name: str) -> float:
    value = getattr(metrics, name, None)
    if value is None:
        raise ValueError(f"unknown selection metric: {name}")
    return float(value)


def _run(snapshots, instruments, strategy_cls, params, backtest_config) -> object:
    feed = ListFeed(snapshots, instruments)
    engine = BacktestEngine(feed, strategy_cls(**params), backtest_config)
    return engine.run()


def walk_forward(
    feed: DataFeed,
    strategy_cls: type[Strategy],
    param_grid: dict[str, list] | None = None,
    *,
    config: WalkForwardConfig | None = None,
    backtest: BacktestConfig | None = None,
    validation: ValidationConfig | None = None,
) -> WalkForwardResult:
    """Walk a strategy forward, choosing parameters only on prior data.

    For each fold: search ``param_grid`` on the training window, take the
    winner, then score it once on the following unseen window. The returned
    equity curve is the concatenation of those unseen windows only.
    """
    cfg = config or WalkForwardConfig()
    bt = backtest or BacktestConfig()
    val = validation or ValidationConfig()

    snapshots = feed.load()
    instruments = feed.instruments
    total = len(snapshots)
    combos = _grid(param_grid)
    step = cfg.step_bars or cfg.test_bars

    if cfg.train_bars < 2 or cfg.test_bars < 2:
        raise ValueError("train_bars and test_bars must both be >= 2")
    if total < cfg.train_bars + cfg.test_bars:
        raise ValueError(
            f"need at least {cfg.train_bars + cfg.test_bars} bars, got {total}"
        )

    folds: list[FoldResult] = []
    start = 0
    fold_index = 0
    warmup = max(int(cfg.warmup_bars), 0)
    while start + cfg.train_bars + cfg.test_bars <= total:
        train_start = 0 if cfg.anchored else start
        train = snapshots[train_start : start + cfg.train_bars]
        test_index = start + cfg.train_bars
        test_end_index = test_index + cfg.test_bars
        test = snapshots[test_index:test_end_index]

        # Prepend a warmup prefix that is run but not scored, so the strategy
        # has indicator history from the first scored bar. Without it, every
        # test window opens with NaN indicators and the strategy is handicapped
        # precisely where it is being measured.
        warm_start = max(test_index - warmup, 0)
        window = snapshots[warm_start:test_end_index]
        bt_window = replace(bt, warmup_bars=test_index - warm_start)

        # --- search on train only -------------------------------------
        trial_sharpes: list[float] = []
        best_params: dict = {}
        best_score = -np.inf
        best_result = None
        for params in combos:
            result = _run(train, instruments, strategy_cls, params, bt)
            score = _score(result.metrics, cfg.selection_metric)
            trial_sharpes.append(per_bar_sharpe(result.equity))
            if score > best_score:
                best_score, best_params, best_result = score, params, result

        # --- score once on the unseen window --------------------------
        raw = _run(window, instruments, strategy_cls, best_params, bt_window)
        score_from = test[0].ts
        test_equity = raw.equity[raw.equity.index >= score_from]
        if test_equity.empty:
            raise ValueError(f"fold {fold_index}: no equity inside the test window")
        # warmup_bars blocks on_bar, so nothing was traded before the window.
        test_metrics = compute_metrics(
            test_equity,
            raw.trades,
            risk_free_rate=bt.risk_free_rate,
            total_commission=float(raw.metrics.total_commission),
            total_slippage=float(raw.metrics.total_slippage),
            final_positions=raw.metrics.final_positions,
        )

        folds.append(
            FoldResult(
                fold=fold_index,
                params=best_params,
                train_bars=len(train),
                test_bars=len(test_equity),
                train_start=train[0].ts,
                train_end=train[-1].ts,
                test_start=test[0].ts,
                test_end=test[-1].ts,
                train_metrics=best_result.metrics,
                test_metrics=test_metrics,
                test_equity=test_equity,
                test_trades=raw.trades,
                trial_sharpes=trial_sharpes,
            )
        )
        fold_index += 1
        start += step

    if not folds:
        raise ValueError("no complete folds — window sizes too large for this history")

    # --- stitch the out-of-sample windows into one equity curve -------
    level = bt.initial_cash
    parts = []
    for f in folds:
        scaled = f.test_equity / float(f.test_equity.iloc[0]) * level
        level = float(scaled.iloc[-1])
        parts.append(scaled)
    oos_equity = pd.concat(parts)
    oos_equity = oos_equity[~oos_equity.index.duplicated(keep="last")]
    oos_equity.name = "equity"

    all_trades = (
        pd.concat([f.test_trades for f in folds if not f.test_trades.empty])
        if any(not f.test_trades.empty for f in folds)
        else pd.DataFrame()
    )
    oos_metrics = compute_metrics(
        oos_equity,
        all_trades,
        risk_free_rate=bt.risk_free_rate,
        total_commission=sum(f.test_metrics.total_commission for f in folds),
        total_slippage=sum(f.test_metrics.total_slippage for f in folds),
        final_positions=folds[-1].test_metrics.final_positions,
    )

    # --- benchmark over the same window -------------------------------
    window = [s for s in snapshots if folds[0].test_start <= s.ts <= folds[-1].test_end]
    bench_equity = buy_and_hold_equity(window, bt.initial_cash)
    if len(bench_equity) > 1:
        bench_metrics = compute_metrics(
            bench_equity, pd.DataFrame(), risk_free_rate=bt.risk_free_rate
        )
    else:
        # No usable prices: score the benchmark as flat, not as a pass.
        flat = pd.Series(bt.initial_cash, index=oos_equity.index, dtype=float)
        bench_metrics = compute_metrics(flat, pd.DataFrame(), risk_free_rate=bt.risk_free_rate)

    # --- significance, corrected for everything we tried --------------
    returns = oos_equity.pct_change().dropna()
    skew = float(returns.skew()) if len(returns) > 2 else 0.0
    kurtosis = float(returns.kurt()) + 3.0 if len(returns) > 3 else 3.0
    all_trials = [s for f in folds for s in f.trial_sharpes]
    dsr, hurdle = deflated_sharpe_ratio(all_trials, len(returns), skew, kurtosis)

    verdict = _verdict(folds, oos_metrics, bench_metrics, dsr, hurdle, val)
    return WalkForwardResult(
        folds=folds,
        oos_equity=oos_equity,
        oos_metrics=oos_metrics,
        benchmark_metrics=bench_metrics,
        deflated_sharpe=dsr,
        required_sharpe=hurdle,
        n_trials=len(all_trials),
        verdict=verdict,
    )


def _verdict(
    folds: list[FoldResult],
    oos: Metrics,
    bench: Metrics,
    dsr: float,
    hurdle: float,
    cfg: ValidationConfig,
) -> Verdict:
    checks: list[tuple[str, bool, str]] = []

    ok = len(folds) >= cfg.min_folds
    checks.append((
        f"at least {cfg.min_folds} out-of-sample folds",
        ok,
        f"{len(folds)} folds",
    ))

    ok = oos.num_trades >= cfg.min_trades
    checks.append((
        f"at least {cfg.min_trades} trades",
        ok,
        f"{oos.num_trades} trades — {'enough' if ok else 'too few to mean anything'}",
    ))

    ok = dsr >= cfg.min_confidence
    checks.append((
        f"deflated Sharpe >= {cfg.min_confidence:.2f}",
        ok,
        f"{dsr:.3f} vs required Sharpe {hurdle:.4f}",
    ))

    ok = oos.sharpe > 0
    checks.append(("positive out-of-sample Sharpe", ok, f"{oos.sharpe:.2f}"))

    if cfg.require_beat_benchmark:
        ok = oos.sharpe > bench.sharpe
        checks.append((
            "beats buy & hold",
            ok,
            f"{oos.sharpe:.2f} vs {bench.sharpe:.2f}",
        ))

    ok = oos.max_drawdown_pct <= cfg.max_drawdown_pct
    checks.append((
        f"max drawdown <= {cfg.max_drawdown_pct:.0f}%",
        ok,
        f"{oos.max_drawdown_pct:.2f}%",
    ))

    return Verdict(passed=all(c[1] for c in checks), checks=checks)
