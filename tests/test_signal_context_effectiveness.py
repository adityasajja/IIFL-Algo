"""Acceptance: does the context score carry information about forward outcomes?

The chain under test is the whole point of the Context-Aware Signal Engine:

    signal -> context score -> closed forward trade -> learning dataset
           -> context effectiveness analysis -> statistical observation

Nothing here is a stand-in. Contexts are recorded through
:class:`~atr.signal_context.service.SignalContextService`; outcomes are resolved
from the real order book and trade journal; the analysis runs through
:mod:`atr.research.learning_context`; and supported findings are persisted as
learning observations. The suite also pins the non-interference promise: running
the analysis changes no strategy, version or deployment.

The score is set directly on the built context. Scoring itself is measured
elsewhere (``tests/test_signal_context.py``); what this suite has to prove is the
pipeline from *a given score* to *a persisted finding*, and driving the score
keeps the fixture from depending on which way a random walk happened to lean.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atr.appdb.engine import utcnow
from atr.appdb.repositories import (
    DeploymentRepository,
    LearningObservationRepository,
    OrderRepository,
    StrategyRepository,
    TradeJournalRepository,
    UserRepository,
)
from atr.appdb.schema import deployments, strategies, strategy_versions
from atr.research.learning_evidence import (
    CLASS_PAPER_FORWARD,
    GRADE_FORWARD,
)
from atr.services.learning import LearningDatasetBuilder
from atr.signal_context.engine import SignalContextEngine
from atr.signal_context.service import SignalContextService

HIGH_SCORE = 90
LOW_SCORE = 10
HIGH_RETURN = 3.0
LOW_RETURN = -3.0
MIN_SAMPLE = 10


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _frame(seed: int, periods: int = 300, up: float = 0.001) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(up, 0.012, periods).cumsum()
    close = 100.0 * np.exp(steps)
    high = close * (1 + rng.uniform(0.001, 0.02, periods))
    low = close * (1 - rng.uniform(0.001, 0.02, periods))
    open_ = close * (1 + rng.normal(0, 0.004, periods))
    volume = rng.integers(100_000, 900_000, periods).astype(float)
    return pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=periods, freq="B"),
            "open": open_,
            "high": np.maximum.reduce([high, open_, close]),
            "low": np.minimum.reduce([low, open_, close]),
            "close": close,
            "volume": volume,
        }
    )


def _make_user(app_db, *, username: str = "owner") -> str:
    with app_db.session() as session:
        user = UserRepository.create(
            session,
            email=f"{username}@example.com",
            username=username,
            password_hash="x",
            role="owner",
        )
    return user["user_id"]


def _context(
    engine: SignalContextEngine,
    *,
    signal_id: str,
    symbol: str,
    source: str,
    score: int,
    seed: int,
):
    """A fully formed context (real snapshots) carrying a chosen score."""
    stock = _frame(seed, up=0.002)
    bench = _frame(seed + 1)
    when = stock["ts"].iloc[250]
    market = engine.build_market_snapshot(
        bench,
        when,
        breadth_above_ema50_pct=62.0,
        regime="BULLISH_TREND",
    )
    sector = engine.build_sector_snapshot("IT", when, relative_strength_1m=2.0)
    context = engine.enrich(
        signal_id=signal_id,
        symbol=symbol,
        action="BUY",
        signal_source=source,
        signal_ts=str(when),
        strategy_id="strat-1",
        strategy_version=1,
        stock_frame=stock,
        bench_frame=bench,
        market_hint=market,
        sector_hint=sector,
        when=when,
    )
    context.context_score = score
    context.has_insufficient_data = False
    context.context_class = engine.config.classify(score, False)
    return context


def _seed_forward(
    app_db,
    service: SignalContextService,
    user_id: str,
    *,
    prefix: str,
    count: int,
    score: int,
    return_pct: float,
    strategy_id: str = "strat-1",
) -> list[str]:
    """``count`` closed paper trades, each with a context and a journal episode.

    One distinct symbol per trade so the journal episode and the order carrying
    the same ``signal_id`` match one-to-one, which is what the service's outcome
    resolution relies on.
    """
    engine = service.engine
    created: list[str] = []
    for i in range(count):
        symbol = f"{prefix}{i:02d}"
        signal_id = f"{symbol}:2024-06-03"
        with app_db.session() as session:
            service.record(
                session,
                _context(
                    engine,
                    signal_id=signal_id,
                    symbol=symbol,
                    source="PAPER",
                    score=score,
                    seed=100 + i,
                ),
                user_id=user_id,
            )
        with app_db.session() as session:
            OrderRepository.create(
                session,
                user_id=user_id,
                symbol=symbol,
                side="BUY",
                quantity=10,
                strategy_id=strategy_id,
                strategy_version=1,
                signal_id=signal_id,
            )
            episode = TradeJournalRepository.open_trade(
                session,
                user_id=user_id,
                symbol=symbol,
                side="BUY",
                quantity=10,
                entry_price=100.0,
                entry_ts=utcnow(),
                strategy_id=strategy_id,
                strategy_version=1,
                evidence_grade=GRADE_FORWARD,
            )
            TradeJournalRepository.close_trade(
                session,
                episode["trade_id"],
                user_id,
                exit_price=100.0 * (1.0 + return_pct / 100.0),
                gross_pnl=return_pct * 10.0,
                net_pnl=return_pct * 10.0,
                exit_ts=utcnow(),
            )
        created.append(signal_id)
    return created


def _table_rows(app_db, table) -> list[dict]:
    from sqlalchemy import select

    with app_db.session() as session:
        rows = [dict(r) for r in session.execute(select(table)).mappings().all()]
    return sorted(rows, key=lambda r: str(sorted(r.items())))


# ---------------------------------------------------------------------------
# 1. forward and in-sample outcomes never mix
# ---------------------------------------------------------------------------


def test_forward_and_in_sample_outcomes_are_reported_separately(app_db):
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db)

    # One backtest context joined to a closed backtest trade (in-sample)...
    from atr.appdb.repositories import BacktestArtefactRepository, BacktestRunRepository

    with app_db.session() as session:
        run = BacktestRunRepository.create(
            session,
            user_id=user_id,
            config={"strategy_id": "strat-1"},
            strategy_id="strat-1",
            strategy_version=1,
        )
        run_id = run["run_id"]
        BacktestArtefactRepository.save_trades(
            session,
            run_id,
            [
                {
                    "symbol": "INFY",
                    "direction": "LONG",
                    "quantity": 10,
                    "entry_ts": utcnow(),
                    "entry_price": 100.0,
                    "exit_price": 120.0,
                    "gross_pnl": 200.0,
                    "net_pnl": 180.0,
                    "return_pct": 20.0,
                    "strategy_id": "strat-1",
                    "strategy_version": 1,
                }
            ],
        )
        trade_id = BacktestArtefactRepository.trades(session, run_id)[0]["trade_id"]

    with app_db.session() as session:
        service.record(
            session,
            _context(
                service.engine,
                signal_id=f"{run_id}:0",
                symbol="INFY",
                source="BACKTEST",
                score=90,
                seed=7,
            ),
            user_id=user_id,
            run_id=run_id,
            trade_id=trade_id,
        )

    # ...and one forward context joined to a closed paper trade.
    _seed_forward(
        app_db, service, user_id, prefix="FWD", count=1, score=90, return_pct=2.0
    )

    result = service.effectiveness(user_id, min_sample=MIN_SAMPLE)

    assert result["forward_n"] == 1
    assert result["in_sample_n"] == 1
    assert result["model_version"] == service.engine.config.version

    # Every in-sample bucket is descriptive and explicitly not a claim.
    for axis in result["in_sample_axes"]:
        for bucket in axis["buckets"]:
            assert bucket["significance"] == "in_sample_not_a_claim"
            assert bucket["evidence_class"] == "IN_SAMPLE"

    # The score verdict is built from forward observations only.
    assert result["score_verdict"]["may_claim"] is False


# ---------------------------------------------------------------------------
# 2. a supported forward sample becomes an observation
# ---------------------------------------------------------------------------


def test_supported_forward_buckets_are_persisted_as_observations(app_db):
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db)

    _seed_forward(
        app_db, service, user_id, prefix="HI", count=12, score=HIGH_SCORE, return_pct=HIGH_RETURN
    )
    _seed_forward(
        app_db, service, user_id, prefix="LO", count=12, score=LOW_SCORE, return_pct=LOW_RETURN
    )

    result = service.effectiveness(user_id, min_sample=MIN_SAMPLE)
    assert result["forward_n"] == 24
    assert result["forward_with_metric"] == 24
    assert result["score_verdict"]["may_claim"] is True
    assert result["score_verdict"]["monotonic_high_is_better"] is True
    assert result["score_verdict"]["means"]["80-100"] == float(HIGH_RETURN)
    assert result["score_verdict"]["means"]["0-39"] == float(LOW_RETURN)

    observations = service.record_effectiveness_observations(
        user_id, min_sample=MIN_SAMPLE
    )
    assert observations, "a supported forward sample must produce an observation"

    buckets = {o["condition_bucket"] for o in observations}
    assert "context_score:80-100" in buckets
    assert "context_score:0-39" in buckets
    assert not any(b.startswith("context_context_") for b in buckets)
    assert all(o["evidence_class"] == CLASS_PAPER_FORWARD for o in observations)
    assert all(o["sample_size"] >= MIN_SAMPLE for o in observations)

    # And they are really in the store, readable through the repository.
    with app_db.session() as session:
        stored = LearningObservationRepository.list_observations(
            session, evidence_class=CLASS_PAPER_FORWARD, limit=500
        )
    assert any(o["condition_bucket"] == "context_score:80-100" for o in stored)


# ---------------------------------------------------------------------------
# 3. a sample below the floor persists nothing and claims nothing
# ---------------------------------------------------------------------------


def test_a_sample_below_the_floor_persists_nothing(app_db):
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db)

    _seed_forward(
        app_db, service, user_id, prefix="SM", count=3, score=HIGH_SCORE, return_pct=HIGH_RETURN
    )

    result = service.effectiveness(user_id, min_sample=MIN_SAMPLE)
    assert result["forward_n"] == 3
    assert result["score_verdict"]["may_claim"] is False

    assert service.record_effectiveness_observations(user_id, min_sample=MIN_SAMPLE) == []
    with app_db.session() as session:
        assert (
            LearningObservationRepository.list_observations(
                session, evidence_class=CLASS_PAPER_FORWARD, limit=500
            )
            == []
        )


# ---------------------------------------------------------------------------
# 4. the analysis changes no strategy, version or deployment
# ---------------------------------------------------------------------------


def test_effectiveness_leaves_strategy_and_deployment_tables_untouched(app_db):
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db)

    with app_db.session() as session:
        strategy = StrategyRepository.create(
            session, user_id=user_id, name="Governed", kind="rules"
        )
        version = StrategyRepository.create_version(
            session,
            strategy_id=strategy["strategy_id"],
            author_user_id=user_id,
            definition={"engine_key": None, "rules": {"entry": {}}},
        )
        DeploymentRepository.create(
            session,
            user_id=user_id,
            strategy_id=strategy["strategy_id"],
            strategy_version=int(version["version"]),
            mode="PAPER",
            capital=100_000.0,
            status="RUNNING",
        )

    _seed_forward(app_db, service, user_id, prefix="GV", count=12, score=HIGH_SCORE, return_pct=1.0)
    _seed_forward(app_db, service, user_id, prefix="GW", count=12, score=LOW_SCORE, return_pct=-1.0)

    before = {
        "strategies": _table_rows(app_db, strategies),
        "strategy_versions": _table_rows(app_db, strategy_versions),
        "deployments": _table_rows(app_db, deployments),
    }

    service.effectiveness(user_id, min_sample=MIN_SAMPLE)
    service.record_effectiveness_observations(user_id, min_sample=MIN_SAMPLE)

    after = {
        "strategies": _table_rows(app_db, strategies),
        "strategy_versions": _table_rows(app_db, strategy_versions),
        "deployments": _table_rows(app_db, deployments),
    }
    assert before == after


# ---------------------------------------------------------------------------
# 5. the same forward trade is visible to the dataset and to effectiveness
# ---------------------------------------------------------------------------


def test_a_forward_trade_appears_in_both_the_dataset_and_effectiveness(
    app_db, tmp_path
):
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db)

    signal_id = "LINK:2024-06-03"
    with app_db.session() as session:
        service.record(
            session,
            _context(
                service.engine,
                signal_id=signal_id,
                symbol="LINK",
                source="PAPER",
                score=HIGH_SCORE,
                seed=42,
            ),
            user_id=user_id,
        )
        OrderRepository.create(
            session,
            user_id=user_id,
            symbol="LINK",
            side="BUY",
            quantity=10,
            strategy_id="strat-1",
            strategy_version=1,
            signal_id=signal_id,
        )
        episode = TradeJournalRepository.open_trade(
            session,
            user_id=user_id,
            symbol="LINK",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            entry_ts=utcnow(),
            strategy_id="strat-1",
            strategy_version=1,
            evidence_grade=GRADE_FORWARD,
        )
        TradeJournalRepository.close_trade(
            session,
            episode["trade_id"],
            user_id,
            exit_price=104.0,
            gross_pnl=40.0,
            net_pnl=40.0,
            exit_ts=utcnow(),
        )

    dataset = LearningDatasetBuilder(db=app_db, cache_root=tmp_path, frames={}).build()
    assert len(dataset) == 1
    row = dataset.rows[0]
    assert row["evidence_class"] == CLASS_PAPER_FORWARD
    assert row["evidence_grade"] == GRADE_FORWARD

    result = service.effectiveness(user_id, min_sample=MIN_SAMPLE)
    assert result["forward_n"] == 1
    assert result["forward_with_metric"] == 1


# ---------------------------------------------------------------------------
# 6. a percentage return is derived for a forward episode
# ---------------------------------------------------------------------------


def test_forward_return_pct_is_derived_from_the_episode_prices(app_db):
    """``trade_journal`` has no ``return_pct`` column; the service derives it."""
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db)

    _seed_forward(
        app_db, service, user_id, prefix="PC", count=1, score=HIGH_SCORE, return_pct=2.5
    )

    result = service.effectiveness(user_id, min_sample=MIN_SAMPLE)
    assert result["forward_with_metric"] == 1
    assert result["score_verdict"]["means"]["80-100"] == 2.5
