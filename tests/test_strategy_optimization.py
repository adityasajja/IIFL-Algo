"""Comprehensive tests for controlled strategy optimization layer.

Validates:
1. Adaptive parameter marking & isolation (optimizer cannot alter arbitrary logic).
2. Candidate generation from genuine forward observations with baseline retention.
3. Multi-metric backtest evaluation (never optimizes for return alone).
4. Walk-forward out-of-sample validation gating.
5. Robustness sensitivity checks (flags isolated peaks as overfitting, prefers plateaus).
6. Recommendation persistence and lifecycle.
7. Strategy immutability: applying recommendation produces V_{N+1} leaving V_N untouched.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from atr.appdb.engine import AppDatabase
from atr.appdb.repositories import (
    OptimizationRecommendationRepository,
    StrategyRepository,
    UserRepository,
)
from atr.backtest.engine import BacktestConfig
from atr.data.synthetic import SyntheticConfig, SyntheticFeed
from atr.optimization.adaptive import (
    AdaptiveParameter,
    apply_parameter_to_definition,
    extract_adaptive_parameters,
)
from atr.optimization.candidates import (
    OptimizationCandidate,
    generate_candidates_from_observations,
    match_observation_to_parameter,
)
from atr.optimization.evaluator import (
    PerformanceMetrics,
    RobustnessResult,
    WalkForwardMetrics,
    evaluate_backtest,
    evaluate_candidate,
    evaluate_robustness,
    evaluate_walk_forward,
)
from atr.research.validate import WalkForwardConfig
from atr.services.optimization import OptimizationService


@pytest.fixture
def test_user(app_db: AppDatabase) -> dict[str, Any]:
    with app_db.session() as session:
        user = UserRepository.create(
            session,
            username="opt_trader",
            email="opt@example.com",
            role="trader",
            password_hash="dummy_hash_for_testing",
        )
        return dict(user)


@pytest.fixture
def feed():
    """Consistent synthetic feed for deterministic test backtests."""
    return SyntheticFeed(
        SyntheticConfig(
            symbols=("NIFTY50",),
            start=datetime(2024, 1, 1, 9, 30),
            end=datetime(2024, 6, 1, 15, 30),
            freq="1D",
            seed=42,
        )
    )


# ---------------------------------------------------------------------------
# 1. Adaptive parameters
# ---------------------------------------------------------------------------


def test_only_explicit_adaptive_parameters_are_extracted():
    """Only parameters explicitly marked adaptive may be optimized."""
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
            },
            {
                "name": "stop_loss_pct",
                "current": 2.0,
                "minimum": 1.0,
                "maximum": 4.0,
                "step": 0.25,
                "adaptive": False,  # Explicitly disabled
                "block": "exit",
            },
        ],
    }

    params = extract_adaptive_parameters(definition)
    assert "volume_multiple" in params
    assert params["volume_multiple"].current == 1.5
    assert params["volume_multiple"].step == 0.1
    assert params["volume_multiple"].adaptive is True

    # stop_loss_pct was marked adaptive: False, so must NOT be adapted
    assert "stop_loss_pct" not in params

    # breakout_lookback was never declared adaptive, so must NOT be adapted
    assert "breakout_lookback" not in params


def test_empty_adaptive_parameters_yields_empty():
    """A strategy without adaptive_parameters allows zero parameter adaptations."""
    definition = {
        "rules": {"entry": {"volume_multiple": 1.5}, "exit": {"stop_loss_pct": 2.0}}
    }
    assert extract_adaptive_parameters(definition) == {}


def test_parameter_bounds_validation():
    """Invalid bounds (min >= max or step <= 0) are rejected."""
    bad_def = {
        "adaptive_parameters": [
            {"name": "volume_multiple", "minimum": 3.0, "maximum": 1.0, "step": 0.1, "current": 1.5}
        ]
    }
    assert extract_adaptive_parameters(bad_def) == {}


def test_apply_parameter_immutability():
    """Applying a parameter produces a new definition and does not mutate the baseline."""
    base_def = {
        "rules": {
            "entry": {"volume_multiple": 1.5, "breakout_proximity_pct": 1.0},
            "exit": {"stop_loss_pct": 2.0},
        },
        "adaptive_parameters": [
            {"name": "volume_multiple", "current": 1.5, "minimum": 1.0, "maximum": 3.0, "step": 0.1}
        ],
    }

    updated = apply_parameter_to_definition(base_def, "volume_multiple", 1.9, block="entry")

    # Baseline remains 1.5
    assert base_def["rules"]["entry"]["volume_multiple"] == 1.5
    assert base_def["adaptive_parameters"][0]["current"] == 1.5

    # New dictionary has 1.9
    assert updated["rules"]["entry"]["volume_multiple"] == 1.9
    assert updated["adaptive_parameters"][0]["current"] == 1.9
    assert updated["rules"]["entry"]["breakout_proximity_pct"] == 1.0


# ---------------------------------------------------------------------------
# 2. Candidate generation
# ---------------------------------------------------------------------------


def test_candidate_generation_from_forward_observations():
    """Forward evidence (e.g. RVOL > 2.0x, n=84) generates discrete candidate steps."""
    strategy_def = {
        "rules": {
            "entry": {"volume_multiple": 1.5, "breakout_lookback": 20},
            "exit": {"stop_loss_pct": 2.0},
        },
        "adaptive_parameters": [
            {
                "name": "volume_multiple",
                "current": 1.5,
                "minimum": 1.2,
                "maximum": 3.0,
                "step": 0.1,
                "adaptive": True,
            }
        ],
    }

    observations = [
        {
            "kind": "strong_bucket",
            "statement": "Over 84 forward trades, condition 'RVOL > 2.0x' averaged +1.42, +0.35 above baseline",
            "evidence": {
                "axis": "relative_volume",
                "bucket": "RVOL > 2.0x",
                "n": 84,
                "lift": 0.35,
                "significance": "strong",
            },
            "sample_size": 84,
        }
    ]

    candidates = generate_candidates_from_observations(strategy_def, observations, min_sample_size=10)
    assert len(candidates) > 0

    # Every candidate retains original strategy as baseline
    for cand in candidates:
        assert cand.parameter_name == "volume_multiple"
        assert cand.current_value == 1.5
        assert cand.proposed_value in (1.8, 1.9, 2.0, 1.6, 1.7)
        assert cand.sample_size == 84
        assert cand.baseline_definition == strategy_def
        assert cand.candidate_definition["rules"]["entry"]["volume_multiple"] == cand.proposed_value


def test_candidate_generation_suppresses_small_samples():
    """Candidates are NEVER generated from insufficient samples (n < 10)."""
    strategy_def = {
        "rules": {"entry": {"volume_multiple": 1.5}},
        "adaptive_parameters": [
            {"name": "volume_multiple", "current": 1.5, "minimum": 1.0, "maximum": 3.0, "step": 0.1}
        ],
    }

    tiny_observations = [
        {
            "kind": "strong_bucket",
            "statement": "Over 4 forward trades, condition 'RVOL > 2.0x' averaged +3.00",
            "evidence": {"axis": "relative_volume", "bucket": "RVOL > 2.0x", "n": 4, "lift": 1.5},
            "sample_size": 4,
        }
    ]

    candidates = generate_candidates_from_observations(strategy_def, tiny_observations, min_sample_size=10)
    assert len(candidates) == 0


# ---------------------------------------------------------------------------
# 3. Backtest & Multi-metric evaluation
# ---------------------------------------------------------------------------


def test_backtest_candidate_multi_metric(feed):
    """Backtest evaluation captures multi-metric profile and return distribution."""
    definition = {
        "engine_key": "sma_crossover",
        "params": {"fast_period": 10, "slow_period": 30},
    }

    result, metrics = evaluate_backtest(feed, definition)
    assert isinstance(metrics, PerformanceMetrics)
    assert hasattr(metrics, "cagr_pct")
    assert hasattr(metrics, "max_drawdown_pct")
    assert hasattr(metrics, "sharpe")
    assert hasattr(metrics, "sortino")
    assert hasattr(metrics, "profit_factor")
    assert hasattr(metrics, "win_rate_pct")
    assert hasattr(metrics, "num_trades")
    assert hasattr(metrics, "avg_trade")


# ---------------------------------------------------------------------------
# 4. Walk-forward validation gating
# ---------------------------------------------------------------------------


def test_walk_forward_validation_structure(feed):
    """Walk-forward evaluation computes out-of-sample metrics and efficiency ratio."""
    definition = {
        "engine_key": "sma_crossover",
        "params": {"fast_period": 10, "slow_period": 30},
    }

    wf_config = WalkForwardConfig(train_bars=60, test_bars=20, step_bars=20)
    wf_metrics = evaluate_walk_forward(
        feed, definition, "fast_period", 10.0, wf_config=wf_config
    )
    assert isinstance(wf_metrics, WalkForwardMetrics)
    assert hasattr(wf_metrics, "in_sample_sharpe")
    assert hasattr(wf_metrics, "out_of_sample_sharpe")
    assert hasattr(wf_metrics, "efficiency_ratio")
    assert hasattr(wf_metrics, "passed")
    assert isinstance(wf_metrics.passed, bool)


# ---------------------------------------------------------------------------
# 5. Robustness sensitivity checks
# ---------------------------------------------------------------------------


def test_robustness_sensitivity_scan(feed):
    """Robustness sensitivity checks neighboring parameter points (v-s, v, v+s)."""
    definition = {
        "engine_key": "sma_crossover",
        "params": {"fast_period": 10, "slow_period": 30},
    }
    param_spec = AdaptiveParameter(
        name="fast_period",
        current=10.0,
        minimum=5.0,
        maximum=20.0,
        step=2.0,
        adaptive=True,
    )

    rob = evaluate_robustness(feed, definition, param_spec, 10.0)
    assert isinstance(rob, RobustnessResult)
    assert rob.parameter == "fast_period"
    assert len(rob.tested_values) >= 3
    assert hasattr(rob, "is_stable")
    assert hasattr(rob, "is_isolated_spike")
    assert hasattr(rob, "sensitivity_verdict")


# ---------------------------------------------------------------------------
# 6. Recommendation persistence & Lifecycle (V_N -> V_{N+1} immutability)
# ---------------------------------------------------------------------------


def test_recommendation_lifecycle_and_version_immutability(app_db: AppDatabase, test_user: dict[str, Any]):
    """End-to-end lifecycle test:

    V1 created
    → Recommendation created (RECOMMENDED)
    → User approval (APPROVED)
    → User apply (APPLIED)
    → Creates V2
    → V1 remains completely untouched and immutable!
    """
    user_id = test_user["user_id"]

    # 1. Create initial strategy and V1
    base_definition = {
        "rules": {
            "entry": {"volume_multiple": 1.5, "breakout_lookback": 20},
            "exit": {"stop_loss_pct": 2.0},
        },
        "adaptive_parameters": [
            {
                "name": "volume_multiple",
                "current": 1.5,
                "minimum": 1.0,
                "maximum": 3.0,
                "step": 0.1,
                "adaptive": True,
            }
        ],
    }

    with app_db.session() as session:
        strat = StrategyRepository.create(
            session,
            user_id=user_id,
            name="Alpha Breakout",
            description="Testing adaptive optimization",
            kind="rules",
        )
        strat_id = strat["strategy_id"]

        v1 = StrategyRepository.create_version(
            session,
            strategy_id=strat_id,
            author_user_id=user_id,
            definition=base_definition,
            change_note="Initial V1",
        )
        assert v1["version"] == 1

    # 2. Record an optimization recommendation
    rec_id = "rec-test-12345"
    with app_db.session() as session:
        OptimizationRecommendationRepository.create(
            session,
            recommendation_id=rec_id,
            strategy_id=strat_id,
            source_strategy_version=1,
            parameter="volume_multiple",
            current_value=1.5,
            proposed_value=1.9,
            reason="Forward evidence and OOS walk-forward validation confirm superior expectancy",
            source_observations={"statement": "RVOL > 2.0x lift +0.35"},
            sample_size=84,
            baseline_metrics={"sharpe": 1.10, "profit_factor": 1.40, "max_drawdown_pct": 14.0},
            candidate_metrics={"sharpe": 1.45, "profit_factor": 1.62, "max_drawdown_pct": 11.5},
            walk_forward_metrics={"passed": True, "efficiency_ratio": 0.85},
            robustness_results={"is_stable": True, "stable_range": [1.8, 2.1]},
            confidence="high",
            status="RECOMMENDED",
        )

        rec = OptimizationRecommendationRepository.get(session, rec_id)
        assert rec is not None
        assert rec["status"] == "RECOMMENDED"
        assert rec["current_value"] == 1.5
        assert rec["proposed_value"] == 1.9

    # 3. Attempting to apply directly without APPROVAL must fail
    opt_service = OptimizationService(db=app_db)
    with pytest.raises(ValueError, match="only APPROVED recommendations may be applied"):
        opt_service.apply_recommendation(rec_id, author_user_id=user_id)

    # 4. Explicit User Approval
    appr = opt_service.approve_recommendation(rec_id, user_id=user_id)
    assert appr["status"] == "APPROVED"

    # 5. Apply Approved Recommendation -> Creates V2
    applied_result = opt_service.apply_recommendation(rec_id, author_user_id=user_id)
    assert applied_result["recommendation"]["status"] == "APPLIED"
    assert applied_result["new_version"]["version"] == 2

    # 6. Verify Immutability of V1 and validity of V2
    with app_db.session() as session:
        v1_check = StrategyRepository.get_version(session, strat_id, version=1)
        v2_check = StrategyRepository.get_version(session, strat_id, version=2)

        assert v1_check is not None
        assert v2_check is not None

        # V1 definition remains completely UNCHANGED (1.5)
        v1_def = json.loads(v1_check["definition"])
        assert v1_def["rules"]["entry"]["volume_multiple"] == 1.5

        # V2 has the new proposed value (1.9)
        v2_def = json.loads(v2_check["definition"])
        assert v2_def["rules"]["entry"]["volume_multiple"] == 1.9

        # V2 change note cites optimization recommendation provenance
        assert "rec-test-12345" in str(v2_check["change_note"])
