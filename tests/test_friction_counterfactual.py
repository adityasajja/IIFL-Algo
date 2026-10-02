"""Friction counterfactuals: banked cost findings become run experiments.

On 2026-09-28 the book learned that costs drowned fourteen trades, and the
finding sat in a table while the stops that caused it stayed exactly where
they were. This wires the two together: a ``cost_drag`` finding proposes a
wider stop, runs it against the identical feed, and records what the
experiment found — or states plainly why there was nothing to test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from atr.appdb.engine import AppDatabase
from atr.appdb.repositories import (
    LearningObservationRepository,
    StrategyRepository,
    UserRepository,
)
from atr.data.synthetic import SyntheticConfig, SyntheticFeed
from atr.services.experiment import StrategyExperimentService


@pytest.fixture
def test_user(app_db: AppDatabase) -> dict[str, Any]:
    with app_db.session() as session:
        user = UserRepository.create(
            session,
            username="friction_researcher",
            email="friction@example.com",
            role="trader",
            password_hash="test_password_hash",
        )
        return dict(user)


def _make_strategy(
    app_db: AppDatabase, user_id: str, *, stop: float | None = 2.0
) -> str:
    exit_block: dict[str, Any] = {}
    if stop is not None:
        exit_block["stop_loss_pct"] = stop
    definition = {"rules": {"entry": {}, "exit": exit_block}}
    with app_db.session() as session:
        strat = StrategyRepository.create(
            session, user_id=user_id, name="Friction Lab Strategy", kind="rules"
        )
        StrategyRepository.create_version(
            session,
            strategy_id=strat["strategy_id"],
            author_user_id=user_id,
            definition=definition,
            change_note="V1 for friction counterfactuals",
        )
        return str(strat["strategy_id"])


def _bank_cost_finding(
    app_db: AppDatabase, strategy_id: str, *, sample_size: int = 14
) -> None:
    with app_db.session() as session:
        LearningObservationRepository.record(
            session,
            strategy_id=strategy_id,
            strategy_version=1,
            date="2026-09-28",
            metric="net_pnl",
            condition_bucket="cost_drag:cost_drowned",
            sample_size=sample_size,
            statistical_result={"mean": -350.0},
            evidence_class="PAPER_FORWARD",
            confidence=0.5,
            source_trades=[],
        )
        session.commit()


@pytest.fixture
def feed() -> SyntheticFeed:
    return SyntheticFeed(
        SyntheticConfig(
            symbols=("NIFTY50",),
            start=datetime(2024, 1, 1, 9, 30),
            end=datetime(2024, 6, 1, 15, 30),
            freq="1D",
            seed=7,
        )
    )


def test_no_cost_finding_creates_nothing(
    app_db: AppDatabase, test_user: dict[str, Any]
) -> None:
    """No banked friction lesson means no experiment — stated, not silent."""
    strategy_id = _make_strategy(app_db, test_user["user_id"])
    svc = StrategyExperimentService(db=app_db)
    result = svc.run_friction_counterfactual(
        strategy_id, 1, creator_user_id=test_user["user_id"]
    )
    assert result["created"] is False
    assert "cost" in result["reason"].lower()


def test_missing_stop_param_states_what_is_missing(
    app_db: AppDatabase, test_user: dict[str, Any]
) -> None:
    """A cost finding against a definition with no stop has nothing to widen."""
    strategy_id = _make_strategy(app_db, test_user["user_id"], stop=None)
    _bank_cost_finding(app_db, strategy_id)
    svc = StrategyExperimentService(db=app_db)
    result = svc.run_friction_counterfactual(
        strategy_id, 1, creator_user_id=test_user["user_id"]
    )
    assert result["created"] is False
    assert "stop_loss_pct" in result["reason"]


def test_friction_counterfactual_runs_end_to_end(
    app_db: AppDatabase, test_user: dict[str, Any], feed: SyntheticFeed
) -> None:
    """The morning's lesson, replayed: 2.0 stop proposes 3.0 and runs."""
    strategy_id = _make_strategy(app_db, test_user["user_id"], stop=2.0)
    _bank_cost_finding(app_db, strategy_id)
    svc = StrategyExperimentService(db=app_db)
    result = svc.run_friction_counterfactual(
        strategy_id, 1, creator_user_id=test_user["user_id"], feed=feed
    )
    assert result["created"] is True
    assert result["baseline"] == 2.0
    assert result["candidate"] == 3.0
    assert result["finding_bucket"] == "cost_drag:cost_drowned"
    assert "stop_loss_pct" in result["result"]["explanation"]["what_changed"]
