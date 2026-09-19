"""THE ACCEPTANCE CHAIN, AUTHORED OVER HTTP AND TRADED BY THE RUNNER.

    Create Strategy -> Create Version 1 -> Validate Version 1
      -> Create Paper Deployment (Version 1) -> Start Deployment
      -> live tick -> signal -> paper fill -> closed trade
      -> PAPER_FORWARD -> learning dataset

What makes this a test rather than a demonstration
--------------------------------------------------

**The authoring half goes over HTTP.** The strategy and its version are created
by ``POST /api/v1/strategies`` and ``POST /api/v1/strategies/{id}/versions``, not
by reaching into ``StrategyRepository``. That is the gap being closed: the pair a
deployment pins used to be creatable only by a script, so the chain started at a
row nobody could author. Every step before the tick is a request a browser makes.

**The trading half runs the real components.** ``PaperRunner.pass_once`` drives
the real rule layer (``eval_entry``/``eval_exit``), the real risk gate, the real
OMS and the real ``PaperVenue``. Nothing is stubbed except ``load_daily``, which
is the *history* the rules need for warmup — the test is about the live tick, not
about the parquet cache. The entry fires because a live tick genuinely satisfies
the stored breakout rule and the exit fires because the price genuinely breaches
the stored stop; both are consequences of the definition, not of an instruction.

**The definition is the shipped example.** ``atr.strategy.example`` is what a
fresh installation seeds, so if the example stops being deployable this test
fails. A worked example that does not work is worse than none, because it is the
first thing anyone tries.

The last link is the point of all of it: the closed trade arrives in the learning
dataset graded ``forward`` and classed ``PAPER_FORWARD``. That is the first
genuine forward observation this system can produce, and it is the only thing
that will ever let the learning engine say something about whether a rule still
works.
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
from atr.strategy.example import EXAMPLE_DEFINITION, EXAMPLE_NAME

SYMBOL = "RELIANCE"
EXCHANGE = "NSEEQ"
#: A Monday, 10:00 IST — inside the cash session. Aware on purpose: a naive
#: datetime is read in the machine's local zone, which would make the session gate
#: depend on where the test runs.
SESSION = datetime(2026, 9, 14, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
CAPITAL = 500_000.0
ORDER_VALUE = 200_000.0

BASE = "/api/v1/strategies"


def _daily(n: int = 300, start: float = 1000.0) -> pd.DataFrame:
    """A rising series with a volume surge on its last bar.

    Both halves of the example's breakout rule need to be true — within 2% of the
    20-bar high *and* on 1.5x average volume — so the entry is a consequence of
    the stored rule rather than of a number this test liked.
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


@pytest.fixture(autouse=True)
def _no_live_seams():
    """Leave the process-wide feed seams clear.

    A subscriber installed by another file would have this suite's runner reaching
    for a broadcaster that is not there. The subject here is the trading chain, not
    the feed wiring.
    """
    from atr.services import paper as paper_service

    paper_service.install_live_source(None)
    paper_service.install_live_subscriber(None)
    yield
    paper_service.install_live_source(None)
    paper_service.install_live_subscriber(None)


class Chain:
    """An authored, started deployment plus the live price it is evaluated against."""

    def __init__(self, runner, deployment, loop, prices, tick, strategy_id, version):
        self.runner = runner
        self.deployment = deployment
        self.deployment_id = deployment["deployment_id"]
        self.loop = loop
        self.prices = prices
        self.tick = tick
        self.strategy_id = strategy_id
        self.version = version

    def move(self, price: float) -> None:
        """Publish a new live tick."""
        self.prices[SYMBOL] = round(price, 2)

    @property
    def price(self) -> float:
        return self.prices[SYMBOL]


@pytest.fixture()
def chain(auth_client, app_db, monkeypatch) -> Chain:
    """Author over HTTP, deploy over HTTP, then hand the loop a live tick.

    Every step up to ``START`` is an HTTP request against the real app. The
    ``PaperRunner`` is built by hand only because the TestClient deliberately does
    not run the app's lifespan (which would warm the real breadth cache and start
    the real background task) — the loop object itself is the production one.
    """
    from atr.services import paper as paper_service
    from atr.services.runner import PaperRunner

    monkeypatch.setattr(
        "atr.signals.engine.load_daily",
        lambda symbol, exchange="NSEEQ", **kw: _daily(),
    )

    # --- Create Strategy ---------------------------------------------------
    created = auth_client.post(
        BASE,
        json={
            "name": EXAMPLE_NAME,
            "kind": "rules",
            "description": "acceptance chain",
            "engine_key": EXAMPLE_DEFINITION["engine_key"],
        },
    )
    assert created.status_code == 201, created.text
    strategy = created.json()
    assert strategy["latest_version"] is None, "a new strategy has no version"

    # --- Create Version 1 --------------------------------------------------
    version = auth_client.post(
        f"{BASE}/{strategy['strategy_id']}/versions",
        json={
            "definition": EXAMPLE_DEFINITION,
            "change_note": "acceptance chain: v1",
        },
    )
    assert version.status_code == 201, version.text
    version_body = version.json()
    assert version_body["version"] == 1

    # --- Validate Version 1 ------------------------------------------------
    report = auth_client.post(
        f"{BASE}/{strategy['strategy_id']}/validate", json={"version": 1}
    )
    assert report.status_code == 200, report.text
    report_body = report.json()
    assert report_body["ok"] is True, report_body
    assert report_body["paper"]["resolvable"] is True
    assert report_body["statistical_validation"]["performed"] is False

    # --- Create Paper Deployment using Version 1 ---------------------------
    deployment = auth_client.post(
        "/api/v1/paper/deployments",
        json={
            "strategy_id": strategy["strategy_id"],
            "strategy_version": 1,
            "capital": CAPITAL,
            "mode": "PAPER",
            "config": {
                "symbols": [SYMBOL],
                "exchange": EXCHANGE,
                "order_value": ORDER_VALUE,
                "lookback_days": 400,
                "max_open_positions": 1,
            },
        },
    )
    assert deployment.status_code == 201, deployment.text
    deployment_body = deployment.json()
    assert deployment_body["status"] == "PENDING"
    assert deployment_body["strategy_version"] == 1

    # --- Start Deployment --------------------------------------------------
    started = auth_client.post(
        f"/api/v1/paper/deployments/{deployment_body['deployment_id']}/start"
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "RUNNING"

    # --- the live tick the loop will see ----------------------------------
    frame = _daily()
    high_20 = float(frame["high"].shift(1).tail(20).max())
    tick = round(high_20 * 0.995, 2)

    prices = {SYMBOL: tick}
    # The live tick, through the seam the API wires at startup — not a patch on
    # the venue. ``default_price_source`` reads the live source *first* and falls
    # back to the daily cache only when it returns nothing, so a fill priced here
    # is a fill priced off the tick; and because the fallback stays reachable, the
    # assertion that the fill is *not* at the cached close is a real assertion.
    #
    # Installed *before* the runner is built, deliberately: the venue captures
    # whatever source exists when it is first constructed, so installing after
    # would leave it reading a source that never prices.
    paper_service.install_live_source(
        lambda symbol, exchange: prices.get(symbol.upper())
    )

    runner = PaperRunner(db=app_db)
    runner.sync_loops()
    loop = runner._loops[deployment_body["deployment_id"]]
    assert loop.venue.prices(SYMBOL, EXCHANGE) == tick, (
        "the venue is not reading the live tick, so nothing below would be a "
        "live-price result"
    )

    return Chain(
        runner,
        deployment_body,
        loop,
        prices,
        tick,
        strategy["strategy_id"],
        version_body,
    )


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

    # A tick through the stored 5% stop. The exit rule is the stored one, so the
    # close is a consequence of the price, not of an instruction.
    chain.move(chain.price * 0.94)
    closing = chain.runner.pass_once(now=SESSION + timedelta(minutes=5))
    assert closing.orders == 1, f"the exit leg did not trade: {closing}"


# ---------------------------------------------------------------------------
# 1. the whole chain, in one test
# ---------------------------------------------------------------------------
def test_the_acceptance_chain_end_to_end(chain, auth_client, app_db, tmp_path):
    """Every link, in order, from an HTTP request to a graded forward observation."""
    # --- the strategy version the deployment pinned is the one that runs ----
    stored = auth_client.get(
        f"{BASE}/{chain.strategy_id}/versions/{chain.version['version']}"
    ).json()
    assert stored["definition_hash"] == chain.version["definition_hash"]
    assert chain.loop.row["strategy_version"] == 1
    assert chain.loop.blocked_reason is None, chain.loop.blocked_reason

    # --- LIVE TICK ---------------------------------------------------------
    assert chain.price > 0

    # --- STRATEGY SIGNAL -> RISK -> OMS -> PAPER FILL ---------------------
    _open_then_close(chain)

    # --- CLOSED TRADE ------------------------------------------------------
    assert chain.loop._portfolio().position(SYMBOL).quantity == 0
    journal = _journal_rows(app_db)
    assert len(journal) == 1, "the round trip did not produce exactly one episode"
    assert journal[0]["exit_ts"] is not None, "the episode was never closed"

    # --- PAPER_FORWARD -----------------------------------------------------
    assert journal[0]["evidence_grade"] == GRADE_FORWARD

    # --- LEARNING DATASET --------------------------------------------------
    dataset = _dataset(app_db, tmp_path)
    assert len(dataset) == 1, "the closed trade did not reach the dataset"

    row = dataset.rows[0]
    assert row["evidence_grade"] == GRADE_FORWARD
    assert row["evidence_class"] == CLASS_PAPER_FORWARD
    assert row["is_forward"] is True
    assert row["source"] == "PAPER"
    assert row["source_ref"] == chain.deployment_id
    assert dataset.grade_counts() == {GRADE_FORWARD: 1, GRADE_IN_SAMPLE: 0}

    summary = dataset.summary()
    assert summary["forward_observations"] == 1
    assert summary["in_sample_observations"] == 0
    assert summary["latest_forward_ts"] is not None


def test_the_forward_row_is_attributed_to_the_authored_version(chain, app_db, tmp_path):
    """Attribution, not just presence: the row names the version HTTP created."""
    _open_then_close(chain)
    row = _dataset(app_db, tmp_path).rows[0]

    assert row["strategy_id"] == chain.strategy_id
    assert row["strategy_version"] == 1
    assert row["entry_ts"] is not None and row["exit_ts"] is not None
    assert row["exit_ts"] >= row["entry_ts"]
    assert row["quantity"] > 0
    assert row["return_pct"] < 0, "the stop-loss leg lost money"
    assert row["net_pnl"] < row["gross_pnl"], "costs were not deducted"


def test_the_learning_service_reports_it_without_being_told(chain, app_db, tmp_path):
    """The learning pipeline reads the journal the runner wrote; nothing points it there."""
    from atr.services.learning import LearningService

    service = LearningService(db=app_db, cache_root=tmp_path)
    before = service.status()
    assert before["counts"]["trade_journal"] == 0
    assert before["forward_available"] == 0

    _open_then_close(chain)

    after = service.status()
    assert after["counts"]["trade_journal"] == 1
    assert after["counts"]["trade_journal_forward"] == 1
    assert after["forward_available"] == 1
    # One forward observation is evidence and still not a claim, and the status
    # keeps those two questions apart.
    assert after["sufficient_for_a_claim"] is False
    assert "1 of them forward evidence" in after["note"]


# ---------------------------------------------------------------------------
# 2. the links that could quietly not be the real thing
# ---------------------------------------------------------------------------
def test_the_order_lifecycle_is_the_oms_state_machine(chain, app_db):
    """RISK -> OMS in order, with the source of every transition recorded.

    Asserted rather than inferred from the trade: a fill that happened to be small
    enough to pass the limits looks identical to one that was measured against
    them.
    """
    _open_then_close(chain)
    orders = _orders(app_db)
    assert len(orders) == 2, "one order for the entry and one for the exit"

    for order in orders:
        events = _events(app_db, order["order_id"])
        assert [e["to_status"] for e in events] == [
            "NEW",
            "VALIDATING",
            "RISK_APPROVED",
            "SUBMITTED",
            "ACKNOWLEDGED",
            "FILLED",
        ]
        risk = [e for e in events if e["to_status"] == "RISK_APPROVED"]
        assert risk and risk[0]["source"] == "risk"
        assert events[0]["source"] == "oms"
        assert all(_raw(e).get("venue") == "paper" for e in events if e.get("filled_qty"))


def test_the_venue_priced_the_fill_off_the_live_tick(chain, app_db):
    """The fill is at the tick, not at yesterday's daily close."""
    entry_tick = chain.price
    _open_then_close(chain)

    buy = [o for o in _orders(app_db) if o["side"] == "BUY"][0]
    assert float(buy["requested_price"]) == pytest.approx(entry_tick)
    assert float(buy["avg_fill_price"]) == pytest.approx(entry_tick, rel=0.01)
    # The cached last close, which a day-late system would have filled at.
    assert float(buy["avg_fill_price"]) != pytest.approx(1299.0, rel=0.001)


def test_the_entry_order_carries_the_execution_time_provenance_stamp(chain, app_db):
    """The stamp the journal copies and the dataset grades on, at its source.

    A timestamp cannot do this job — a backfill harness writes those too — which is
    why the OMS writes the provenance on the order's ``NEW`` event at creation.
    """
    from atr.execution.oms import PROVENANCE_FORWARD, PROVENANCE_KEY, RECORDED_AT_KEY

    chain.runner.pass_once(now=SESSION)
    buy = [o for o in _orders(app_db) if o["side"] == "BUY"][0]
    raw = _raw(_events(app_db, buy["order_id"])[0])

    assert raw.get(PROVENANCE_KEY) == PROVENANCE_FORWARD
    assert raw.get(RECORDED_AT_KEY), "the instant the claim was made must be recorded"
    assert "breakout" in (raw.get("reason") or ""), (
        "the rule's own words travel with the order, which is what makes it "
        "attributable to a decision rather than only to a symbol"
    )


def test_a_closed_market_produces_no_trade_and_no_evidence(chain, app_db, tmp_path):
    """The converse, so a passing chain cannot be an artefact of the harness.

    A paper fill at 20:15 IST is a fill at a price no exchange offered, and it
    would arrive in the dataset as forward evidence — worse than not trading.
    """
    tick = chain.runner.pass_once(
        now=datetime(2026, 9, 14, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    )

    assert tick.signals == 0 and tick.orders == 0
    assert _orders(app_db) == []
    assert _journal_rows(app_db) == []
    assert len(_dataset(app_db, tmp_path)) == 0


def test_the_seeded_example_is_the_same_definition_the_chain_authored(
    chain, auth_client
):
    """The shipped example and the authored version are one thing, not two.

    If they drifted, the test above would be proving that *some* definition works
    while a fresh install seeded a different one.
    """
    seeded = auth_client.post(f"{BASE}/seed", params={"name": "Example breakout copy"})
    assert seeded.status_code == 201, seeded.text
    body = seeded.json()
    assert body["created"] is True
    assert body["version"]["deployable"] is True
    # Identical rules produce an identical canonical hash, which is the same
    # property the store uses to refuse a duplicate version.
    assert body["version"]["definition_hash"] == chain.version["definition_hash"]
