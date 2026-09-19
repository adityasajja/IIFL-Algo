"""Candidate backtesting, walk-forward validation, and robustness sensitivity engine.

Enforces:
1. Multi-metric evaluation (CAGR, DD, Sharpe, Sortino, PF, Win Rate, Trades, Expectancy, Return Distribution).
   Never optimizes for return alone.
2. Walk-forward out-of-sample validation using atr.research.validate.walk_forward.
   Rejects candidates that improve in-sample but fail out-of-sample.
3. Robustness checks: Scans parameter neighborhood (v-s, v, v+s) to prefer stable
   plateaus over isolated peaks (flags overfitting).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from atr.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from atr.data.base import DataFeed
from atr.optimization.adaptive import (
    AdaptiveParameter,
    apply_parameter_to_definition,
)
from atr.optimization.candidates import OptimizationCandidate
from atr.research.validate import WalkForwardConfig, walk_forward
from atr.signals.strategy import SignalEntryStrategy
from atr.strategy.base import Strategy
from atr.strategy.strategies import STRATEGIES


@dataclass(frozen=True)
class PerformanceMetrics:
    cagr_pct: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    profit_factor: float
    win_rate_pct: float
    num_trades: int
    avg_trade: float
    median_return: float | None = None
    trimmed_mean: float | None = None
    skew: float | None = None
    kurtosis: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WalkForwardMetrics:
    in_sample_sharpe: float
    out_of_sample_sharpe: float
    in_sample_profit_factor: float
    out_of_sample_profit_factor: float
    out_of_sample_cagr_pct: float
    out_of_sample_max_dd_pct: float
    out_of_sample_trades: int
    efficiency_ratio: float  # OOS Sharpe / IS Sharpe
    passed: bool
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RobustnessResult:
    parameter: str
    tested_values: list[float]
    metrics_by_value: dict[str, dict[str, float]]
    is_stable: bool
    is_isolated_spike: bool
    stable_range: tuple[float, float]
    sensitivity_verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter": self.parameter,
            "tested_values": self.tested_values,
            "metrics_by_value": self.metrics_by_value,
            "is_stable": self.is_stable,
            "is_isolated_spike": self.is_isolated_spike,
            "stable_range": list(self.stable_range),
            "sensitivity_verdict": self.sensitivity_verdict,
        }


@dataclass(frozen=True)
class EvaluationReport:
    candidate: OptimizationCandidate
    baseline_metrics: PerformanceMetrics
    candidate_metrics: PerformanceMetrics
    walk_forward_metrics: WalkForwardMetrics
    robustness_result: RobustnessResult
    recommendable: bool
    confidence: str  # "high", "medium", "low", "insufficient"
    reason: str
    status: str  # "RECOMMENDED", "REJECTED"


def _resolve_strategy_class_and_params(
    definition: dict[str, Any],
) -> tuple[type[Strategy], dict[str, Any]]:
    """Resolve a strategy class and constructor kwargs from definition."""
    engine_key = definition.get("engine_key")
    if engine_key and engine_key in STRATEGIES:
        cls = STRATEGIES[engine_key]
        params = dict(definition.get("params", {}))
        return cls, params

    # Default to SignalEntryStrategy for rule-based strategies
    params: dict[str, Any] = {}
    rules = definition.get("rules") if isinstance(definition.get("rules"), dict) else definition
    entry = rules.get("entry") or rules.get("entries") or {}
    exit_rules = rules.get("exit") or rules.get("exits") or {}

    params.update(entry)
    params.update(exit_rules)
    if "params" in definition and isinstance(definition["params"], dict):
        params.update(definition["params"])

    return SignalEntryStrategy, params


def _extract_metrics(result: BacktestResult) -> PerformanceMetrics:
    """Extract standard comparison metrics from a BacktestResult."""
    m = result.metrics
    trades = result.trades

    median_ret = None
    trimmed = None
    skew_val = None
    kurt_val = None

    if not trades.empty and "pnl_pct" in trades.columns:
        pnl = trades["pnl_pct"].dropna()
        if len(pnl) > 0:
            median_ret = float(pnl.median())
            if len(pnl) >= 4:
                # 10% trimmed mean
                sorted_pnl = sorted(pnl)
                cut = max(1, int(len(sorted_pnl) * 0.10))
                trimmed = float(np.mean(sorted_pnl[cut:-cut]))
            else:
                trimmed = float(pnl.mean())
            if len(pnl) >= 3:
                skew_val = float(pnl.skew())
            if len(pnl) >= 4:
                kurt_val = float(pnl.kurt())

    return PerformanceMetrics(
        cagr_pct=round(float(m.cagr_pct), 2),
        total_return_pct=round(float(m.total_return_pct), 2),
        max_drawdown_pct=round(float(m.max_drawdown_pct), 2),
        sharpe=round(float(m.sharpe), 2),
        sortino=round(float(m.sortino), 2),
        profit_factor=round(float(m.profit_factor), 2),
        win_rate_pct=round(float(m.win_rate_pct), 2),
        num_trades=int(m.num_trades),
        avg_trade=round(float(m.avg_trade), 2),
        median_return=round(median_ret, 4) if median_ret is not None else None,
        trimmed_mean=round(trimmed, 4) if trimmed is not None else None,
        skew=round(skew_val, 2) if skew_val is not None and not math.isnan(skew_val) else None,
        kurtosis=round(kurt_val, 2) if kurt_val is not None and not math.isnan(kurt_val) else None,
    )


def evaluate_backtest(
    feed: DataFeed,
    definition: dict[str, Any],
    config: BacktestConfig | None = None,
) -> tuple[BacktestResult, PerformanceMetrics]:
    """Run BacktestEngine on a strategy definition and compute detailed metrics."""
    cls, params = _resolve_strategy_class_and_params(definition)
    strategy_instance = cls(**params)
    engine = BacktestEngine(feed, strategy_instance, config or BacktestConfig())
    result = engine.run()
    metrics = _extract_metrics(result)
    return result, metrics


def evaluate_walk_forward(
    feed: DataFeed,
    definition: dict[str, Any],
    parameter_name: str,
    tested_value: float,
    *,
    wf_config: WalkForwardConfig | None = None,
    bt_config: BacktestConfig | None = None,
) -> WalkForwardMetrics:
    """Run standard walk-forward out-of-sample validation."""
    cls, base_params = _resolve_strategy_class_and_params(definition)

    # Param grid tests the candidate value in fold-by-fold walk-forward
    param_grid = {parameter_name: [tested_value]}
    wf_cfg = wf_config or WalkForwardConfig(train_bars=252, test_bars=63, step_bars=63)
    bt_cfg = bt_config or BacktestConfig()

    try:
        wf_result = walk_forward(feed, cls, param_grid=param_grid, config=wf_cfg, backtest=bt_cfg)
        oos_m = wf_result.oos_metrics
        # Calculate mean in-sample Sharpe across folds
        is_sharpes = [float(f.train_metrics.sharpe) for f in wf_result.folds if not math.isnan(float(f.train_metrics.sharpe))]
        is_pfs = [float(f.train_metrics.profit_factor) for f in wf_result.folds if not math.isnan(float(f.train_metrics.profit_factor))]

        avg_is_sharpe = float(np.mean(is_sharpes)) if is_sharpes else 0.0
        avg_is_pf = float(np.mean(is_pfs)) if is_pfs else 1.0
        oos_sharpe = float(oos_m.sharpe)
        oos_pf = float(oos_m.profit_factor)

        # Efficiency ratio = OOS Sharpe / IS Sharpe
        efficiency = round(oos_sharpe / avg_is_sharpe, 2) if avg_is_sharpe > 0 else (1.0 if oos_sharpe > 0 else 0.0)

        # Gating: out of sample must not collapse
        passed = (
            oos_sharpe >= 0.20
            and oos_pf >= 1.05
            and (avg_is_sharpe <= 0 or efficiency >= 0.35)
            and int(oos_m.num_trades) >= 5
        )

        verdict = (
            f"Passed OOS validation (OOS Sharpe {oos_sharpe:.2f}, PF {oos_pf:.2f}, efficiency {efficiency:.2f})"
            if passed
            else f"Failed OOS validation (OOS Sharpe {oos_sharpe:.2f}, PF {oos_pf:.2f}, efficiency {efficiency:.2f})"
        )

        return WalkForwardMetrics(
            in_sample_sharpe=round(avg_is_sharpe, 2),
            out_of_sample_sharpe=round(oos_sharpe, 2),
            in_sample_profit_factor=round(avg_is_pf, 2),
            out_of_sample_profit_factor=round(oos_pf, 2),
            out_of_sample_cagr_pct=round(float(oos_m.cagr_pct), 2),
            out_of_sample_max_dd_pct=round(float(oos_m.max_drawdown_pct), 2),
            out_of_sample_trades=int(oos_m.num_trades),
            efficiency_ratio=efficiency,
            passed=passed,
            verdict=verdict,
        )
    except Exception as exc:
        return WalkForwardMetrics(
            in_sample_sharpe=0.0,
            out_of_sample_sharpe=0.0,
            in_sample_profit_factor=0.0,
            out_of_sample_profit_factor=0.0,
            out_of_sample_cagr_pct=0.0,
            out_of_sample_max_dd_pct=0.0,
            out_of_sample_trades=0,
            efficiency_ratio=0.0,
            passed=False,
            verdict=f"Walk-forward validation could not complete: {exc}",
        )


def evaluate_robustness(
    feed: DataFeed,
    base_definition: dict[str, Any],
    param_spec: AdaptiveParameter,
    proposed_val: float,
    config: BacktestConfig | None = None,
) -> RobustnessResult:
    """Test parameter sensitivity in the neighborhood of proposed_val (e.g. 1.8, 1.9, 2.0, 2.1).

    Verifies parameter stability: if only exactly proposed_val works and nearby
    values collapse, flags overfitting.
    """
    step = param_spec.step
    test_offsets = [-2 * step, -1 * step, 0.0, 1 * step, 2 * step]
    tested_values: list[float] = []

    for offset in test_offsets:
        v = round(proposed_val + offset, 4)
        if param_spec.minimum <= v <= param_spec.maximum and v not in tested_values:
            tested_values.append(v)
    tested_values.sort()

    metrics_by_value: dict[str, dict[str, float]] = {}
    peak_sharpe = -np.inf
    peak_val = proposed_val

    for v in tested_values:
        cand_def = apply_parameter_to_definition(
            base_definition, param_spec.name, v, block=param_spec.block
        )
        try:
            _, m = evaluate_backtest(feed, cand_def, config)
            metrics_by_value[str(v)] = {
                "sharpe": m.sharpe,
                "profit_factor": m.profit_factor,
                "cagr_pct": m.cagr_pct,
                "max_drawdown_pct": m.max_drawdown_pct,
                "num_trades": m.num_trades,
            }
            if m.sharpe > peak_sharpe:
                peak_sharpe = m.sharpe
                peak_val = v
        except Exception:
            metrics_by_value[str(v)] = {
                "sharpe": 0.0,
                "profit_factor": 0.0,
                "cagr_pct": 0.0,
                "max_drawdown_pct": 100.0,
                "num_trades": 0,
            }

    # Analyze stability in the neighborhood
    # Adjacent neighbors are tested_values immediately below and above proposed_val
    idx = tested_values.index(proposed_val) if proposed_val in tested_values else -1
    neighbors = []
    if idx > 0:
        neighbors.append(tested_values[idx - 1])
    if idx >= 0 and idx < len(tested_values) - 1:
        neighbors.append(tested_values[idx + 1])

    target_m = metrics_by_value.get(str(proposed_val), {})
    target_sharpe = target_m.get("sharpe", 0.0)
    target_pf = target_m.get("profit_factor", 1.0)

    # Check for isolated spike: if target looks great but all neighbors lose money or drop by >50%
    neighbor_sharpes = [metrics_by_value[str(nv)].get("sharpe", 0.0) for nv in neighbors]
    neighbor_pfs = [metrics_by_value[str(nv)].get("profit_factor", 0.0) for nv in neighbors]

    is_isolated_spike = False
    if target_sharpe > 1.0 and neighbors:
        collapsed = all(ns < 0.3 * target_sharpe or npf < 1.0 for ns, npf in zip(neighbor_sharpes, neighbor_pfs))
        if collapsed:
            is_isolated_spike = True

    # Stable plateau exists if at least one neighbor retains profitable / acceptable performance
    stable_plateau = (
        not is_isolated_spike
        and all(npf >= 1.05 for npf in neighbor_pfs)
        if neighbor_pfs
        else (target_pf >= 1.1)
    )

    # Compute stable range bounds
    stable_pts = [
        v for v in tested_values
        if metrics_by_value.get(str(v), {}).get("profit_factor", 0.0) >= 1.05
    ]
    min_stable = min(stable_pts) if stable_pts else proposed_val
    max_stable = max(stable_pts) if stable_pts else proposed_val

    if is_isolated_spike:
        verdict = f"Isolated peak at {proposed_val} (adjacent neighbors collapse, overfitting risk flagged)"
    elif stable_plateau:
        verdict = f"Stable parameter plateau between {min_stable} and {max_stable}"
    else:
        verdict = f"Moderate sensitivity around {proposed_val} (acceptable variance)"

    return RobustnessResult(
        parameter=param_spec.name,
        tested_values=tested_values,
        metrics_by_value=metrics_by_value,
        is_stable=not is_isolated_spike,
        is_isolated_spike=is_isolated_spike,
        stable_range=(min_stable, max_stable),
        sensitivity_verdict=verdict,
    )


def evaluate_candidate(
    feed: DataFeed,
    candidate: OptimizationCandidate,
    param_spec: AdaptiveParameter,
    *,
    bt_config: BacktestConfig | None = None,
    wf_config: WalkForwardConfig | None = None,
) -> EvaluationReport:
    """Run full multi-metric backtest, walk-forward, and robustness verification on a candidate."""
    # 1. Backtest Baseline
    _, base_m = evaluate_backtest(feed, candidate.baseline_definition, bt_config)

    # 2. Backtest Candidate
    _, cand_m = evaluate_backtest(feed, candidate.candidate_definition, bt_config)

    # 3. Walk-Forward OOS Validation
    wf_m = evaluate_walk_forward(
        feed,
        candidate.baseline_definition,
        candidate.parameter_name,
        candidate.proposed_value,
        wf_config=wf_config,
        bt_config=bt_config,
    )

    # 4. Robustness Sensitivity Scan
    rob = evaluate_robustness(
        feed,
        candidate.baseline_definition,
        param_spec,
        candidate.proposed_value,
        config=bt_config,
    )

    # 5. Synthesis & Multi-Objective Comparison
    # Not return alone: Sharpe, Drawdown, Profit Factor, Walk-Forward, and Robustness must align!
    sharpe_improved = cand_m.sharpe >= base_m.sharpe * 0.95 or cand_m.sharpe > 1.0
    pf_improved = cand_m.profit_factor >= base_m.profit_factor
    dd_acceptable = cand_m.max_drawdown_pct <= (base_m.max_drawdown_pct * 1.20) + 2.0
    trades_sufficient = cand_m.num_trades >= 5

    recommendable = (
        sharpe_improved
        and pf_improved
        and dd_acceptable
        and trades_sufficient
        and wf_m.passed
        and rob.is_stable
    )

    rejection_reasons = []
    if not sharpe_improved:
        rejection_reasons.append(f"Sharpe declined ({base_m.sharpe} -> {cand_m.sharpe})")
    if not pf_improved:
        rejection_reasons.append(f"Profit factor declined ({base_m.profit_factor} -> {cand_m.profit_factor})")
    if not dd_acceptable:
        rejection_reasons.append(f"Drawdown degraded ({base_m.max_drawdown_pct}% -> {cand_m.max_drawdown_pct}%)")
    if not trades_sufficient:
        rejection_reasons.append(f"Insufficient trade count ({cand_m.num_trades})")
    if not wf_m.passed:
        rejection_reasons.append(f"Failed walk-forward OOS validation ({wf_m.verdict})")
    if not rob.is_stable:
        rejection_reasons.append(f"Failed robustness check: {rob.sensitivity_verdict}")

    status = "RECOMMENDED" if recommendable else "REJECTED"
    reason = (
        f"Candidate improves risk-adjusted metrics (PF {base_m.profit_factor} -> {cand_m.profit_factor}, "
        f"Sharpe {base_m.sharpe} -> {cand_m.sharpe}), passes walk-forward OOS ({wf_m.efficiency_ratio:.2f} eff), "
        f"and demonstrates stability in {rob.stable_range}."
        if recommendable
        else f"Candidate rejected: {'; '.join(rejection_reasons)}"
    )

    confidence = "high" if (recommendable and candidate.sample_size >= 30 and wf_m.efficiency_ratio >= 0.5) else (
        "medium" if recommendable else "low"
    )

    return EvaluationReport(
        candidate=candidate,
        baseline_metrics=base_m,
        candidate_metrics=cand_m,
        walk_forward_metrics=wf_m,
        robustness_result=rob,
        recommendable=recommendable,
        confidence=confidence,
        reason=reason,
        status=status,
    )
