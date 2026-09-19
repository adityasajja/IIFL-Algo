"""Strategy Experiment Lab Service.

Objective side-by-side comparison:
CURRENT STRATEGY vs LEARNED CANDIDATE vs BASELINE
→ Evaluated under identical assumptions: dates, universe, capital, costs, slippage.
→ Deep comparative metrics: returns, max DD, Sharpe, Sortino, profit factor, win rate, trades, median trade, OOS efficiency.
→ Curve comparisons: daily equity curves, drawdown curves, monthly returns, trade-return distributions, regime performance.
→ Structured explanations: WHAT CHANGED, WHY PROPOSED, WHAT EXPERIMENT FOUND.
→ Zero autonomous deployment: strictly produces research findings & approvals.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

from atr.appdb.engine import AppDatabase, get_app_db, utcnow
from atr.appdb.repositories import (
    OptimizationRecommendationRepository,
    StrategyExperimentRepository,
    StrategyRepository,
)
from atr.backtest.engine import BacktestConfig, BacktestResult
from atr.data.base import DataFeed
from atr.data.synthetic import SyntheticConfig, SyntheticFeed
from atr.optimization.adaptive import (
    apply_parameter_to_definition,
    extract_adaptive_parameters,
)
from atr.optimization.evaluator import (
    PerformanceMetrics,
    RobustnessResult,
    WalkForwardMetrics,
    evaluate_backtest,
    evaluate_robustness,
    evaluate_walk_forward,
)
from atr.research.validate import WalkForwardConfig


def _round_float(val: Any, decimals: int = 2) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, decimals)
    except (ValueError, TypeError):
        return None


def _calc_curve_points(series: pd.Series, max_points: int = 150) -> list[dict[str, Any]]:
    """Sample time-series curve to reasonable point count for frontend visualization."""
    if series.empty:
        return []
    s = series.dropna()
    total = len(s)
    step = max(1, total // max_points) if total > max_points else 1
    sampled = s.iloc[::step]
    if total > 0 and (total - 1) not in sampled.index:
        sampled = pd.concat([sampled, s.iloc[[-1]]])

    points = []
    for dt, val in sampled.items():
        ts_str = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        points.append({"ts": ts_str, "value": round(float(val), 2)})
    return points


def _calc_drawdown_curve(equity_series: pd.Series) -> pd.Series:
    """Compute underwater drawdown series in percent."""
    if equity_series.empty:
        return pd.Series(dtype=float)
    peak = equity_series.cummax()
    dd = (equity_series - peak) / peak * 100.0
    return dd


def _calc_monthly_returns(equity_series: pd.Series) -> list[dict[str, Any]]:
    """Calculate monthly return grid from daily equity series."""
    if equity_series.empty:
        return []
    try:
        resampled = equity_series.resample("ME").last()
        if len(resampled) < 2:
            return []
        pcts = resampled.pct_change().dropna() * 100.0
        out = []
        for dt, ret in pcts.items():
            out.append({
                "year": int(dt.year),
                "month": int(dt.month),
                "return_pct": round(float(ret), 2),
            })
        return out
    except Exception:
        return []


def _calc_return_distribution(trades_df: pd.DataFrame) -> dict[str, Any]:
    """Calculate histogram and moment statistics of trade return distribution."""
    if trades_df.empty or "pnl_pct" not in trades_df.columns:
        return {
            "buckets": [],
            "mean": 0.0,
            "median": 0.0,
            "std": 0.0,
            "skew": 0.0,
            "kurtosis": 0.0,
        }

    pnl = trades_df["pnl_pct"].dropna()
    if len(pnl) == 0:
        return {"buckets": [], "mean": 0.0, "median": 0.0, "std": 0.0, "skew": 0.0, "kurtosis": 0.0}

    bins = [-float("inf"), -5.0, -2.0, -1.0, 0.0, 1.0, 2.0, 5.0, float("inf")]
    labels = ["<-5%", "-5% to -2%", "-2% to -1%", "-1% to 0%", "0% to 1%", "1% to 2%", "2% to 5%", ">5%"]
    cats = pd.cut(pnl, bins=bins, labels=labels)
    counts = cats.value_counts(sort=False).to_dict()

    buckets = [{"label": str(k), "count": int(v)} for k, v in counts.items()]
    return {
        "buckets": buckets,
        "mean": _round_float(pnl.mean(), 2) or 0.0,
        "median": _round_float(pnl.median(), 2) or 0.0,
        "std": _round_float(pnl.std(), 2) or 0.0,
        "skew": _round_float(pnl.skew(), 2) or 0.0,
        "kurtosis": _round_float(pnl.kurt(), 2) or 0.0,
    }


def _calc_regime_performance(trades_df: pd.DataFrame) -> list[dict[str, Any]]:
    """Aggregate trade performance by market regime if available."""
    if trades_df.empty:
        return []
    col = "market_regime" if "market_regime" in trades_df.columns else "regime"
    if col not in trades_df.columns:
        return []

    out = []
    for regime, group in trades_df.groupby(col):
        n = len(group)
        if n == 0:
            continue
        pnl = group["pnl_pct"].dropna() if "pnl_pct" in group.columns else pd.Series(dtype=float)
        wins = sum(1 for p in pnl if p > 0)
        win_rate = round((wins / n) * 100.0, 1) if n > 0 else 0.0
        mean_ret = round(float(pnl.mean()), 2) if len(pnl) > 0 else 0.0
        out.append({
            "regime": str(regime),
            "trades": n,
            "win_rate_pct": win_rate,
            "mean_return_pct": mean_ret,
        })
    return out


class StrategyExperimentService:
    """Orchestrates strategy experiments comparing Baseline vs Candidate under identical assumptions."""

    def __init__(self, db: AppDatabase | None = None) -> None:
        self._db = db

    @property
    def db(self) -> AppDatabase:
        if self._db is not None:
            return self._db
        return get_app_db()

    def _default_feed(self) -> DataFeed:
        """Standard synthetic feed for deterministic comparisons when live feed is absent."""
        return SyntheticFeed(
            SyntheticConfig(
                symbols=("NIFTY50",),
                start=datetime(2024, 1, 1, 9, 30),
                end=datetime(2024, 6, 1, 15, 30),
                freq="1D",
                seed=42,
            )
        )

    def create_experiment(
        self,
        strategy_id: str,
        *,
        source_version: int | None = None,
        parameter_changes: dict[str, Any] | None = None,
        creator_user_id: str,
        name: str | None = None,
        reason: str | None = None,
        recommendation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new experiment record from strategy version and parameter adjustments."""
        with self.db.session() as session:
            # 1. Resolve strategy version
            if source_version is None:
                latest_ver = StrategyRepository.latest_version(session, strategy_id)
                if latest_ver is None:
                    raise LookupError(f"No version found for strategy {strategy_id}")
                source_ver_row = latest_ver
            else:
                source_ver_row = StrategyRepository.version(session, strategy_id, source_version)
                if source_ver_row is None:
                    raise LookupError(f"Strategy {strategy_id} v{source_version} not found")

            src_v = int(source_ver_row["version"])
            raw_def = source_ver_row["definition"]
            baseline_def = json.loads(raw_def) if isinstance(raw_def, str) else dict(raw_def)

            # 2. Check recommendation if provided
            rec_row = None
            if recommendation_id:
                rec_row = OptimizationRecommendationRepository.get(session, recommendation_id)
                if rec_row and not reason:
                    reason = f"Derived from recommendation {recommendation_id}: {rec_row.get('reason')}"

            # If parameter_changes not passed, infer from recommendation
            if not parameter_changes and rec_row:
                parameter_changes = {rec_row["parameter"]: rec_row["proposed_value"]}
            elif not parameter_changes:
                parameter_changes = {}

            # 3. Apply parameter changes to candidate definition
            candidate_def = json.loads(json.dumps(baseline_def))
            adaptive_specs = extract_adaptive_parameters(baseline_def)

            for p_name, p_val in parameter_changes.items():
                val_to_apply = p_val.get("proposed") if isinstance(p_val, dict) else p_val
                # Ensure parameter is allowed
                if p_name in adaptive_specs:
                    spec = adaptive_specs[p_name]
                    # Clamp to bounds
                    val_to_apply = max(spec.minimum, min(spec.maximum, float(val_to_apply)))
                candidate_def = apply_parameter_to_definition(candidate_def, p_name, float(val_to_apply))

            exp_name = name or f"Experiment: {', '.join(parameter_changes.keys())} on v{src_v}"
            exp_reason = reason or f"Comparative evaluation of {list(parameter_changes.keys())} against v{src_v}"

            return StrategyExperimentRepository.create(
                session,
                strategy_id=strategy_id,
                source_version=src_v,
                creator_user_id=creator_user_id,
                name=exp_name,
                reason=exp_reason,
                parameter_changes=parameter_changes,
                baseline_definition=baseline_def,
                candidate_definition=candidate_def,
                recommendation_id=recommendation_id,
            )

    def run_experiment(
        self,
        experiment_id: str,
        *,
        feed: DataFeed | None = None,
        config: BacktestConfig | None = None,
        wf_config: WalkForwardConfig | None = None,
    ) -> dict[str, Any]:
        """Execute experiment: runs Baseline and Candidate under IDENTICAL assumptions.

        Calculates:
        - Side-by-side metric comparison table
        - Equity and Drawdown curves
        - Monthly return matrix
        - Return distributions (skew, kurtosis, buckets)
        - Regime performance
        - Walk-forward out-of-sample validation
        - Parameter robustness analysis
        - Structured explanation: WHAT CHANGED, WHY PROPOSED, WHAT EXPERIMENT FOUND
        """
        with self.db.session() as session:
            exp = StrategyExperimentRepository.get(session, experiment_id)
            if not exp:
                raise LookupError(f"Experiment {experiment_id} not found")
            StrategyExperimentRepository.update_status(session, experiment_id, "RUNNING")

        eval_feed = feed or self._default_feed()
        bt_config = config or BacktestConfig(initial_cash=1_000_000.0)

        baseline_def = exp["baseline_definition"]
        candidate_def = exp["candidate_definition"]
        strategy_id = exp["strategy_id"]

        try:
            # 1. Evaluate Baseline Backtest
            base_res, base_m = evaluate_backtest(eval_feed, baseline_def, bt_config)

            # 2. Evaluate Candidate Backtest under exact identical assumptions
            cand_res, cand_m = evaluate_backtest(eval_feed, candidate_def, bt_config)

            # 3. Walk-Forward OOS Validation on candidate
            param_changes = exp.get("parameter_changes") or {}
            first_param = next(iter(param_changes.keys()), "param")
            prop_val = (
                param_changes[first_param].get("proposed")
                if isinstance(param_changes[first_param], dict)
                else param_changes[first_param]
            )

            wf_m = evaluate_walk_forward(
                eval_feed,
                baseline_def,
                first_param,
                float(prop_val),
                wf_config=wf_config,
                bt_config=bt_config,
            )

            # 4. Robustness scan
            adaptive_specs = extract_adaptive_parameters(baseline_def)
            param_spec = adaptive_specs.get(first_param)
            if param_spec:
                rob = evaluate_robustness(
                    eval_feed,
                    baseline_def,
                    param_spec,
                    float(prop_val),
                    config=bt_config,
                )
            else:
                rob = RobustnessResult(
                    parameter=first_param,
                    tested_values=[float(prop_val)],
                    metrics_by_value={str(prop_val): cand_m.to_dict()},
                    is_stable=True,
                    is_isolated_spike=False,
                    stable_range=(float(prop_val), float(prop_val)),
                    sensitivity_verdict="Single parameter point evaluated (unbounded)",
                )

            # 5. Build Side-by-Side Metric Comparison Table
            metric_keys = [
                ("total_return_pct", "Return (%)", "%", True),
                ("max_drawdown_pct", "Max Drawdown (%)", "%", False),
                ("sharpe", "Sharpe Ratio", "", True),
                ("sortino", "Sortino Ratio", "", True),
                ("profit_factor", "Profit Factor", "", True),
                ("win_rate_pct", "Win Rate (%)", "%", True),
                ("num_trades", "Trades Count", "", True),
                ("median_return", "Median Trade (%)", "%", True),
                ("cagr_pct", "CAGR (%)", "%", True),
            ]

            metrics_table = []
            for key, label, unit, higher_is_better in metric_keys:
                cur_val = getattr(base_m, key, None)
                cand_val = getattr(cand_m, key, None)
                diff = None
                if cur_val is not None and cand_val is not None:
                    diff = round(float(cand_val) - float(cur_val), 2)
                metrics_table.append({
                    "metric": key,
                    "label": label,
                    "unit": unit,
                    "current": cur_val,
                    "candidate": cand_val,
                    "difference": diff,
                    "higher_is_better": higher_is_better,
                })

            # Add OOS Efficiency metric
            metrics_table.append({
                "metric": "oos_efficiency",
                "label": "OOS Efficiency Ratio",
                "unit": "",
                "current": 1.0,
                "candidate": wf_m.efficiency_ratio,
                "difference": round(wf_m.efficiency_ratio - 1.0, 2),
                "higher_is_better": True,
            })

            # 6. Curves & Distributions
            base_equity = base_res.equity
            cand_equity = cand_res.equity

            equity_curves = {
                "baseline": _calc_curve_points(base_equity),
                "candidate": _calc_curve_points(cand_equity),
            }

            drawdown_curves = {
                "baseline": _calc_curve_points(_calc_drawdown_curve(base_equity)),
                "candidate": _calc_curve_points(_calc_drawdown_curve(cand_equity)),
            }

            monthly_returns = {
                "baseline": _calc_monthly_returns(base_equity),
                "candidate": _calc_monthly_returns(cand_equity),
            }

            return_distributions = {
                "baseline": _calc_return_distribution(base_res.trades),
                "candidate": _calc_return_distribution(cand_res.trades),
            }

            regime_performance = {
                "baseline": _calc_regime_performance(base_res.trades),
                "candidate": _calc_regime_performance(cand_res.trades),
            }

            # 7. Formulate Honest Structured Explanation
            # WHAT CHANGED
            changes_desc = [
                f"{p}: {v.get('current', '—')} → {v.get('proposed', v)}"
                if isinstance(v, dict)
                else f"{p}: → {v}"
                for p, v in param_changes.items()
            ]
            what_changed = ", ".join(changes_desc)

            # WHY IT WAS PROPOSED
            why_proposed = exp.get("reason") or "Identified via forward statistical pattern detection & research candidate."

            # WHAT THE EXPERIMENT FOUND
            what_found = (
                f"Backtest: PF {base_m.profit_factor:.2f} → {cand_m.profit_factor:.2f}, "
                f"Sharpe {base_m.sharpe:.2f} → {cand_m.sharpe:.2f}, MaxDD {base_m.max_drawdown_pct:.1f}% → {cand_m.max_drawdown_pct:.1f}%. "
                f"Walk-Forward OOS: {wf_m.verdict}. "
                f"Robustness: {rob.sensitivity_verdict}."
            )

            explanation = {
                "what_changed": what_changed,
                "why_proposed": why_proposed,
                "what_experiment_found": what_found,
            }

            results_payload = {
                "metrics_table": metrics_table,
                "baseline_metrics": base_m.to_dict(),
                "candidate_metrics": cand_m.to_dict(),
                "walk_forward_metrics": wf_m.to_dict(),
                "robustness_results": rob.to_dict(),
                "equity_curves": equity_curves,
                "drawdown_curves": drawdown_curves,
                "monthly_returns": monthly_returns,
                "return_distributions": return_distributions,
                "regime_performance": regime_performance,
            }

            with self.db.session() as session:
                updated = StrategyExperimentRepository.save_results(
                    session,
                    experiment_id,
                    results=results_payload,
                    explanation=explanation,
                    status="COMPLETED",
                )
            return updated or {}

        except Exception as exc:
            logger.exception("Experiment execution failed: {}", exc)
            with self.db.session() as session:
                StrategyExperimentRepository.update_status(
                    session,
                    experiment_id,
                    "FAILED",
                    error=str(exc),
                )
            raise

    def approve_experiment(self, experiment_id: str, user_id: str) -> dict[str, Any]:
        """Operator explicitly marks an experiment as APPROVED."""
        with self.db.session() as session:
            exp = StrategyExperimentRepository.get(session, experiment_id)
            if not exp:
                raise LookupError(f"Experiment {experiment_id} not found")
            if exp["status"] not in ("COMPLETED", "REJECTED"):
                raise ValueError(f"Cannot approve experiment with status {exp['status']}")

            res = StrategyExperimentRepository.update_status(
                session,
                experiment_id,
                "APPROVED",
                reviewed_by=user_id,
            )
            return res or {}

    def reject_experiment(self, experiment_id: str, user_id: str, reason: str | None = None) -> dict[str, Any]:
        """Operator rejects an experiment."""
        with self.db.session() as session:
            exp = StrategyExperimentRepository.get(session, experiment_id)
            if not exp:
                raise LookupError(f"Experiment {experiment_id} not found")
            if exp["status"] == "APPLIED":
                raise ValueError("Cannot reject an already applied experiment")

            res = StrategyExperimentRepository.update_status(
                session,
                experiment_id,
                "REJECTED",
                reviewed_by=user_id,
                rejection_reason=reason or "Operator rejected experimental candidate.",
            )
            return res or {}

    def apply_experiment(self, experiment_id: str, user_id: str) -> dict[str, Any]:
        """Apply approved experiment to create immutable version V(N+1).

        Source version V(N) remains strictly untouched.
        """
        with self.db.session() as session:
            return StrategyExperimentRepository.apply(session, experiment_id, author_user_id=user_id)

    def list_experiments(
        self,
        strategy_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List experiments for auditing and history."""
        with self.db.session() as session:
            return StrategyExperimentRepository.list_for_strategy(
                session,
                strategy_id=strategy_id,
                status=status,
                limit=limit,
            )

    def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        """Fetch detailed experiment report."""
        with self.db.session() as session:
            return StrategyExperimentRepository.get(session, experiment_id)
