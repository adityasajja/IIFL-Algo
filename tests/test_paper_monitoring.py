"""The monitoring surface, and the pipeline it reports on.

What this suite protects
------------------------

The runner's job is a chain: live price → signal → risk → OMS order → paper fill
→ position → P&L. The monitoring screen's job is to tell the truth about that
chain. Two things can go wrong and only one of them is loud:

* Something in the chain breaks. Orders stop appearing. Loud, and the existing
  runner tests catch it.
* The chain runs perfectly and the screen reports it wrongly — or reports a
  *different* chain because it re-derived the state from a second source. Quiet,
  and this is the failure mode that makes an operator trust a number that is not
  the number.

So the tests below pin both. The pipeline tests drive the real ``PaperRunner``
through the real risk gate, the real OMS and the real ``PaperVenue``, and then
assert that what monitoring reports matches what actually happened.

Three specific honesty properties are pinned, because each is a place where the
easy implementation lies:

* ``today_pnl`` is ``None``, not ``0.0``, when there is no earlier equity to
  subtract. A first day's gain reported as nothing is a wrong number, not a
  missing one.
* a deployment that is ``RUNNING`` but cannot trade says so, via
  ``not_trading_because``. "Running" and "trading" are different claims.
* the timeline explains *why* a signal became an order. An order log that shows
  only fills is a record of what happened with no account of why.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

SYMBOL = "RELIANCE"
EXCHANGE = "NSEEQ"
#: A Monday, 10:00 IST — inside the cash session. Aware on purpose: a naive
#: datetime is interpreted as the machine's local zone, which would make the
#: session gate depend on where the test happens to run.
SESSION = datetime(2026, 9, 14, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def _daily(n: int = 300, start: float = 1000.0, *, ramp: bool = False) -> pd.DataFrame:
    """A daily frame long enough for every indicator to warm up.

    ``ramp`` makes the series rise steadily and puts a volume surge on the last
    bar. Both are needed to trigger the breakout rule, which requires the price to
    approach a 63-bar high *and* volume to clear a multiple of its average — real
    conditions, not test scaffolding. A constant-volume series satisfies neither,
    and would make the pipeline tests pass or fail for reasons unrelated to the
    code under test.
    """
    index = pd.date_range("2025-01-01", periods=n, freq="D")
    closes = [start + i for i in range(n)] if ramp else [start] * n
    volumes = [500_000.0] * n
    if ramp:
        # The live tick is appended as a *new* bar, so the surge has to be on the
        # appended one. ``append_live_bar`` carries the last row's volume forward.
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


@pytest.fixture()
def user(app_db):
    from atr.appdb.schema import users

    with app_db.session() as session:
        session.execute(
            users.insert().values(
                user_id="u1",
                email="m@example.com",
                username="monitor",
                display_name="M",
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


def _strategy(app_db, *, definition: dict, name: str = "Monitor rules") -> tuple[str, int]:
    from atr.appdb.repositories import StrategyRepository

    with app_db.session() as session:
        strategy = StrategyRepository.create(
            session, user_id="u1", name=name, kind="rules"
        )
        version = StrategyRepository.create_version(
            session,
            strategy_id=strategy["strategy_id"],
            author_user_id="u1",
            definition=definition,
        )
    return strategy["strategy_id"], int(version["version"])


#: A breakout entry, so a live tick can be chosen that actually triggers it.
BREAKOUT = {
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


def _deploy(app_db, *, strategy_id: str, version: int, capital: float = 500_000.0,
            config: dict | None = None, status: str = "RUNNING") -> dict:
    from atr.appdb.repositories import DeploymentRepository

    payload = {
        "symbols": [SYMBOL],
        "exchange": EXCHANGE,
        "timeframe": "1d",
        "order_value": 250_000.0,
        "lookback_days": 400,
        "max_open_positions": 1,
    }
    payload.update(config or {})
    with app_db.session() as session:
        return DeploymentRepository.create(
            session,
            user_id="u1",
            strategy_id=strategy_id,
            strategy_version=version,
            mode="PAPER",
            capital=capital,
            status=status,
            config=payload,
        )


@pytest.fixture()
def wired(app_db, user, monkeypatch):
    """A runner over a rising series, with the live price wired to a breakout.

    Returns ``(runner, deployment_id, tick_price)``. The price is computed from
    the seeded history so the entry is a consequence of the stored rule rather
    than of a number the test liked.
    """
    monkeypatch.setattr(
        "atr.signals.engine.load_daily",
        lambda symbol, exchange="NSEEQ", **kw: _daily(ramp=True),
    )
    from atr.services.runner import PaperRunner

    strategy_id, version = _strategy(app_db, definition=BREAKOUT)
    row = _deploy(app_db, strategy_id=strategy_id, version=version)

    frame = _daily(ramp=True)
    high_63 = float(frame["close"].tail(63).max())
    tick = round(high_63 * 0.995, 2)

    runner = PaperRunner(db=app_db)
    runner.sync_loops()
    loop = runner._loops[row["deployment_id"]]
    loop.venue.prices = lambda s, e: tick
    return runner, row["deployment_id"], tick


@pytest.fixture()
def monitor(app_db, user, wired):
    """A monitor pointed at the runner the test is actually driving.

    ``DeploymentMonitor`` defaults to the process-wide runner, which is right in
    production and wrong in a test: a test that starts its own runner would
    otherwise have its monitor report on a second runner that never saw the
    deployment, and every ``trading``/``blocked_reason`` assertion would be
    about the wrong process.

    Depends on ``user`` explicitly: the strategies table has a foreign key onto
    ``users``, and a fixture that only asked for ``app_db`` would race the user
    insert and fail on the FK rather than on anything real.
    """
    from atr.services.monitoring import DeploymentMonitor

    runner, _deployment_id, _tick = wired
    return DeploymentMonitor(db=app_db, runner=runner)


@pytest.fixture()
def idle_monitor(app_db, user):
    """A monitor with no runner of its own, for deployments nothing is driving."""
    from atr.services.monitoring import DeploymentMonitor

    return DeploymentMonitor(db=app_db)


# ---------------------------------------------------------------------------
# the pipeline: live tick -> signal -> risk -> OMS -> fill -> position
# ---------------------------------------------------------------------------
def test_a_live_tick_produces_a_signal_and_a_filled_order(app_db, wired, monitor):
    """The whole chain, in one pass, through the real components."""
    runner, deployment_id, tick = wired
    tick_result = runner.pass_once(now=SESSION)

    assert tick_result.signals == 1, f"no signal fired: {runner._loops[deployment_id].last_pass}"
    assert tick_result.orders == 1

    orders = monitor.orders("u1", deployment_id)
    fills = monitor.fills("u1", deployment_id)
    signals = monitor.signals("u1", deployment_id)

    assert len(orders) == 1, orders
    assert len(fills) == 1, fills
    assert len(signals) == 1, signals

    order = orders[0]
    assert order["status"] == "FILLED", order["status"]
    assert order["filled_quantity"] > 0
    assert order["avg_fill_price"] is not None
    # The fill is priced off the live tick plus slippage, so it cannot be below
    # the tick for a BUY.
    assert float(order["avg_fill_price"]) >= tick


def test_the_signal_records_why_it_became_an_order(app_db, wired, monitor):
    """An order with no stated reason is a record nobody can explain later.

    The rule's own sentence must survive to the order's ``NEW`` event, because
    that event is the only place a monitoring screen can read the causal chain
    from.
    """
    runner, deployment_id, _ = wired
    runner.pass_once(now=SESSION)

    signals = monitor.signals("u1", deployment_id)
    assert signals, "no signal recorded"
    reason = signals[0]["reason"]
    assert reason, "the signal carried no reason"
    assert "breakout" in reason.lower(), reason

    timeline = monitor.timeline("u1", deployment_id)
    first = next(e for e in timeline if e.stage == "order")
    assert "breakout" in first.summary.lower(), first.summary


def test_the_risk_decision_is_visible_in_the_timeline(app_db, wired, monitor):
    """Signal → Risk → Order → Fill → Position must all be present and ordered."""
    runner, deployment_id, _ = wired
    runner.pass_once(now=SESSION)

    stages = [e.stage for e in monitor.timeline("u1", deployment_id)]
    for expected in ("risk", "order", "fill", "position"):
        assert expected in stages, f"{expected} missing from {stages}"
    assert stages.index("risk") < stages.index("fill")
    assert stages.index("order") < stages.index("position")


def test_a_position_and_its_pnl_follow_from_the_fill(app_db, wired, monitor):
    """The fold must agree with itself: equity == cash + market value."""
    runner, deployment_id, tick = wired
    runner.pass_once(now=SESSION)

    pnl = monitor.pnl("u1", deployment_id, prices={SYMBOL: tick})
    assert pnl["positions"], "the fill produced no position"
    position = pnl["positions"][0]
    assert abs(float(position["quantity"])) > 0
    assert abs(pnl["equity"] - (pnl["cash"] + pnl["market_value"])) < 1.0
    assert pnl["capital"] == 500_000.0
    assert pnl["complete"] is True


def test_a_rejected_order_is_reported_as_rejected_not_as_a_trade(
    app_db, wired, monitor
):
    """A risk rejection is an outcome, and the screen must show it as one.

    The gate is closed by a kill switch, which is the cheapest way to make the
    real ``RiskEngine`` say no without weakening it.
    """
    runner, deployment_id, _ = wired

    from atr.services.risk import RiskStateService

    RiskStateService(db=app_db).set_kill_switch(
        True, reason="test: gate closed", actor="test"
    )

    runner.pass_once(now=SESSION)

    orders = monitor.orders("u1", deployment_id)
    assert orders, "an order row should still exist — the refusal is recorded"
    assert orders[0]["status"] == "REJECTED", orders[0]["status"]
    assert not monitor.fills("u1", deployment_id), "a rejected order must not fill"

    timeline = monitor.timeline("u1", deployment_id)
    rejected = [e for e in timeline if e.outcome == "rejected"]
    assert rejected, [e.summary for e in timeline]
    assert "risk" in rejected[0].summary.lower() or "reject" in rejected[0].summary.lower()


# ---------------------------------------------------------------------------
# honest reporting
# ---------------------------------------------------------------------------
def test_todays_pnl_is_null_when_there_is_no_earlier_equity(app_db, wired, monitor):
    """The distinction between "no baseline" and "flat".

    A deployment whose whole history is today has no previous equity to subtract.
    Returning ``0.0`` would report a day's gain as nothing, which is a wrong
    number rather than a missing one.
    """
    runner, deployment_id, tick = wired
    runner.pass_once(now=SESSION)

    pnl = monitor.pnl("u1", deployment_id, prices={SYMBOL: tick})
    # Every fill happened today, so there is no boundary equity.
    assert pnl["today_pnl"] is None, pnl["today_pnl"]
    assert pnl["today_since"] is None
    # The lifetime figure is still real and still reported.
    assert pnl["total_pct"] is not None


def test_todays_pnl_appears_once_a_prior_day_has_equity(app_db, wired, monitor):
    """With a fill dated yesterday, today's figure is a real difference."""
    runner, deployment_id, tick = wired
    runner.pass_once(now=SESSION)

    # Backdate the fills to yesterday, so the boundary has something before it.
    from atr.appdb.schema import order_events

    yesterday = datetime.now(UTC) - timedelta(days=1)
    with app_db.session() as session:
        session.execute(
            order_events.update().values(ts=yesterday, fill_ts=yesterday)
        )

    pnl = monitor.pnl("u1", deployment_id, prices={SYMBOL: tick})
    assert pnl["today_pnl"] is not None, "a boundary existed but today's P&L was null"
    assert pnl["today_since"] is not None


def test_status_separates_running_from_trading(app_db, idle_monitor):
    """``RUNNING`` is a lifecycle state, not a claim that orders will be placed."""
    strategy_id, version = _strategy(app_db, definition=BREAKOUT)
    row = _deploy(app_db, strategy_id=strategy_id, version=version, status="PENDING")

    status = idle_monitor.status("u1", row["deployment_id"])
    assert status["status"] == "PENDING"
    assert status["trading"] is False
    assert status["not_trading_because"], "the reason must be stated"
    assert "PENDING" in status["not_trading_because"]


def test_a_blocked_deployment_says_why_it_is_not_trading(app_db, user, monkeypatch):
    """The runner's ``blocked_reason`` must reach the status surface.

    This is the field that distinguishes "the market is quiet" from "this
    deployment is silently inert", and it has no other way to be seen.
    """
    from atr.services.monitoring import DeploymentMonitor
    from atr.services.runner import PaperRunner

    monkeypatch.setattr(
        "atr.signals.engine.load_daily",
        lambda symbol, exchange="NSEEQ", **kw: _daily(),
    )
    # A version whose rules cannot be parsed into usable numeric values.
    strategy_id, version = _strategy(
        app_db,
        name="Broken rules",
        definition={"rules": {"entry": {"breakout_lookback": "not-a-number"}}},
    )
    row = _deploy(app_db, strategy_id=strategy_id, version=version)

    runner = PaperRunner(db=app_db)
    runner.sync_loops()
    loop = runner._loops.get(row["deployment_id"])
    assert loop is not None
    assert loop._rules() is None, "a malformed version must resolve to None"
    assert loop.blocked_reason

    # The monitor must be pointed at this runner: the default is the process-wide
    # one, which knows nothing about a runner a caller built for itself.
    monitor = DeploymentMonitor(db=app_db, runner=runner)
    status = monitor.status("u1", row["deployment_id"])
    assert status["trading"] is False
    assert status["blocked_reason"], status
    assert status["blocked_reason"] in status["not_trading_because"]


def test_monitoring_is_owner_scoped(app_db, idle_monitor):
    """Another user's deployment must be indistinguishable from one that is absent."""
    strategy_id, version = _strategy(app_db, definition=BREAKOUT)
    row = _deploy(app_db, strategy_id=strategy_id, version=version)

    from atr.services.monitoring import MonitoringError

    with pytest.raises(MonitoringError) as caught:
        idle_monitor.status("someone-else", row["deployment_id"])
    assert caught.value.status == 404
    assert caught.value.code == "not_found"


def test_the_overview_is_one_consistent_read(app_db, wired, monitor):
    """Every view must be of the same state, and it must be serialisable."""
    runner, deployment_id, tick = wired
    runner.pass_once(now=SESSION)

    overview = monitor.overview("u1", deployment_id)
    for key in (
        "status", "pnl", "positions", "orders", "fills",
        "signals", "trades", "risk", "timeline",
    ):
        assert key in overview, f"{key} missing from overview"

    # A payload the API cannot encode is a payload the screen cannot show.
    blob = json.dumps(overview, default=str)
    assert len(blob) > 100

    # Risk limits are data, not a dataclass repr.
    assert not isinstance(overview["risk"]["limits"], str)


def test_a_deployment_with_no_activity_reports_empty_not_fabricated(app_db, idle_monitor):
    """An idle deployment must show zeroes and empty lists, never invented rows."""
    strategy_id, version = _strategy(app_db, definition=BREAKOUT)
    row = _deploy(app_db, strategy_id=strategy_id, version=version)

    assert idle_monitor.orders("u1", row["deployment_id"]) == []
    assert idle_monitor.fills("u1", row["deployment_id"]) == []
    assert idle_monitor.signals("u1", row["deployment_id"]) == []
    assert idle_monitor.timeline("u1", row["deployment_id"]) == []

    trades = idle_monitor.trades("u1", row["deployment_id"])
    # The journal has no writer yet, and this read says so rather than
    # reconstructing episodes from fills and calling the guess a record.
    assert trades["trades"] == []
    assert trades["open_count"] == 0

    pnl = idle_monitor.pnl("u1", row["deployment_id"])
    assert pnl["equity"] == pytest.approx(row["capital"])
    assert pnl["positions"] == []


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def test_pause_and_stop_detach_the_loop(app_db, wired):
    """Pause and stop are DB-driven: the runner drops the loop on the next pass."""
    from atr.appdb.repositories import DeploymentRepository

    runner, deployment_id, _ = wired
    assert deployment_id in runner._loops

    with app_db.session() as session:
        DeploymentRepository.pause(session, deployment_id, "u1", reason="test")
    runner.sync_loops()
    assert deployment_id not in runner._loops

    with app_db.session() as session:
        DeploymentRepository.stop(session, deployment_id, "u1", reason="test")
    runner.sync_loops()
    assert deployment_id not in runner._loops


def test_stop_is_terminal_and_reset_replaces_rather_than_revives(app_db, wired):
    """``STOPPED`` cannot be restarted, so RESET creates a new deployment.

    reviving it would leave "why is this still trading?" with no answer. The old
    row must remain readable, because ``order_events`` is append-only.
    """
    from atr.appdb.repositories import DeploymentRepository
    from atr.services.paper import DeploymentService

    runner, deployment_id, _ = wired
    with app_db.session() as session:
        DeploymentRepository.stop(session, deployment_id, "u1", reason="done")

    with app_db.session() as session:
        assert DeploymentRepository.start(session, deployment_id, "u1") == 0

    replacement = DeploymentService(db=app_db).reset(
        "u1", deployment_id, reason="fresh account"
    )
    assert replacement["deployment_id"] != deployment_id
    assert replacement["reset_from"] == deployment_id
    assert float(replacement["capital"]) == 500_000.0

    with app_db.session() as session:
        old = DeploymentRepository.get(session, deployment_id, "u1")
    assert old is not None and old["status"] == "STOPPED"


def test_a_running_deployment_is_recovered_after_a_restart(app_db, wired):
    """A fresh runner must re-attach every deployment the database says is running.

    This is requirement 6 — persistence across an application restart — and the
    mechanism is that the *database* is the source of truth, not the process.
    """
    from atr.services.runner import PaperRunner

    _first, deployment_id, _tick = wired
    assert deployment_id in _first._loops

    # Simulate a restart: a brand new runner, no loops, same database.
    revived = PaperRunner(db=app_db)
    assert revived._loops == {}
    revived.sync_loops()

    assert deployment_id in revived._loops, "the deployment was not recovered"
    loop = revived._loops[deployment_id]
    # And it must recover the *rules*, not just the row: a loop that attaches
    # without resolvable rules is a deployment that looks recovered and trades
    # nothing.
    resolved = revived.resolve_rules(loop)
    assert resolved is not None, loop.blocked_reason
    assert float(resolved[1].stop_loss_pct) == 3.0


# ---------------------------------------------------------------------------
# the trade journal
# ---------------------------------------------------------------------------
def test_the_journal_opens_an_episode_from_the_fold(app_db, wired):
    """A position appearing in the fold must open exactly one episode.

    The journal is a projection of the position fold rather than a second record
    of trades — two records can disagree, and a journal that disagrees with the
    account is worse than none because it looks like evidence.
    """
    runner, deployment_id, _ = wired
    # ``pass_once`` reconciles the journal itself, so the episode exists by the
    # time this returns — that is the point of the hook. A second reconcile must
    # then find nothing to do rather than open a duplicate.
    runner.pass_once(now=SESSION)

    from atr.services.journal import TradeJournalService

    result = TradeJournalService(db=app_db).reconcile("u1", deployment_id)
    assert result["opened"] == [], result
    assert result["open_now"] == 1, result

    from atr.appdb.repositories import TradeJournalRepository

    with app_db.session() as session:
        rows, total = TradeJournalRepository.list_for_user(
            session, "u1", deployment_id=deployment_id
        )
    assert total == 1, rows
    row = rows[0]
    assert row["symbol"] == SYMBOL
    assert row["side"] == "BUY"
    assert row["quantity"] > 0
    assert row["entry_price"] > 0
    assert row["exit_ts"] is None
    # Attribution and the reason survive into the journal, which is what makes a
    # trade explainable a week later.
    assert row["strategy_id"]
    assert row["signal_reason"] and "breakout" in row["signal_reason"].lower()


def test_reconciling_is_idempotent(app_db, wired):
    """Running it twice must not open a second episode for the same position."""
    runner, deployment_id, _ = wired
    runner.pass_once(now=SESSION)

    from atr.services.journal import TradeJournalService

    from atr.services.journal import TradeJournalService

    service = TradeJournalService(db=app_db)
    for _ in range(3):
        result = service.reconcile("u1", deployment_id)
        # Never a second episode for one position, however many times it runs.
        assert result["opened"] == [], result
        assert result["open_now"] == 1, result

    from atr.appdb.repositories import TradeJournalRepository

    with app_db.session() as session:
        rows, total = TradeJournalRepository.list_for_user(
            session, "u1", deployment_id=deployment_id
        )
    assert total == 1, f"{total} episodes for one position"


def test_the_journal_closes_an_episode_when_the_book_goes_flat(app_db, wired):
    """Closing must record exit, P&L and duration rather than leaving it open."""
    runner, deployment_id, tick = wired
    runner.pass_once(now=SESSION)

    from atr.services.journal import TradeJournalService

    journal = TradeJournalService(db=app_db)
    journal.reconcile("u1", deployment_id)

    # Flatten through the same pipeline, at a gain.
    from atr.execution.oms import OrderDraft
    from atr.services import paper as paper_service
    from atr.services.execution import ExecutionService
    from atr.services.orders import get_order_service
    from atr.services.paper import PaperLedger

    ledger = PaperLedger(db=app_db)
    portfolio = ledger.portfolio("u1", deployment_id=deployment_id)
    quantity = abs(float(portfolio.position(SYMBOL).quantity))
    assert quantity > 0

    exit_price = round(tick * 1.02, 2)
    ExecutionService(
        orders=get_order_service(portfolio=portfolio),
        venue=paper_service.paper_venue(lambda s, e: exit_price),
    ).place(
        OrderDraft(
            user_id="u1", symbol=SYMBOL, side="SELL", quantity=quantity,
            mode="PAPER", exchange=EXCHANGE, requested_price=exit_price,
            deployment_id=deployment_id,
        )
    )

    result = journal.reconcile("u1", deployment_id)
    assert len(result["closed"]) == 1, result

    from atr.appdb.repositories import TradeJournalRepository

    with app_db.session() as session:
        rows, _ = TradeJournalRepository.list_for_user(
            session, "u1", deployment_id=deployment_id, closed_only=True
        )
    assert len(rows) == 1
    closed = rows[0]
    assert closed["exit_ts"] is not None
    # The journal records the price that actually filled, not the quote asked of
    # the venue: a paper fill carries slippage, and restating it as the requested
    # price would understate every cost in the book by the slippage it ignored.
    assert closed["exit_price"] == pytest.approx(exit_price, rel=0.01)
    assert closed["exit_price"] != exit_price
    # The exit is above the entry, so the episode is a gain — and net is below
    # gross by the commission, which is the only pair worth showing together.
    assert closed["gross_pnl"] > 0
    assert closed["net_pnl"] < closed["gross_pnl"]
    assert closed["mfe"] is not None
    assert closed["mae"] is not None


def test_the_journal_is_owner_scoped(app_db, wired):
    """One user's journal must not be visible to another."""
    runner, deployment_id, _ = wired
    runner.pass_once(now=SESSION)

    from atr.appdb.repositories import TradeJournalRepository
    from atr.services.journal import TradeJournalService

    TradeJournalService(db=app_db).reconcile("u1", deployment_id)

    with app_db.session() as session:
        rows, total = TradeJournalRepository.list_for_user(
            session, "not-the-owner", deployment_id=deployment_id
        )
    assert total == 0
    assert rows == []


# ---------------------------------------------------------------------------
# the trade journal
# ---------------------------------------------------------------------------
def test_the_journal_opens_an_episode_from_the_fold(app_db, wired):
    """A position appearing in the fold must open exactly one episode.

    The journal is a projection of the position fold rather than a second record
    of trades — two records can disagree, and a journal that disagrees with the
    account is worse than none because it looks like evidence.
    """
    runner, deployment_id, _ = wired
    # ``pass_once`` reconciles the journal itself, so the episode exists by the
    # time this returns — that is the point of the hook. A second reconcile must
    # then find nothing to do rather than open a duplicate.
    runner.pass_once(now=SESSION)

    from atr.services.journal import TradeJournalService

    result = TradeJournalService(db=app_db).reconcile("u1", deployment_id)
    assert result["opened"] == [], result
    assert result["open_now"] == 1, result

    from atr.appdb.repositories import TradeJournalRepository

    with app_db.session() as session:
        rows, total = TradeJournalRepository.list_for_user(
            session, "u1", deployment_id=deployment_id
        )
    assert total == 1, rows
    row = rows[0]
    assert row["symbol"] == SYMBOL
    assert row["side"] == "BUY"
    assert row["quantity"] > 0
    assert row["entry_price"] > 0
    assert row["exit_ts"] is None
    # Attribution and the reason survive into the journal, which is what makes a
    # trade explainable a week later.
    assert row["strategy_id"]
    assert row["signal_reason"] and "breakout" in row["signal_reason"].lower()


def test_reconciling_is_idempotent(app_db, wired):
    """Running it twice must not open a second episode for the same position."""
    runner, deployment_id, _ = wired
    runner.pass_once(now=SESSION)

    from atr.services.journal import TradeJournalService

    from atr.services.journal import TradeJournalService

    service = TradeJournalService(db=app_db)
    for _ in range(3):
        result = service.reconcile("u1", deployment_id)
        # Never a second episode for one position, however many times it runs.
        assert result["opened"] == [], result
        assert result["open_now"] == 1, result

    from atr.appdb.repositories import TradeJournalRepository

    with app_db.session() as session:
        rows, total = TradeJournalRepository.list_for_user(
            session, "u1", deployment_id=deployment_id
        )
    assert total == 1, f"{total} episodes for one position"


def test_the_journal_closes_an_episode_when_the_book_goes_flat(app_db, wired):
    """Closing must record exit, P&L and duration rather than leaving it open."""
    runner, deployment_id, tick = wired
    runner.pass_once(now=SESSION)

    from atr.services.journal import TradeJournalService

    journal = TradeJournalService(db=app_db)
    journal.reconcile("u1", deployment_id)

    # Flatten through the same pipeline, at a gain.
    from atr.execution.oms import OrderDraft
    from atr.services import paper as paper_service
    from atr.services.execution import ExecutionService
    from atr.services.orders import get_order_service
    from atr.services.paper import PaperLedger

    ledger = PaperLedger(db=app_db)
    portfolio = ledger.portfolio("u1", deployment_id=deployment_id)
    quantity = abs(float(portfolio.position(SYMBOL).quantity))
    assert quantity > 0

    exit_price = round(tick * 1.02, 2)
    ExecutionService(
        orders=get_order_service(portfolio=portfolio),
        venue=paper_service.paper_venue(lambda s, e: exit_price),
    ).place(
        OrderDraft(
            user_id="u1", symbol=SYMBOL, side="SELL", quantity=quantity,
            mode="PAPER", exchange=EXCHANGE, requested_price=exit_price,
            deployment_id=deployment_id,
        )
    )

    result = journal.reconcile("u1", deployment_id)
    assert len(result["closed"]) == 1, result

    from atr.appdb.repositories import TradeJournalRepository

    with app_db.session() as session:
        rows, _ = TradeJournalRepository.list_for_user(
            session, "u1", deployment_id=deployment_id, closed_only=True
        )
    assert len(rows) == 1
    closed = rows[0]
    assert closed["exit_ts"] is not None
    # The journal records the price that actually filled, not the quote asked of
    # the venue: a paper fill carries slippage, and restating it as the requested
    # price would understate every cost in the book by the slippage it ignored.
    assert closed["exit_price"] == pytest.approx(exit_price, rel=0.01)
    assert closed["exit_price"] != exit_price
    # The exit is above the entry, so the episode is a gain — and net is below
    # gross by the commission, which is the only pair worth showing together.
    assert closed["gross_pnl"] > 0
    assert closed["net_pnl"] < closed["gross_pnl"]
    assert closed["mfe"] is not None
    assert closed["mae"] is not None


def test_the_journal_is_owner_scoped(app_db, wired):
    """One user's journal must not be visible to another."""
    runner, deployment_id, _ = wired
    runner.pass_once(now=SESSION)

    from atr.appdb.repositories import TradeJournalRepository
    from atr.services.journal import TradeJournalService

    TradeJournalService(db=app_db).reconcile("u1", deployment_id)

    with app_db.session() as session:
        rows, total = TradeJournalRepository.list_for_user(
            session, "not-the-owner", deployment_id=deployment_id
        )
    assert total == 0
    assert rows == []
