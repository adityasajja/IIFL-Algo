"""Acceptance tests for the Automated Daily Learning Cycle.

Verifies end-to-end:
New PAPER_FORWARD trades
→ Learning Cycle
→ Forward Analysis across declared axes
→ Observation Generation (with n >= 10 & significance safeguards)
→ Research Hypothesis Generation
→ Optimization Candidate Generation
→ Backtest & Walk-forward Evaluation
→ Optimization Recommendation (remains PROPOSED/RECOMMENDED)
→ Safe exit with full explanations on insufficient data (n < 10 or zero trades)
→ NO automatic modification of strategy versions or live deployment.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from atr.appdb.engine import AppDatabase
from atr.appdb.repositories import (
    LearningObservationRepository,
    OptimizationRecommendationRepository,
    StrategyRepository,
    SystemStateRepository,
    UserRepository,
)
from atr.data.synthetic import SyntheticConfig, SyntheticFeed
from atr.research.learning_evidence import (
    CLASS_BACKTEST,
    CLASS_PAPER_FORWARD,
    GRADE_FORWARD,
    GRADE_IN_SAMPLE,
)
from atr.services.daily_learning import DailyLearningCycleService, KEY_LAST_CYCLE
from atr.services.learning import LearningDataset, LearningService
from atr.services.optimization import OptimizationService

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def test_user(app_db: AppDatabase) -> dict[str, Any]:
    with app_db.session() as session:
        user = UserRepository.create(
            session,
            username="cycle_tester",
            email="cycle@example.com",
            role="trader",
            password_hash="test_pw_hash",
        )
        return dict(user)


@pytest.fixture
def test_strategy(app_db: AppDatabase, test_user: dict[str, Any]) -> dict[str, Any]:
    strat_id = "strat-cycle-breakout"
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
            name="Daily Cycle Test Strategy",
            kind="rules",
        )
        strat_id = strat["strategy_id"]
        StrategyRepository.create_version(
            session,
            strategy_id=strat_id,
            author_user_id=test_user["user_id"],
            definition=definition,
            change_note="Initial V1",
        )
        return strat




@pytest.fixture
def feed():
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
    """Mock learning service returning controlled trades for deterministic cycle testing."""

    def __init__(self, trades: list[dict[str, Any]]) -> None:
        self._mock_trades = trades

    def dataset(self, refresh: bool = False) -> LearningDataset:
        return LearningDataset(
            rows=self._mock_trades,
            missing_features={},
            generated_at=datetime.now().isoformat(),
        )


def _generate_synthetic_trades(
    strategy_id: str,
    count: int,
    source: str = "PAPER",
    evidence_class: str = CLASS_PAPER_FORWARD,
    evidence_grade: str = GRADE_FORWARD,
    high_rvol: bool = False,
) -> list[dict[str, Any]]:
    trades = []
    base_time = datetime(2026, 9, 10, 10, 0, tzinfo=IST)
    for i in range(count):
        t_time = base_time + timedelta(hours=i)
        exit_t = t_time + timedelta(minutes=45)
        is_high = high_rvol and (i % 2 == 0)
        rvol = 2.5 if is_high else 1.2
        rvol_b = "2.0-3.0" if is_high else "1.0-1.5"
        ret = 2.5 if is_high else -0.5
        pnl = 250.0 if is_high else -50.0
        trades.append(
            {
                "trade_id": f"trade_{source}_{i}",
                "trade_ref": f"ref_{source}_{i}",
                "strategy_id": strategy_id,
                "strategy_key": strategy_id,
                "strategy_version": 1,
                "symbol": "NIFTY50",
                "source": source,
                "evidence_class": evidence_class,
                "evidence_grade": evidence_grade,
                "entry_time": t_time.isoformat(),
                "exit_time": exit_t.isoformat(),
                "entry_ts": t_time,
                "exit_ts": exit_t,
                "pnl": pnl,
                "net_pnl": pnl,
                "net_return": ret,
                "return_pct": ret,
                "market_regime": "bull_quiet",
                "day_of_week": "Tuesday",
                "time_of_day": "morning",
                "rsi": 58.0,
                "atr": 18.5,
                "relative_volume": rvol,
                "rvol_bucket": rvol_b,
                "gap": 0.3,
                "trend": "up",
                "sector": "FINANCIAL_SERVICES",
                "exit_reason": "take_profit" if ret > 0 else "stop_loss",
            }
        )
    return trades


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


def test_empty_trades_completes_safely(app_db: AppDatabase, test_strategy: dict[str, Any]):
    """If no forward trades exist, cycle exits safely with NO_DATA and explanation."""
    mock_learning = MockLearningService(trades=[])
    cycle_service = DailyLearningCycleService(db=app_db, learning_service=mock_learning)

    report = cycle_service.run_cycle(strategy_id=test_strategy["strategy_id"])
    assert report.status == "NO_DATA"
    assert report.trades_processed == 0
    assert report.observations_generated == 0
    assert report.hypotheses_generated == 0
    assert report.recommendations_generated == 0
    assert any("No genuine PAPER_FORWARD trades" in note for note in report.notes)

    # Durable system_state check
    latest = cycle_service.get_latest_cycle()
    assert latest is not None
    assert latest["cycle_id"] == report.cycle_id
    assert latest["status"] == "NO_DATA"


def test_insufficient_samples_withheld(app_db: AppDatabase, test_strategy: dict[str, Any]):
    """If n < 10 trades exist, strategy is flagged as insufficient and withheld from candidate generation."""
    # Only 5 trades
    trades = _generate_synthetic_trades(test_strategy["strategy_id"], count=5)
    mock_learning = MockLearningService(trades=trades)
    cycle_service = DailyLearningCycleService(db=app_db, learning_service=mock_learning)

    report = cycle_service.run_cycle(strategy_id=test_strategy["strategy_id"], min_sample_size=10)
    assert report.status == "INSUFFICIENT_SAMPLE"
    assert report.trades_processed == 5
    assert test_strategy["strategy_id"] in report.insufficient_data_strategies
    assert report.hypotheses_generated == 0
    assert report.recommendations_generated == 0
    assert any("requires min 10" in note for note in report.notes)


def test_end_to_end_daily_learning_cycle(
    app_db: AppDatabase,
    test_strategy: dict[str, Any],
    feed: SyntheticFeed,
):
    """Full end-to-end automated daily learning cycle verification:

    PAPER_FORWARD trades (n=30)
    → Learning cycle collects them
    → Runs forward analysis
    → Identifies significant pattern & records observation
    → Derives hypothesis statement
    → Generates optimization candidate for adaptive volume_multiple
    → Evaluates candidate via backtest, walk-forward, robustness
    → Persists recommendation in PROPOSED or RECOMMENDED status
    → Asserts strategy version remains V1 unchanged (no live deployment).
    """
    strat_id = test_strategy["strategy_id"]


    # 1. Provide backtest baseline + genuine paper forward trades (n=30)
    backtest_trades = _generate_synthetic_trades(
        strat_id,
        count=30,
        source="BACKTEST",
        evidence_class=CLASS_BACKTEST,
        evidence_grade=GRADE_IN_SAMPLE,
        high_rvol=False,
    )
    forward_trades = _generate_synthetic_trades(
        strat_id,
        count=30,
        source="PAPER",
        evidence_class=CLASS_PAPER_FORWARD,
        evidence_grade=GRADE_FORWARD,
        high_rvol=True,
    )

    all_trades = backtest_trades + forward_trades
    mock_learning = MockLearningService(trades=all_trades)
    opt_service = OptimizationService(db=app_db, learning_service=mock_learning)
    cycle_service = DailyLearningCycleService(
        db=app_db,
        learning_service=mock_learning,
        optimization_service=opt_service,
    )

    # 2. Run Daily Learning Cycle
    report = cycle_service.run_cycle(
        strategy_id=strat_id,
        min_sample_size=10,
        feed=feed,
    )

    assert report.status in ("SUCCESS", "PARTIAL")
    assert report.trades_processed == 30
    assert report.observations_generated > 0
    assert report.hypotheses_generated > 0
    assert report.candidates_generated > 0
    assert report.recommendations_generated > 0
    assert len(report.recommendation_ids) > 0

    # 3. Verify Observations in Database
    with app_db.session() as session:
        observations = LearningObservationRepository.list_observations(session, strategy_id=strat_id)
        assert len(observations) > 0
        for obs in observations:

            assert obs["sample_size"] >= 10
            assert obs["evidence_class"] == CLASS_PAPER_FORWARD
            assert obs["confidence"] is not None

    # 4. Verify Recommendations in Database
    with app_db.session() as session:
        for rec_id in report.recommendation_ids:
            rec = OptimizationRecommendationRepository.get(session, rec_id)
            assert rec is not None
            # SAFEGUARD: Recommendation must remain research candidate (PROPOSED/RECOMMENDED/REJECTED), never auto-approved/applied
            assert rec["status"] in ("PROPOSED", "RECOMMENDED", "REJECTED")
            assert rec["status"] not in ("APPROVED", "APPLIED")
            assert rec["parameter"] == "volume_multiple"
            assert rec["current_value"] == 1.5


    # 5. SAFEGUARD: Strategy Version V1 remains completely UNCHANGED (immutability)
    with app_db.session() as session:
        v1 = StrategyRepository.get_version(session, strat_id, version=1)
        assert v1 is not None
        v1_def = json.loads(v1["definition"])
        assert v1_def["rules"]["entry"]["volume_multiple"] == 1.5

        # No V2 was created automatically
        v2 = StrategyRepository.get_version(session, strat_id, version=2)
        assert v2 is None

    # 6. Verify Cycle Persistence in SystemState
    latest_cycle = cycle_service.get_latest_cycle()
    assert latest_cycle is not None
    assert latest_cycle["cycle_id"] == report.cycle_id
    assert latest_cycle["trades_processed"] == 30
    assert latest_cycle["observations_generated"] == report.observations_generated
