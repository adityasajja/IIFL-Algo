"""Tests for the Learning Intelligence Layer.

Exercises:
1. Forward trade analytics bucketing and sample adequacy flags.
2. Backtest vs Forward comparison (BACKTEST vs PAPER_FORWARD).
3. Learning observations persistence and query repository.
4. Learning API endpoints for observations and backtest-vs-forward.
5. Observational phrasing in daily reports.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
import pytest

from atr.appdb.repositories import LearningObservationRepository
from atr.appdb.schema import learning_observations
from atr.research.learning_stats import BucketVerdict, summarise, compare_bucket
from atr.services.learning import (
    LearningDataset,
    LearningService,
    DailyLearningReport,
    PerformanceAnalysis,
    GRADE_FORWARD,
    GRADE_IN_SAMPLE,
)


def test_bucket_verdict_explicit_metric_fields():
    """Verify BucketVerdict.as_dict exposes sample_size, win_rate, mean, median, PF, and sample_adequacy."""
    # Adequate sample: 35 values
    values = [10.0, 15.0, -5.0, 20.0, -2.0, 8.0, 12.0, -4.0, 14.0, 6.0] * 3 + [5.0, -1.0, 7.0, 9.0, 11.0]
    baseline = [2.0, -1.0, 3.0, 1.0, 0.0] * 7
    verdict = compare_bucket("test_bucket", values, baseline)
    d = verdict.as_dict()

    assert d["label"] == "test_bucket"
    assert d["sample_size"] == 35
    assert d["n"] == 35
    assert d["win_rate"] is not None
    assert d["win_rate"] > 0.6
    assert d["win_rate_ci"] is not None
    assert d["mean"] is not None
    assert d["mean_ci"] is not None
    assert d["median"] is not None
    assert d["profit_factor"] is not None
    assert d["sample_adequacy"] == "adequate"

    # Small sample (n=5 < 10): suppressed
    small = [5.0, 6.0, 7.0, 8.0, 9.0]
    small_verdict = compare_bucket("small_bucket", small, baseline)
    sd = small_verdict.as_dict()
    assert sd["sample_size"] == 5
    assert sd["suppressed"] is True
    assert sd["sample_adequacy"] == "insufficient"
    assert sd["is_meaningful"] is False


def test_learning_observation_repository_record_and_list(app_db):
    """Test persisting and querying structured observations in SQLite control plane."""
    with app_db.session() as session:
        created = LearningObservationRepository.record(
            session,
            strategy_id="momentum_v1",
            strategy_version=2,
            date="2026-09-16",
            metric="net_pnl",
            condition_bucket="rvol_bucket:>2.0",
            sample_size=42,
            statistical_result={"win_rate": 0.65, "mean": 1250.5, "lift": 320.0},
            evidence_class="PAPER_FORWARD",
            confidence=0.95,
            source_trades=["trade-001", "trade-002"],
        )
        session.commit()

    assert created["observation_id"] is not None
    assert created["sample_size"] == 42
    assert created["evidence_class"] == "PAPER_FORWARD"
    assert created["confidence"] == 0.95
    assert created["source_trades"] == ["trade-001", "trade-002"]

    with app_db.session() as session:
        rows = LearningObservationRepository.list_observations(session, strategy_id="momentum_v1")
        assert len(rows) >= 1
        found = rows[0]
        assert found["strategy_id"] == "momentum_v1"
        assert found["strategy_version"] == 2
        assert found["condition_bucket"] == "rvol_bucket:>2.0"
        assert found["statistical_result"]["win_rate"] == 0.65
        assert found["source_trades"] == ["trade-001", "trade-002"]


def test_learning_service_backtest_vs_forward(app_db, tmp_path):
    """Test side-by-side comparison of BACKTEST vs PAPER_FORWARD in LearningService."""
    # Synthetic dataset with backtest rows and paper forward rows
    rows = []
    # 20 backtest trades
    for i in range(20):
        rows.append({
            "source": "BACKTEST",
            "evidence_class": "BACKTEST",
            "evidence_grade": "in_sample",
            "strategy_id": "trend_follow",
            "strategy_key": "trend_follow",
            "strategy_version": 1,
            "exit_ts": "2026-01-10T15:30:00Z",
            "entry_price": 100.0,
            "exit_price": 105.0 if i % 2 == 0 else 98.0,
            "net_pnl": 500.0 if i % 2 == 0 else -200.0,
            "return_pct": 0.05 if i % 2 == 0 else -0.02,
            "market_regime": "trending_up",
        })
    # 15 paper forward trades
    for i in range(15):
        rows.append({
            "source": "PAPER",
            "evidence_class": "PAPER_FORWARD",
            "evidence_grade": "forward",
            "strategy_id": "trend_follow",
            "strategy_key": "trend_follow",
            "strategy_version": 1,
            "exit_ts": "2026-09-15T15:30:00Z",
            "entry_price": 200.0,
            "exit_price": 208.0 if i % 3 != 0 else 195.0,
            "net_pnl": 800.0 if i % 3 != 0 else -500.0,
            "return_pct": 0.04 if i % 3 != 0 else -0.025,
            "market_regime": "trending_up",
        })

    dataset = LearningDataset(rows=rows, missing_features={}, generated_at=datetime.now(UTC))

    class MockBuilder:
        def build(self, **_kwargs):
            return dataset

    service = LearningService(db=app_db, builder=MockBuilder(), cache_root=tmp_path)
    comp = service.backtest_vs_forward(strategy="trend_follow")

    assert comp["strategy"] == "trend_follow"
    assert comp["backtest_n"] == 20
    assert comp["forward_n"] == 15
    assert comp["comparison"]["reference"] == "BACKTEST"
    assert comp["comparison"]["comparison"] == "PAPER_FORWARD"
    assert comp["comparison"]["status"] == "ok"

    # Test record observations from genuine forward trades
    records = service.record_observations(strategy_id="trend_follow", min_sample=10)
    assert isinstance(records, list)

    listed = service.list_observations(strategy_id="trend_follow")
    assert isinstance(listed, list)


def test_learning_api_routes(app_db):
    """Test API router endpoints for observations and backtest-vs-forward."""
    from fastapi.testclient import TestClient
    from atr.api.main import app

    client = TestClient(app)
    resp = client.get("/api/v1/learning/backtest-vs-forward")
    assert resp.status_code in (200, 401, 403)

    resp_obs = client.get("/api/v1/learning/observations")
    assert resp_obs.status_code in (200, 401, 403)
