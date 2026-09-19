"""Champion vs challenger acceptance: same market, independent books, honest read.

The chain under test
--------------------
Champion (V1) + challenger (V2), launched as a pair
→ the same live tick, one shared venue (prices, costs, slippage)
→ independent execution, positions, P&L and journals
→ independent PAPER_FORWARD learning evidence per version
→ side-by-side comparison with a readiness verdict — never a promotion.

One session is driven through the genuine chain (rule, risk gate, OMS order
with its provenance stamp, paper fill, position fold, journal reconcile) for
*both* arms at once. Contexts are recorded explicitly against the shared
signal: the market conditions at a bar are one measurement, and both arms
fired on it — per-arm outcomes resolve through each arm's own orders.

Also proven, because each would silently corrupt the comparison:

* **no cross-contamination** — orders, positions, journal episodes and
  learning rows are scoped to their deployment and version;
* **no duplicate orders** — one signal raises one order per arm; retries and
  the sibling arm never collapse into each other (idempotency keys carry the
  version);
* **no strategy mutation** — definitions, hashes, version counts, deployment
  pins and statuses are identical before and after the comparison;
* **no automatic promotion** — comparing creates nothing and changes nothing,
  and there is no route or method that could promote.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from atr.appdb.repositories import (
    DeploymentRepository,
    StrategyRepository,
    TradeJournalRepository,
)
from atr.research.learning_evidence import CLASS_PAPER_FORWARD
from atr.services.champions import ChampionService
from atr.services.learning import LearningDatasetBuilder, LearningService

SYMBOL = "RELIANCE"
EXCHANGE = "NSEEQ"
USER = "u1"
SESSION = datetime(2026, 9, 14, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

V1 = {
    "engine_key": None,
    "rules": {
        "entry": {
            "breakout_lookback": 63,
            "breakout_proximity_pct": 2.0,
            "volume_multiple": 1.5,
            "volume_lookback": 20,
            "min_history_bars": 70,
        },
        "exit": {"stop_loss_pct": 3.0, "min_history_bars": 70},
    },
}
V2 = {
    "engine_key": None,
    "rules": {
        "entry": {
            "breakout_lookback": 63,
            "breakout_proximity_pct": 2.0,
            "volume_multiple": 1.9,
            "volume_lookback": 20,
            "min_history_bars": 70,
        },
        "exit": {"stop_loss_pct": 3.0, "min_history_bars": 70},
    },
}


def _daily(n: int = 300, start: float = 1000.0) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=n, freq="D")
    closes = [start + i for i in range(n)]
    volumes = [500_000.0] * n
    volumes[-1] = 5_000_000.0
    return pd.DataFrame(
        {
            "ts": index,
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": volumes,
        }
    )


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


# ---------------------------------------------------------------------------
# fixtures — a genuine paired session
# ---------------------------------------------------------------------------


@pytest.fixture()
def owner(app_db) -> str:
    from atr.appdb.schema import users

    with app_db.session() as session:
        session.execute(
            users.insert().values(
                user_id=USER,
                email="champion@example.com",
                username="champion",
                display_name="C",
                password_hash="x",
                role="owner",
                is_active=True,
                mfa_enabled=False,
                failed_logins=0,
                created_at=datetime(2026, 1, 1),
                updated_at=datetime(2026, 1, 1),
            )
        )
    return USER


@pytest.fixture()
def strategy(app_db, owner) -> dict:
    with app_db.session() as session:
        created = StrategyRepository.create(
            session, user_id=owner, name="Breakout paired", kind="rules"
        )
        StrategyRepository.create_version(
            session,
            strategy_id=created["strategy_id"],
            author_user_id=owner,
            definition=V1,
        )
        StrategyRepository.create_version(
            session,
            strategy_id=created["strategy_id"],
            author_user_id=owner,
            definition=V2,
        )
    return {"strategy_id": created["strategy_id"]}


def _deployment_config() -> dict:
    return {
        "symbols": [SYMBOL],
        "exchange": EXCHANGE,
        "timeframe": "1d",
        "order_value": 250_000.0,
        "lookback_days": 400,
        "max_open_positions": 1,
    }


@pytest.fixture()
def arms(app_db, owner, strategy, monkeypatch) -> dict:
    """Champion V1 + challenger V2, one shared session, both closed."""
    from atr.services.journal import TradeJournalService
    from atr.services.runner import PaperRunner

    monkeypatch.setattr(
        "atr.signals.engine.load_daily",
        lambda symbol, exchange="NSEEQ", **kw: _daily(),
    )
    with app_db.session() as session:
        champion = DeploymentRepository.create(
            session,
            user_id=owner,
            strategy_id=strategy["strategy_id"],
            strategy_version=1,
            mode="PAPER",
            capital=500_000.0,
            status="RUNNING",
            config=_deployment_config(),
        )
    service = ChampionService(db=app_db)
    challenger = service.launch_challenger(
        owner,
        strategy_id=strategy["strategy_id"],
        champion_deployment_id=champion["deployment_id"],
        challenger_version=2,
    )
    with app_db.session() as session:
        DeploymentRepository.start(
            session, challenger["deployment_id"], owner
        )

    runner = PaperRunner(db=app_db)
    runner.sync_loops()
    assert set(runner._loops) == {
        champion["deployment_id"],
        challenger["deployment_id"],
    }, "both arms must run simultaneously"
    frame = _daily()
    tick = round(float(frame["close"].tail(63).max()) * 0.995, 2)
    runner.venue.prices = lambda s, e: tick
    runner.pass_once(now=SESSION)

    _record_context(app_db, owner, strategy["strategy_id"])

    journal = TradeJournalService(db=app_db)
    for deployment_id in (champion["deployment_id"], challenger["deployment_id"]):
        journal.reconcile(USER, deployment_id)
        _flatten(app_db, deployment_id, multiplier=1.02)
        journal.reconcile(USER, deployment_id)

    return {
        "strategy_id": strategy["strategy_id"],
        "champion_id": champion["deployment_id"],
        "challenger_id": challenger["deployment_id"],
    }


def _flatten(app_db, deployment_id: str, *, multiplier: float) -> None:
    from atr.execution.oms import OrderDraft
    from atr.services import paper as paper_service
    from atr.services.execution import ExecutionService
    from atr.services.orders import get_order_service
    from atr.services.paper import PaperLedger

    ledger = PaperLedger(db=app_db)
    portfolio = ledger.portfolio(USER, deployment_id=deployment_id)
    position = portfolio.position(SYMBOL)
    quantity = abs(float(position.quantity))
    assert quantity > 0, "the entry must have filled before the exit is placed"
    exit_price = round(float(position.avg_price) * multiplier, 2)
    ExecutionService(
        orders=get_order_service(portfolio=portfolio),
        venue=paper_service.paper_venue(lambda s, e: exit_price),
    ).place(
        OrderDraft(
            user_id=USER,
            symbol=SYMBOL,
            side="SELL",
            quantity=quantity,
            mode="PAPER",
            exchange=EXCHANGE,
            requested_price=exit_price,
            deployment_id=deployment_id,
        )
    )


def _record_context(app_db, user_id: str, strategy_id: str) -> None:
    """One recorded context for the shared signal.

    Both arms fired on the same bar under the same conditions — that shared
    reality is recorded once, and each arm's outcome resolves through its own
    orders. Recording it per arm would either collide on the signal key or
    duplicate one measurement into two.
    """
    from sqlalchemy import select

    from atr.appdb.schema import orders
    from atr.signal_context.service import SignalContextService

    with app_db.session() as session:
        signal_id = session.execute(
            select(orders.c.signal_id)
            .where(orders.c.user_id == user_id, orders.c.side == "BUY")
            .order_by(orders.c.created_at)
            .limit(1)
        ).scalar()
    assert signal_id, "the session must have raised an entry order"

    service = SignalContextService(db=app_db)
    stock = _frame(21, up=0.002)
    bench = _frame(22)
    when = stock["ts"].iloc[250]
    market = service.engine.build_market_snapshot(
        bench, when, breadth_above_ema50_pct=62.0, regime="BULLISH_TREND"
    )
    sector = service.engine.build_sector_snapshot("IT", when, relative_strength_1m=2.0)
    context = service.engine.enrich(
        signal_id=signal_id,
        symbol=SYMBOL,
        action="BUY",
        signal_source="PAPER",
        signal_ts=str(when),
        strategy_id=strategy_id,
        strategy_version=1,
        stock_frame=stock,
        bench_frame=bench,
        market_hint=market,
        sector_hint=sector,
        when=when,
    )
    with app_db.session() as session:
        assert service.record(session, context, user_id=user_id) == 1


def _orders_by_deployment(app_db, deployment_id: str) -> list[dict]:
    from sqlalchemy import select

    from atr.appdb.schema import orders

    with app_db.session() as session:
        return [
            dict(r)
            for r in session.execute(
                select(orders).where(orders.c.deployment_id == deployment_id)
            ).mappings().all()
        ]


def _governance_snapshot(app_db) -> dict:
    from sqlalchemy import select

    from atr.appdb.schema import deployments, strategies, strategy_versions

    with app_db.session() as session:
        return {
            name: sorted(
                str(sorted(dict(r).items()))
                for r in session.execute(select(table)).mappings().all()
            )
            for name, table in (
                ("strategies", strategies),
                ("strategy_versions", strategy_versions),
                ("deployments", deployments),
            )
        }


# ---------------------------------------------------------------------------
# isolation: same market, independent books
# ---------------------------------------------------------------------------


def test_both_arms_trade_the_same_tick_independently(app_db, arms):
    champion_orders = _orders_by_deployment(app_db, arms["champion_id"])
    challenger_orders = _orders_by_deployment(app_db, arms["challenger_id"])

    def _buys(rows: list[dict]) -> list[dict]:
        return [r for r in rows if r["side"] == "BUY"]

    assert len(_buys(champion_orders)) == 1, "one signal raises one order per arm"
    assert len(_buys(challenger_orders)) == 1

    # No duplicate orders: disjoint identities across arms…
    champion_ids = {r["order_id"] for r in champion_orders}
    challenger_ids = {r["order_id"] for r in challenger_orders}
    assert champion_ids and challenger_ids
    assert champion_ids.isdisjoint(challenger_ids)

    # …but identical market data: same fill price on both entries.
    champion_fill = _buys(champion_orders)[0]["avg_fill_price"]
    challenger_fill = _buys(challenger_orders)[0]["avg_fill_price"]
    assert champion_fill > 0
    assert challenger_fill == champion_fill


def test_positions_pnl_and_journals_stay_in_their_arm(app_db, arms, strategy):
    from atr.services.paper import PaperLedger

    ledger = PaperLedger(db=app_db)
    champion_book = ledger.portfolio(USER, deployment_id=arms["champion_id"])
    challenger_book = ledger.portfolio(USER, deployment_id=arms["challenger_id"])
    assert abs(float(champion_book.position(SYMBOL).quantity)) == 0
    assert abs(float(challenger_book.position(SYMBOL).quantity)) == 0

    with app_db.session() as session:
        episodes, _ = TradeJournalRepository.list_for_user(
            session, USER, closed_only=True, limit=100
        )
    by_deployment: dict[str, list] = {}
    for episode in episodes:
        by_deployment.setdefault(episode.get("deployment_id"), []).append(episode)
    assert set(by_deployment) == {arms["champion_id"], arms["challenger_id"]}
    for deployment_id, rows in by_deployment.items():
        assert len(rows) == 1
        expected_version = 1 if deployment_id == arms["champion_id"] else 2
        assert rows[0]["strategy_version"] == expected_version
        assert rows[0]["strategy_id"] == strategy["strategy_id"]
        assert rows[0]["evidence_grade"] == "forward"


def test_learning_evidence_separates_by_version(app_db, arms, tmp_path, strategy):
    builder = LearningDatasetBuilder(db=app_db, cache_root=tmp_path, frames={})
    dataset = builder.build(user_id=USER)

    v1 = [
        r for r in dataset.rows
        if r.get("strategy_id") == strategy["strategy_id"]
        and r.get("strategy_version") == 1
        and r.get("evidence_class") == CLASS_PAPER_FORWARD
    ]
    v2 = [
        r for r in dataset.rows
        if r.get("strategy_id") == strategy["strategy_id"]
        and r.get("strategy_version") == 2
        and r.get("evidence_class") == CLASS_PAPER_FORWARD
    ]
    assert len(v1) == 1 and len(v2) == 1
    assert v1[0]["trade_ref"] != v2[0]["trade_ref"]

    from atr.signal_context.service import SignalContextService

    contexts = SignalContextService(db=app_db)
    arm1 = contexts.effectiveness(
        USER, strategy_id=strategy["strategy_id"], strategy_version=1, source="PAPER"
    )
    arm2 = contexts.effectiveness(
        USER, strategy_id=strategy["strategy_id"], strategy_version=2, source="PAPER"
    )
    # One shared signal, one outcome per arm.
    assert arm1["forward_n"] == 1 and arm1["forward_with_metric"] == 1
    assert arm2["forward_n"] == 1 and arm2["forward_with_metric"] == 1


# ---------------------------------------------------------------------------
# comparison: side-by-side, readiness verdict, no promotion
# ---------------------------------------------------------------------------


def test_compare_reports_both_arms_and_insufficient_evidence(app_db, arms, strategy):
    service = ChampionService(db=app_db)
    before = _governance_snapshot(app_db)

    result = service.compare(
        USER, strategy["strategy_id"], champion_version=1, challenger_version=2
    )

    assert result["champion_version"] == 1
    assert result["challenger_version"] == 2
    comparison = result["comparison"]
    assert comparison["champion"]["n"] == 1
    assert comparison["challenger"]["n"] == 1
    assert comparison["verdict"] == "INSUFFICIENT EVIDENCE"
    assert "deltas" in comparison and "mean" in comparison["deltas"]
    assert "p_value" not in comparison["deltas"], "no test on the difference, ever"

    assert result["context"]["champion"]["forward_n"] == 1
    assert result["context"]["challenger"]["forward_n"] == 1

    assert result["definition_diff"]["changes"] == [
        {
            "parameter": "rules.entry.volume_multiple",
            "champion": 1.5,
            "challenger": 1.9,
        }
    ]
    assert [(t["version"], t["role"]) for t in result["timeline"]] == [
        (1, "CHAMPION"),
        (2, "CHALLENGER"),
    ]
    assert [t["forward_observations"] for t in result["timeline"]] == [1, 1]

    assert result["parity"]["identical_capital"] is True
    assert result["parity"]["identical_config"] is True
    assert result["parity"]["venue_shared"] is True

    assert result["advisory_only"] is True
    assert result["applies_changes"] is False
    assert result["promotes"] is False

    # Comparing creates nothing and changes nothing.
    assert _governance_snapshot(app_db) == before


def test_roles_infer_from_running_arms_when_unnamed(app_db, arms, strategy):
    result = ChampionService(db=app_db).compare(USER, strategy["strategy_id"])
    assert (result["champion_version"], result["challenger_version"]) == (1, 2)


def test_no_strategy_mutation_no_promotion(app_db, arms, strategy, tmp_path):
    service = ChampionService(db=app_db)

    with app_db.session() as session:
        hashes_before = {
            v["version"]: v["definition_hash"]
            for v in StrategyRepository.versions(session, strategy["strategy_id"])
        }
        count_before = StrategyRepository.version_count(session, strategy["strategy_id"])
    deployments_before = _governance_snapshot(app_db)["deployments"]

    LearningService(db=app_db).readiness(user_id=USER)
    service.compare(USER, strategy["strategy_id"], champion_version=1, challenger_version=2)

    with app_db.session() as session:
        hashes_after = {
            v["version"]: v["definition_hash"]
            for v in StrategyRepository.versions(session, strategy["strategy_id"])
        }
        assert StrategyRepository.version_count(session, strategy["strategy_id"]) == count_before
        for version in (1, 2):
            assert StrategyRepository.version(
                session, strategy["strategy_id"], version
            )["is_deployed"] is False, "comparing must not flip deployment flags"
    assert hashes_before == hashes_after
    assert _governance_snapshot(app_db)["deployments"] == deployments_before
    with app_db.session() as session:
        rows = DeploymentRepository.list_for_user(session, USER, status="RUNNING")
    assert {(r["strategy_version"], r["mode"]) for r in rows} == {(1, "PAPER"), (2, "PAPER")}
