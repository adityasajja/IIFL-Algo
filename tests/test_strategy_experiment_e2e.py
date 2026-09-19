"""End-to-End Acceptance Test for the Strategy Experiment Lab.

Validates the full progression:
1. Forward evidence (genuine PAPER_FORWARD trades in learning dataset)
2. Learning observation generation across trading axes
3. Research hypothesis & optimization recommendation
4. Strategy experiment creation from recommendation or manual parameter change
5. Experiment execution:
   - Identical dates, universe, capital, slippage, and costs for baseline and candidate
   - Backtest execution
   - Walk-forward out-of-sample evaluation
   - Robustness parameter sensitivity scan
6. Side-by-side results:
   - 9 metrics table: Return, Max DD, Sharpe, Sortino, Profit Factor, Win Rate, Trades, Median Trade, OOS Efficiency
   - Equity curves & Drawdown curves
   - Monthly returns matrix
   - Trade return distributions
   - Regime performance
7. Derived, non-hallucinated structured explanation:
   - WHAT CHANGED
   - WHY IT WAS PROPOSED
   - WHAT THE EXPERIMENT FOUND
8. Approval workflow:
   - User Review → Approve → Apply & Create Immutable V(N+1)
   - Or Reject with audit rationale
9. Safety guarantees:
   - V(N) remains strictly untouched
   - Zero orders placed
   - Zero automatic deployments
   - Full experiment history persisted in database
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from atr.appdb.engine import AppDatabase
from atr.appdb.repositories import (
    LearningObservationRepository,
    OptimizationRecommendationRepository,
    StrategyExperimentRepository,
    StrategyRepository,
    UserRepository,
)
from atr.data.synthetic import SyntheticConfig, SyntheticFeed
from atr.research.learning_evidence import CLASS_PAPER_FORWARD, GRADE_FORWARD
from atr.services.experiment import StrategyExperimentService
from atr.services.learning import LearningDataset, LearningService
from atr.services.optimization import OptimizationService

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def test_user(app_db: AppDatabase) -> dict[str, Any]:
    with app_db.session() as session:
        user = UserRepository.create(
            session,
            username="lab_researcher",
            email="lab@example.com",
            role="trader",
            password_hash="test_password_hash",
        )
        return dict(user)


@pytest.fixture
def test_strategy(app_db: AppDatabase, test_user: dict[str, Any]) -> dict[str, Any]:
    strat_id = "strat-lab-breakout"
    definition = {
        "rules": {
            "entry": {
                "volume_multiple": 1.5,
                "breakout_lookback": 20,
            },
            "exit": {
                "stop_loss_pct": 2.0,
                "take_profit_pct": 5.0,
            },
        },
        "adaptive_parameters": [
            {
                "name": "volume_multiple",
                "current": 1.5,
                "minimum": 1.2,
                "maximum": 3.0,
                "step": 0.1,
                "adaptive": True,
                "block": "entry",
            }
        ],
    }

    with app_db.session() as session:
        strat = StrategyRepository.create(
            session,
            user_id=test_user["user_id"],
            name="Experiment Lab Test Strategy",
            kind="rules",
        )
        strat_id = strat["strategy_id"]
        StrategyRepository.create_version(
            session,
            strategy_id=strat_id,
            author_user_id=test_user["user_id"],
            definition=definition,
            change_note="Initial V1 for Experiment Lab",
        )
        saved = StrategyRepository.get(session, strat_id, test_user["user_id"])
        assert saved is not None
        return dict(saved)


@pytest.fixture
def feed():
    """Consistent synthetic feed for deterministic test evaluations."""
    return SyntheticFeed(
        SyntheticConfig(
            symbols=("NIFTY50",),
            start=datetime(2024, 1, 1, 9, 30),
            end=datetime(2024, 6, 1, 15, 30),
            freq="1D",
            seed=42,
        )
    )


class MockLearningService(LearningService):
    """Mock learning service returning forward trades for deterministic testing."""

    def __init__(self, trades: list[dict[str, Any]]) -> None:
        self._mock_trades = trades

    def dataset(self, refresh: bool = False) -> LearningDataset:
        return LearningDataset(
            rows=self._mock_trades,
            missing_features={},
            generated_at=datetime.now().isoformat(),
        )


def test_strategy_experiment_lab_full_lifecycle(
    app_db: AppDatabase,
    test_user: dict[str, Any],
    test_strategy: dict[str, Any],
    feed: SyntheticFeed,
):
    """End-to-end verification of Strategy Experiment Lab.

    Forward evidence → learning observation → hypothesis → recommendation →
    experiment creation → identical backtests → walk-forward → robustness →
    results table & curves → structured explanation → approval → immutable V(N+1).
    """
    strategy_id = test_strategy["strategy_id"]

    # -----------------------------------------------------------------------
    # Step 1: Forward evidence (PAPER_FORWARD trades)
    # -----------------------------------------------------------------------
    forward_trades = []
    base_time = datetime(2024, 4, 1, 10, 0, tzinfo=IST)
    for i in range(25):
        # 15 trades with high RVOL (positive return), 10 trades with lower RVOL
        rvol = 2.2 if i < 15 else 1.2
        ret_pct = 2.8 if i < 15 else -0.8
        forward_trades.append({
            "trade_id": f"pforward-lab-{i:03d}",
            "symbol": "NIFTY50",
            "entry_time": (base_time + timedelta(days=i)).isoformat(),
            "exit_time": (base_time + timedelta(days=i, hours=2)).isoformat(),
            "side": "BUY",
            "quantity": 50,
            "entry_price": 22000.0,
            "exit_price": 22000.0 * (1.0 + ret_pct / 100.0),
            "pnl": 50 * 22000.0 * (ret_pct / 100.0),
            "pnl_pct": ret_pct,
            "strategy_id": strategy_id,
            "strategy_version": 1,
            "evidence_class": CLASS_PAPER_FORWARD,
            "evidence_grade": GRADE_FORWARD,
            "relative_volume": rvol,
            "market_regime": "BULL_TRENDING" if i % 2 == 0 else "RANGING",
        })

    learning_svc = MockLearningService(trades=forward_trades)
    assert len(learning_svc.dataset().rows) == 25
    opt_svc = OptimizationService(db=app_db, learning_service=learning_svc)
    exp_svc = StrategyExperimentService(db=app_db)

    # -----------------------------------------------------------------------
    # Step 2: Learning Observation & Hypothesis
    # -----------------------------------------------------------------------
    with app_db.session() as session:
        obs_row = LearningObservationRepository.record(
            session,
            strategy_id=strategy_id,
            strategy_version=1,
            date="2024-04-25",
            metric="relative_volume",
            condition_bucket="RVOL >= 2.0x",
            sample_size=25,
            statistical_result={
                "mean_return_lift": 3.6,
                "hypothesis": "Increasing volume_multiple threshold from 1.5 to 1.8 filters false breakouts.",
                "parameter_hint": "volume_multiple",
                "suggested_value": 1.8,
            },
            evidence_class=CLASS_PAPER_FORWARD,
            confidence=0.95,
        )
        obs_id = obs_row["observation_id"]

    # -----------------------------------------------------------------------
    # Step 3: Optimization Recommendation
    # -----------------------------------------------------------------------
    rec_id = "rec-lab-test-01"
    with app_db.session() as session:
        rec_row = OptimizationRecommendationRepository.create(
            session,
            recommendation_id=rec_id,
            strategy_id=strategy_id,
            source_strategy_version=1,
            parameter="volume_multiple",
            current_value=1.5,
            proposed_value=1.8,
            reason="84 forward trades observed with RVOL >= 2.0x showed mean return diff +0.82%",
            source_observations={"observation_id": obs_id, "statement": "RVOL > 2.0x lift +0.82%"},
            sample_size=25,
            baseline_metrics={"sharpe": 1.15, "profit_factor": 1.42, "max_drawdown_pct": 14.5},
            candidate_metrics={"sharpe": 1.48, "profit_factor": 1.61, "max_drawdown_pct": 11.2},
            walk_forward_metrics={"passed": True, "efficiency_ratio": 0.88, "out_of_sample_profit_factor": 1.52},
            robustness_results={"is_stable": True, "stable_range": [1.7, 2.0]},
            confidence="high",
            status="RECOMMENDED",
        )
        assert rec_row is not None
        assert rec_row["parameter"] == "volume_multiple"

    # -----------------------------------------------------------------------
    # Step 4: Create Experiment from Recommendation
    # -----------------------------------------------------------------------
    exp = exp_svc.create_experiment(
        strategy_id=strategy_id,
        recommendation_id=rec_id,
        creator_user_id=test_user["user_id"],
        reason="Test RVOL parameter change against current strategy baseline",
    )

    assert exp["experiment_id"] is not None
    assert len(exp["experiment_id"]) >= 8
    assert exp["strategy_id"] == strategy_id
    assert exp["source_version"] == 1
    assert exp["status"] == "CREATED"
    assert exp["creator_user_id"] == test_user["user_id"]
    assert "volume_multiple" in exp["parameter_changes"]
    assert exp["target_version"] is None

    # Verify baseline & candidate definitions are stored
    assert exp["baseline_definition"]["rules"]["entry"]["volume_multiple"] == 1.5
    assert exp["candidate_definition"]["rules"]["entry"]["volume_multiple"] != 1.5

    # -----------------------------------------------------------------------
    # Step 5: Execute Experiment (Identical Backtest, Walk-Forward, Robustness)
    # -----------------------------------------------------------------------
    run_res = exp_svc.run_experiment(exp["experiment_id"], feed=feed)
    assert run_res["status"] == "COMPLETED"
    results = run_res["results"]
    assert results is not None

    # Step 5a: Verify Side-by-Side 9 Metrics Table
    metrics_table = results["metrics_table"]
    metric_names = [m["metric"] for m in metrics_table]
    assert "total_return_pct" in metric_names
    assert "max_drawdown_pct" in metric_names
    assert "sharpe" in metric_names
    assert "sortino" in metric_names
    assert "profit_factor" in metric_names
    assert "win_rate_pct" in metric_names
    assert "num_trades" in metric_names
    assert "median_return" in metric_names
    assert "oos_efficiency" in metric_names

    # Check differences are calculated
    for row in metrics_table:
        assert "current" in row
        assert "candidate" in row
        assert "difference" in row
        if row["current"] is not None and row["candidate"] is not None:
            expected_diff = round(row["candidate"] - row["current"], 4)
            assert abs(row["difference"] - expected_diff) < 1e-3

    # Step 5b: Verify Curves
    assert "equity_curves" in results
    assert "baseline" in results["equity_curves"]
    assert "candidate" in results["equity_curves"]
    assert len(results["equity_curves"]["baseline"]) > 0

    assert "drawdown_curves" in results
    assert len(results["drawdown_curves"]["candidate"]) > 0

    # Step 5c: Verify Monthly Returns Matrix
    assert "monthly_returns" in results
    assert "candidate" in results["monthly_returns"]

    # Step 5d: Verify Return Distribution
    assert "return_distributions" in results
    cand_dist = results["return_distributions"]["candidate"]
    assert "mean" in cand_dist
    assert "median" in cand_dist
    assert "buckets" in cand_dist

    # Step 5e: Verify Walk-Forward & Robustness
    assert "walk_forward_metrics" in results
    assert results["walk_forward_metrics"]["efficiency_ratio"] is not None
    assert "robustness_results" in results
    assert "is_stable" in results["robustness_results"]

    # -----------------------------------------------------------------------
    # Step 6: Verify Structured Explanation (Derived, Non-Hallucinated)
    # -----------------------------------------------------------------------
    explanation = run_res["explanation"]
    assert explanation is not None
    assert "volume_multiple" in explanation["what_changed"]
    assert len(explanation["why_proposed"]) > 0
    assert "Backtest:" in explanation["what_experiment_found"]
    assert "Walk-Forward" in explanation["what_experiment_found"]
    assert "Robustness:" in explanation["what_experiment_found"]

    # -----------------------------------------------------------------------
    # Step 7: Rejection Workflow Test
    # -----------------------------------------------------------------------
    # Create a secondary experiment to test rejection
    exp_reject = exp_svc.create_experiment(
        strategy_id=strategy_id,
        parameter_changes={"volume_multiple": 2.9},
        creator_user_id=test_user["user_id"],
        reason="Extreme RVOL parameter test",
    )
    exp_svc.run_experiment(exp_reject["experiment_id"], feed=feed)
    rejected = exp_svc.reject_experiment(
        exp_reject["experiment_id"],
        user_id=test_user["user_id"],
        reason="Parameter 2.9 is outside acceptable liquidity tolerance",
    )
    assert rejected["status"] == "REJECTED"
    assert "outside acceptable liquidity" in rejected["rejection_reason"]

    # -----------------------------------------------------------------------
    # Step 8: Approval Workflow Test (Review → Approve → Create Immutable V(N+1))
    # -----------------------------------------------------------------------
    # Attempting to apply before approval should fail
    with pytest.raises(ValueError, match="must be APPROVED"):
        exp_svc.apply_experiment(exp["experiment_id"], user_id=test_user["user_id"])

    approved = exp_svc.approve_experiment(exp["experiment_id"], user_id=test_user["user_id"])
    assert approved["status"] == "APPROVED"
    assert approved["reviewed_by"] == test_user["user_id"]

    apply_res = exp_svc.apply_experiment(
        exp["experiment_id"], user_id=test_user["user_id"]
    )
    final_exp = apply_res["experiment"]
    new_version = apply_res["new_version"]
    assert final_exp["status"] == "APPLIED"
    assert final_exp["target_version"] == 2
    assert new_version["version"] == 2
    assert new_version["strategy_id"] == strategy_id

    # -----------------------------------------------------------------------
    # Step 9: Verify Strict Safety Guarantees
    # -----------------------------------------------------------------------
    with app_db.session() as session:
        # V(1) must remain strictly UNCHANGED
        v1 = StrategyRepository.get_version(session, strategy_id, 1)
        assert v1 is not None
        v1_def = json.loads(v1["definition"]) if isinstance(v1["definition"], str) else v1["definition"]
        assert v1_def["rules"]["entry"]["volume_multiple"] == 1.5

        # V(2) contains the approved candidate parameter
        v2 = StrategyRepository.get_version(session, strategy_id, 2)
        assert v2 is not None
        v2_def = json.loads(v2["definition"]) if isinstance(v2["definition"], str) else v2["definition"]
        assert v2_def["rules"]["entry"]["volume_multiple"] != 1.5
        assert f"experiment {exp['experiment_id']}" in (v2["change_note"] or "")

        # Verify experiment history is stored and retrievable
        exp_list = exp_svc.list_experiments(strategy_id)
        exp_ids = [e["experiment_id"] for e in exp_list]
        assert exp["experiment_id"] in exp_ids
        assert exp_reject["experiment_id"] in exp_ids

        # Verify NO orders were placed
        orders = session.execute(text("SELECT count(*) FROM orders")).scalar()
        assert orders == 0

        # Verify NO live deployments occurred
        deployments_count = session.execute(text("SELECT count(*) FROM deployments")).scalar()
        assert deployments_count == 0
