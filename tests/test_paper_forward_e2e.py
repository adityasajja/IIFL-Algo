"""LIVE TICK → SIGNAL → RISK → OMS → PAPER FILL → CLOSED TRADE → PAPER_FORWARD → DATASET.

What this suite protects
------------------------

The learning engine's only claim to being worth anything is that it can tell
**evidence** from **measurement**, and its forward observations are supposed to
come from a paper deployment that actually traded. Everything needed for that
exists — the rule layer, the risk gate, the OMS, the venue, the ledger, the
journal, the dataset builder — and this file is the one place where the whole
chain is driven in a single test, through the real components, with the runner
doing the trading.

Three deliberate choices, and each one is the difference between a test and a
decoration:

* **No rule stubbing.** ``eval_entry`` / ``eval_exit`` are the real functions the
  scanner and the backtester call. The entry fires because a live tick genuinely
  satisfies a stored breakout rule, and the exit fires because the price
  genuinely breaches the stored stop. A test that stubbed the rules would prove
  the plumbing between two fakes.
* **No hand-written journal row.** The episode is opened and closed by
  :class:`~atr.services.journal.TradeJournalService` reconciling the position
  fold, which is the production path. Seeding ``trade_journal`` directly — as
  ``test_learning_forward_paper.py`` does for the *backfill* case, on purpose —
  would prove nothing about the pipeline, because the pipeline is the thing that
  has to work.
* **Both legs through the runner.** The entry *and* the exit are driven by
  ``PaperRunner.pass_once``, so the risk gate, the OMS and the venue are
  exercised on the closing side too. Closing the position with a hand-placed
  order would leave the one leg that most often breaks — a SELL that the risk
  engine reads as opening a short — untested.

The final link is the point of all of it: the closed trade must arrive in the
learning dataset graded ``forward`` and classed ``PAPER_FORWARD``, with the
context a real trade had. That is the first genuine forward observation the
system is capable of producing, and if any link stops carrying the provenance the
row comes out in-sample and these tests fail.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from atr.research.learning_evidence import (
    CLASS_PAPER_FORWARD,
    GRADE_FORWARD,
    GRADE_IN_SAMPLE,
)

SYMBOL = "RELIANCE"
EXCHANGE = "NSEEQ"
#: A Monday, 10:00 IST — inside the cash session. Aware on purpose: a naive
#: datetime is read in the machine's local zone, which would make the session
#: gate depend on where the test runs.
SESSION = datetime(2026, 9, 14, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
#: The deployment's capital and the size of one trade.
CAPITAL = 500_000.0
ORDER_VALUE = 200_000.0

#: A breakout entry and a stop-loss exit, as a *stored strategy version* — the
#: same shape a version authored from the rule layer has. Both legs are real
#: rules: the entry needs a 63-bar high within 2% and a volume surge, the exit
#: needs a 3% loss against the entry price.
STRATEGY_DEFINITION = {
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


def _daily(n: int = 300, start: float = 1000.0) -> pd.DataFrame:
    """A rising series with a volume surge on its last bar.

    Both conditions are needed for the breakout rule to fire, so the entry is a
    consequence of the stored rule rather than of a number this test liked.
    """
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


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_live_seams():
    """Leave the process-wide feed seams clear.

    A subscriber left installed by another file would have this suite's runner
    reaching for a broadcaster that is not there. The point of the suite is the
    trading chain, not the feed wiring.
    """
    from atr.services import paper as paper_service

    paper_service.install_live_source(None)
    paper_service.install_live_subscriber(None)
    yield
    paper_service.install_live_source(None)
    paper_service.install_live_subscriber(None)


@pytest.fixture()
def owner(app_db) -> str:
    from atr.appdb.schema import users

    with app_db.session() as session:
        session.execute(
            users.insert().values(
                user_id="u1",
                email="e2e@example.com",
                username="e2e",
                display_name="E",
                password_hash="x",
                role="owner",
                is_active=True,
                mfa_enabled=False,
                failed_logins=0,
                created_at=datetime(2026, 1, 1),
                updated_at=datetime(2026, 1, 1),
            )
        )
    return "u1"


class Chain:
    """A running deployment plus the live price it is being evaluated against."""

    def __init__(self, runner, deployment_id: str, loop, prices: dict[str, float], tick: float):
        self.runner = runner
        self.deployment_id = deployment_id
        self.loop = loop
        self.prices = prices
        self.tick = tick

    def move(self, price: float) -> None:
        """Publish a new live tick."""
        self.prices[SYMBOL] = round(price, 2)

    @property
    def price(self) -> float:
        return self.prices[SYMBOL]


@pytest.fixture()
def chain(app_db, owner, monkeypatch) -> Chain:
    """A RUNNING paper deployment over a rising series, priced off a live tick.

    ``load_daily`` is the only thing stubbed: the daily cache is the *history*
    the rules need for warmup, and this test is about the live price, not about
    the cache.
    """
    from atr.appdb.repositories import DeploymentRepository, StrategyRepository
    from atr.services.runner import PaperRunner

    monkeypatch.setattr(
        "atr.signals.engine.load_daily",
        lambda symbol, exchange="NSEEQ", **kw: _daily(),
    )

    with app_db.session() as session:
        strategy = StrategyRepository.create(
            session, user_id="u1", name="E2E breakout", kind="rules"
        )
        version = StrategyRepository.create_version(
            session,
            strategy_id=strategy["strategy_id"],
            author_user_id="u1",
            definition=STRATEGY_DEFINITION,
        )
        deployment = DeploymentRepository.create(
            session,
            user_id="u1",
            strategy_id=strategy["strategy_id"],
            strategy_version=int(version["version"]),
            mode="PAPER",
            capital=CAPITAL,
            status="RUNNING",
            config={
                "symbols": [SYMBOL],
                "exchange": EXCHANGE,
                "order_value": ORDER_VALUE,
                "lookback_days": 400,
                "max_open_positions": 1,
            },
        )

    # A tick just inside the 63-bar high, which is what the stored entry rule
    # asks for.
    frame = _daily()
    high_63 = float(frame["close"].tail(63).max())
    tick = round(high_63 * 0.995, 2)

    runner = PaperRunner(db=app_db)
    runner.sync_loops()
    loop = runner._loops[deployment["deployment_id"]]

    prices = {SYMBOL: tick}
    # The live feed, as the venue sees it. Everything downstream reads the price
    # through this and nothing else.
    loop.venue.prices = lambda symbol, exchange: prices.get(symbol.upper())

    return Chain(runner, deployment["deployment_id"], loop, prices, tick)


# ---------------------------------------------------------------------------
# helpers — reading back what the chain recorded
# ---------------------------------------------------------------------------
def _orders(app_db) -> list[dict]:
    from sqlalchemy import text

    with app_db.session() as session:
        return [
            dict(row)
            for row in session.execute(
                text("select * from orders order by created_at, order_id")
            ).mappings()
        ]


def _events(app_db, order_id: str) -> list[dict]:
    from atr.appdb.repositories import OrderEventRepository

    with app_db.session() as session:
        return OrderEventRepository.history(session, order_id)


def _raw(event: dict) -> dict:
    raw = event.get("raw")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw or {}


def _journal_rows(app_db) -> list[dict]:
    from sqlalchemy import text

    with app_db.session() as session:
        return [
            dict(row)
            for row in session.execute(
                text("select * from trade_journal order by entry_ts")
            ).mappings()
        ]


def _dataset(app_db, tmp_path):
    from atr.services.learning import LearningDatasetBuilder

    return LearningDatasetBuilder(db=app_db, cache_root=tmp_path, frames={}).build()


def _open_then_close(chain: Chain) -> None:
    """Drive both legs of one round trip through the runner."""
    entry = chain.runner.pass_once(now=SESSION)
    assert entry.orders == 1, f"the entry leg did not trade: {entry}"

    # A tick through the stored 3% stop. The exit rule is the stored one, so the
    # close is a consequence of the price, not of an instruction.
    chain.move(chain.price * 0.95)
    closing = chain.runner.pass_once(now=SESSION + timedelta(minutes=5))
    assert closing.orders == 1, f"the exit leg did not trade: {closing}"


# ---------------------------------------------------------------------------
# 1. the whole chain, in one test
# ---------------------------------------------------------------------------
def test_a_live_tick_becomes_a_closed_forward_learning_observation(
    chain, app_db, tmp_path
):
    """The headline property, link by link, with nothing stubbed in between."""
    # --- LIVE TICK -----------------------------------------------------
    assert chain.price > 0

    # --- STRATEGY SIGNAL → RISK → OMS → PAPER FILL ---------------------
    _open_then_close(chain)

    # --- CLOSED TRADE --------------------------------------------------
    # The fold says flat, and the journal says so too.
    assert chain.loop._portfolio().position(SYMBOL).quantity == 0
    journal = _journal_rows(app_db)
    assert len(journal) == 1, "the round trip did not produce exactly one episode"
    assert journal[0]["exit_ts"] is not None, "the episode was never closed"

    # --- PAPER_FORWARD -------------------------------------------------
    assert journal[0]["evidence_grade"] == GRADE_FORWARD

    # --- LEARNING DATASET ----------------------------------------------
    dataset = _dataset(app_db, tmp_path)
    assert len(dataset) == 1, "the closed trade did not reach the dataset"

    row = dataset.rows[0]
    assert row["evidence_grade"] == GRADE_FORWARD
    assert row["evidence_class"] == CLASS_PAPER_FORWARD
    assert row["is_forward"] is True
    assert row["evidence_note"], "the row must state the basis of its grade"
    assert row["source"] == "PAPER"
    assert row["source_ref"] == chain.deployment_id
    assert dataset.grade_counts() == {GRADE_FORWARD: 1, GRADE_IN_SAMPLE: 0}

    # The summary is what the screen leads with.
    summary = dataset.summary()
    assert summary["forward_observations"] == 1
    assert summary["in_sample_observations"] == 0
    assert summary["latest_forward_ts"] is not None


def test_the_forward_row_carries_the_trade_the_chain_actually_produced(
    chain, app_db, tmp_path
):
    """Real context, and the gaps named rather than filled in."""
    _open_then_close(chain)
    row = _dataset(app_db, tmp_path).rows[0]

    # attribution
    assert row["strategy_id"] == chain.loop.row["strategy_id"]
    assert row["strategy_version"] == chain.loop.row["strategy_version"]

    # timing — stamped when the order executed
    assert row["entry_ts"] is not None and row["exit_ts"] is not None
    assert row["exit_ts"] >= row["entry_ts"]
    assert row["day_of_week"] is not None
    assert row["time_of_day"] is not None

    # outcome — rupees, size and costs, all from the fold
    assert row["quantity"] > 0
    assert row["entry_price"] > 0
    assert row["exit_price"] < row["entry_price"], "the stop-loss leg lost money"
    assert row["return_pct"] < 0
    assert row["gross_pnl"] is not None
    assert row["net_pnl"] is not None
    assert row["net_pnl"] < row["gross_pnl"], "costs were not deducted"
    assert row["commission"] and row["commission"] > 0, "NSE delivery costs are never 0"

    # excursions and slippage, measured on the fills the log holds
    assert row["mfe"] is not None and "mfe" not in row["missing_features"]
    assert row["mae"] is not None and "mae" not in row["missing_features"]
    assert row["slippage_bps"] is not None
    assert "slippage_bps" not in row["missing_features"]

    # what the chain cannot supply is absent *and* named
    assert row["vwap_relationship"] is None
    assert "vwap_relationship" in row["missing_features"]


# ---------------------------------------------------------------------------
# 2. each link, pinned on its own
# ---------------------------------------------------------------------------
def test_the_order_lifecycle_is_the_oms_state_machine(chain, app_db):
    """RISK → OMS, in order, with the source of every transition recorded.

    The risk link is asserted here rather than inferred from the trade: a fill
    that happened to be small enough to pass would look identical to a fill that
    was actually measured against the limits.
    """
    _open_then_close(chain)
    orders = _orders(app_db)
    assert len(orders) == 2, "one order for the entry and one for the exit"

    for order in orders:
        events = _events(app_db, order["order_id"])
        path = [e["to_status"] for e in events]
        assert path == [
            "NEW",
            "VALIDATING",
            "RISK_APPROVED",
            "SUBMITTED",
            "ACKNOWLEDGED",
            "FILLED",
        ], f"unexpected lifecycle for {order['side']}: {path}"

        # Requirement 4 — the risk engine decided, and the decision is on the
        # record with its own source rather than being assumed.
        risk = [e for e in events if e["to_status"] == "RISK_APPROVED"]
        assert risk and risk[0]["source"] == "risk"

        # Requirement 5/6 — the order was raised by the OMS and filled by the
        # paper venue, not by anything else.
        assert events[0]["source"] == "oms"
        assert all(_raw(e).get("venue") == "paper" for e in events if e.get("filled_qty"))


def test_the_exit_is_a_sell_that_the_risk_gate_allowed_as_a_close(chain, app_db):
    """The leg that most often breaks.

    The gate compares a SELL against the portfolio's holding. Handed a flat book
    — the shared default — every exit reads as opening a short and is refused
    whenever ``allow_short`` is false, so stop-losses would never execute and the
    journal would never close an episode. The runner binds the deployment's own
    folded book, which is what this asserts.
    """
    _open_then_close(chain)
    orders = _orders(app_db)
    sells = [o for o in orders if o["side"] == "SELL"]
    assert len(sells) == 1, "the stop-loss never reached the book"
    assert sells[0]["status"] == "FILLED"
    assert float(sells[0]["filled_quantity"]) > 0

    # The gate saw the holding, not a flat book.
    held = float([o for o in orders if o["side"] == "BUY"][0]["filled_quantity"])
    assert float(sells[0]["filled_quantity"]) == pytest.approx(held)


def test_the_venue_priced_the_fill_off_the_live_tick(chain, app_db):
    """Requirement 3 — the fill is at the tick, not at the daily close.

    The cached history says ~1299; the tick is 1292.50. A fill at the cached
    close would mean the deployment is a day behind the market it is trading.
    """
    entry_tick = chain.price
    _open_then_close(chain)

    buy = [o for o in _orders(app_db) if o["side"] == "BUY"][0]
    assert float(buy["requested_price"]) == pytest.approx(entry_tick)
    # Filled at the tick plus modelled slippage, not at the daily close.
    assert float(buy["avg_fill_price"]) == pytest.approx(entry_tick, rel=0.01)
    assert float(buy["avg_fill_price"]) != pytest.approx(1299.0, rel=0.001)


def test_the_entry_order_carries_the_execution_time_provenance_stamp(chain, app_db):
    """Requirement 9, at its source.

    The stamp is what the journal copies and the dataset grades on, so it is
    pinned where it is written: the order's ``NEW`` event, at creation, by the
    OMS. A timestamp cannot do this job — a backfill harness writes those too.
    """
    from atr.execution.oms import PROVENANCE_FORWARD, PROVENANCE_KEY, RECORDED_AT_KEY

    chain.runner.pass_once(now=SESSION)
    buy = [o for o in _orders(app_db) if o["side"] == "BUY"][0]
    new = _events(app_db, buy["order_id"])[0]
    assert new["to_status"] == "NEW"

    raw = _raw(new)
    assert raw.get(PROVENANCE_KEY) == PROVENANCE_FORWARD
    assert raw.get(RECORDED_AT_KEY), "the instant the claim was made must be recorded"
    # The rule's own words travel with it, which is what makes an order
    # attributable to a decision rather than only to a symbol.
    assert "breakout" in (raw.get("reason") or "")


def test_the_closed_episode_records_a_measured_round_trip(chain, app_db):
    """Requirement 8 — the close is a measurement, not a flag."""
    entry_tick = chain.price
    _open_then_close(chain)
    row = _journal_rows(app_db)[0]

    assert float(row["entry_price"]) == pytest.approx(entry_tick, rel=0.01)
    assert float(row["exit_price"]) < float(row["entry_price"])
    assert float(row["gross_pnl"]) < 0
    assert float(row["net_pnl"]) < float(row["gross_pnl"]), "costs were not deducted"

    # The closing order's own reason, from its NEW event — a property of the
    # decision, which the fills could never have told us.
    assert row["exit_reason"] and "stop_loss" in row["exit_reason"]
    # Slippage is the mean of the legs that could be measured, adverse positive.
    assert row["slippage_bps"] is not None
    assert float(row["slippage_bps"]) > 0


def test_the_learning_service_reports_the_forward_observation(chain, app_db, tmp_path):
    """Requirement 10 — the learning pipeline picks it up without being told.

    The dataset builder reads the journal the runner wrote; nothing here points
    it at the trade.
    """
    from atr.services.learning import LearningService

    service = LearningService(db=app_db, cache_root=tmp_path)
    before = service.status()
    assert before["counts"]["trade_journal"] == 0
    assert before["forward_available"] == 0
    assert before["sufficient_for_a_claim"] is False

    _open_then_close(chain)

    after = service.status()
    assert after["counts"]["trade_journal"] == 1
    assert after["counts"]["trade_journal_forward"] == 1
    assert after["counts"]["trade_journal_in_sample"] == 0
    assert after["forward_available"] == 1
    # One forward observation is evidence and still not a claim, and the status
    # keeps those two questions apart.
    assert after["sufficient_for_a_claim"] is False
    assert "1 of them forward evidence" in after["note"]


# ---------------------------------------------------------------------------
# 3. the loop's own gates still hold
# ---------------------------------------------------------------------------
def test_a_closed_market_produces_no_trade_and_no_evidence(chain, app_db, tmp_path):
    """Requirement 1's converse.

    The runner evaluates only inside the session, because a paper fill at 20:15
    IST is a fill at a price no exchange offered — and it would arrive in the
    dataset as forward evidence, which is worse than not trading at all.
    """
    tick = chain.runner.pass_once(now=datetime(2026, 9, 14, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata")))

    assert tick.signals == 0 and tick.orders == 0
    assert _orders(app_db) == []
    assert _journal_rows(app_db) == []
    assert len(_dataset(app_db, tmp_path)) == 0


def test_a_deployment_whose_version_is_missing_trades_nothing(chain, app_db, tmp_path):
    """The refusal that protects attribution.

    A deployment whose pinned version cannot be resolved must not fall back to a
    generic ruleset: every artefact would look correctly attributed while the
    rules actually run were somebody else's. It records why and does nothing —
    and so produces no evidence.

    The row is changed in the *database*, not on the loop, because the runner
    re-reads the deployment on every sync — which is itself the property that
    lets a config change or a crash restart be picked up.
    """
    from sqlalchemy import update

    from atr.appdb.schema import deployments

    with app_db.session() as session:
        session.execute(
            update(deployments)
            .where(deployments.c.deployment_id == chain.deployment_id)
            .values(strategy_version=99)
        )

    tick = chain.runner.pass_once(now=SESSION)

    assert tick.orders == 0
    assert chain.loop.row["strategy_version"] == 99, "the row was not re-read"
    assert chain.loop.blocked_reason, "the refusal was not recorded for the status surface"
    assert "99" in chain.loop.blocked_reason
    assert _orders(app_db) == []
    assert _journal_rows(app_db) == []
    assert len(_dataset(app_db, tmp_path)) == 0


def test_the_runner_is_not_the_only_thing_that_can_write_a_forward_trade(chain, app_db):
    """The provenance is a property of the *order path*, not of the runner.

    A trade raised through the execution service by any caller — a future
    scheduler, a manual override, a different loop — is stamped the same way,
    because the stamp is written where the order is created.
    """
    from atr.execution.oms import PROVENANCE_FORWARD, PROVENANCE_KEY, OrderDraft

    chain.loop.bind()
    placed = chain.loop.execution.place(
        OrderDraft(
            user_id="u1",
            symbol=SYMBOL,
            side="BUY",
            quantity=1,
            mode="PAPER",
            exchange=EXCHANGE,
            order_type="MARKET",
            requested_price=chain.price,
            deployment_id=chain.deployment_id,
            strategy_id=chain.loop.row["strategy_id"],
            strategy_version=chain.loop.row["strategy_version"],
            tag="not-the-runner",
        )
    )
    raw = _raw(_events(app_db, placed.order_id)[0])
    assert raw.get(PROVENANCE_KEY) == PROVENANCE_FORWARD
