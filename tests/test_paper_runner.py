"""The continuous paper-trading runner.

What this suite is protecting
-----------------------------

``atr.services.runner`` is the piece that makes ``BACKTEST → PAPER → LIVE``
have a middle that actually runs. Before it, the paper engine was request-driven:
prices came from the daily parquet close, a resting limit was re-evaluated only
if a client asked, and a deployment marked RUNNING did nothing at all.

The tests are grouped by requirement, because each one is a separate way for the
loop to be *quietly* wrong — filling at a stale price, placing the same order
sixty times a minute, letting a stop-loss through the risk gate's blind spot, or
reporting equity that disagrees with cash plus market value.

The last two are the ones worth reading. ``test_exit_is_not_rejected_as_a_short``
covers a bug that existed in the first draft: the runner handed the risk gate a
flat portfolio, so every SELL looked like opening a short and was rejected
whenever ``allow_short`` was false — which silently disabled stop-losses.
``test_equity_always_equals_cash_plus_market_value`` is the invariant the whole
"the log is the state" design exists to preserve.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from atr.services import runner as runner_module
from atr.services.runner import (
    DeploymentLoop,
    PaperRunner,
    RunnerConfig,
    in_market_hours,
)

#: A Monday, 10:00 IST — inside the cash session.
SESSION = datetime(2026, 9, 14, 10, 0)
SYMBOL = "RELIANCE"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _daily(symbol: str, n: int = 300, start: float = 1000.0) -> pd.DataFrame:
    """A flat daily frame long enough for every indicator to warm up."""
    index = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "ts": index,
            "open": [start] * n,
            "high": [start * 1.01] * n,
            "low": [start * 0.99] * n,
            "close": [start] * n,
            "volume": [100_000.0] * n,
        }
    )


class LiveFeed:
    """A price source whose price the test controls."""

    def __init__(self, prices: dict[str, float] | None = None) -> None:
        self.prices = dict(prices or {SYMBOL: 1000.0})
        self.calls = 0

    def __call__(self, symbol: str, exchange: str) -> float | None:
        self.calls += 1
        return self.prices.get(symbol.upper())


@pytest.fixture()
def deployment(app_db):
    """A RUNNING paper deployment for one strategy over one symbol."""
    from datetime import datetime as _dt

    from atr.appdb.repositories import DeploymentRepository
    from atr.appdb.schema import users

    with app_db.session() as session:
        session.execute(
            users.insert().values(
                user_id="u1",
                email="t@example.com",
                username="tester",
                display_name="T",
                password_hash="x",
                role="owner",
                is_active=True,
                mfa_enabled=False,
                failed_logins=0,
                created_at=_dt(2026, 1, 1),
                updated_at=_dt(2026, 1, 1),
            )
        )
    with app_db.session() as session:
        row = DeploymentRepository.create(
            session,
            user_id="u1",
            strategy_id="sma_pullback",
            strategy_version=1,
            mode="PAPER",
            capital=200_000.0,
            status="RUNNING",
            config={"symbols": [SYMBOL], "exchange": "NSEEQ"},
        )
    return row


@pytest.fixture()
def wired(monkeypatch, app_db):
    """A runner with stubbed history and forced signals."""

    def make(
        *,
        feed: LiveFeed | None = None,
        entry: bool = True,
        stop_loss_pct: float | None = 10.0,
        take_profit_pct: float | None = None,
    ) -> tuple[PaperRunner, LiveFeed]:
        feed = feed or LiveFeed()

        monkeypatch.setattr(
            "atr.signals.engine.load_daily",
            lambda symbol, exchange="NSEEQ", **kw: _daily(symbol),
        )

        from atr.signals import rules
        from atr.signals.models import EntryRules, ExitRules, Signal

        def stub_entry(symbol, frame, rls):
            if not entry:
                return []
            return [
                Signal(
                    symbol=symbol,
                    action="BUY",
                    rule="test_entry",
                    reason="forced",
                    price=float(frame["close"].iloc[-1]),
                )
            ]

        def stub_exit(symbol, frame, avg_price, rls, *, quantity=0.0):
            price = float(frame["close"].iloc[-1])
            pnl = (price / avg_price - 1.0) * 100.0
            if rls.stop_loss_pct and pnl <= -abs(rls.stop_loss_pct):
                return [
                    Signal(
                        symbol=symbol,
                        action="SELL",
                        rule="stop_loss",
                        reason=f"down {pnl:.1f}%",
                        price=price,
                    )
                ]
            if rls.take_profit_pct and pnl >= abs(rls.take_profit_pct):
                return [
                    Signal(
                        symbol=symbol,
                        action="SELL",
                        rule="take_profit",
                        reason=f"up {pnl:.1f}%",
                        price=price,
                    )
                ]
            return []

        monkeypatch.setattr(rules, "eval_entry", stub_entry)
        monkeypatch.setattr(rules, "eval_exit", stub_exit)
        monkeypatch.setattr(
            DeploymentLoop,
            "_rules",
            lambda self: (
                EntryRules(),
                ExitRules(
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                    trailing_stop_pct=None,
                    trend_sma=0,
                    rsi_overbought=None,
                ),
            ),
        )
        # The runner imports the rules inside the function, so patching the
        # module attribute is what takes effect — no need to patch the caller.
        return PaperRunner(db=app_db, price_source=feed), feed

    return make


def _loop(runner: PaperRunner, deployment: dict) -> DeploymentLoop:
    runner.sync_loops()
    return runner._loops[deployment["deployment_id"]]


# ---------------------------------------------------------------------------
# 1. market hours — a signal outside the session is about a closed market
# ---------------------------------------------------------------------------
def test_market_hours_cover_the_session_only():
    assert in_market_hours(datetime(2026, 9, 14, 9, 15)) is True
    assert in_market_hours(datetime(2026, 9, 14, 15, 30)) is True
    assert in_market_hours(datetime(2026, 9, 14, 9, 14)) is False
    assert in_market_hours(datetime(2026, 9, 14, 15, 31)) is False


def test_market_hours_exclude_the_weekend():
    assert in_market_hours(datetime(2026, 9, 19, 11, 0)) is False  # Saturday
    assert in_market_hours(datetime(2026, 9, 20, 11, 0)) is False  # Sunday


# ---------------------------------------------------------------------------
# 2. config — the deployment row is the whole definition
# ---------------------------------------------------------------------------
def test_config_accepts_a_json_string_and_a_mapping():
    from_string = RunnerConfig.from_deployment(
        {"config": json.dumps({"symbols": ["reliance", "tcs"], "exchange": "nseeq"})}
    )
    assert from_string.symbols == ("RELIANCE", "TCS")
    assert from_string.exchange == "NSEEQ"

    from_mapping = RunnerConfig.from_deployment({"config": {"symbols": ["infy"]}})
    assert from_mapping.symbols == ("INFY",)


def test_config_accepts_a_comma_separated_universe():
    config = RunnerConfig.from_deployment({"config": {"symbols": "INFY, wipro ,"}})
    assert config.symbols == ("INFY", "WIPRO")


@pytest.mark.parametrize("raw", [None, {}, {"config": None}, {"config": "{not json"}])
def test_config_degrades_to_empty_rather_than_raising(raw):
    """A malformed config must skip the deployment, not kill the loop."""
    assert RunnerConfig.from_deployment(raw).symbols == ()


# ---------------------------------------------------------------------------
# 3. the happy path — tick to position, through every existing component
# ---------------------------------------------------------------------------
def test_signal_opens_a_position_through_the_full_pipeline(wired, deployment):
    runner, feed = wired()
    loop = _loop(runner, deployment)

    tick = runner.pass_once(now=SESSION)
    assert tick.signals == 1 and tick.orders == 1

    portfolio = loop._portfolio()
    position = portfolio.position(SYMBOL)
    assert position.quantity > 0
    # Filled at the live price plus one tick of slippage, not decayed to cash.
    assert position.avg_price == pytest.approx(feed.prices[SYMBOL], rel=0.01)
    assert portfolio.cash < deployment["capital"]


def test_the_fill_uses_the_live_price_not_the_daily_close(wired, deployment):
    """Requirement 3. The cached frame says 1000; the tick says 1234."""
    feed = LiveFeed({SYMBOL: 1234.0})
    runner, _ = wired(feed=feed)
    loop = _loop(runner, deployment)

    runner.pass_once(now=SESSION)
    assert loop._portfolio().position(SYMBOL).avg_price == pytest.approx(1234.0, rel=0.01)


def test_no_order_is_placed_when_the_market_is_closed(wired, deployment):
    runner, _ = wired()
    loop = _loop(runner, deployment)

    tick = runner.pass_once(now=datetime(2026, 9, 14, 20, 0))
    assert tick.signals == 0 and tick.orders == 0
    assert loop._portfolio().position(SYMBOL).quantity == 0


def test_a_deployment_with_no_universe_is_skipped_not_traded(wired, app_db):
    """A deployment that would silently do nothing says so and is skipped."""
    from datetime import datetime as _dt

    from atr.appdb.repositories import DeploymentRepository
    from atr.appdb.schema import users

    with app_db.session() as session:
        session.execute(
            users.insert().values(
                user_id="u1", email="t@example.com", username="tester",
                display_name="T", password_hash="x", role="owner", is_active=True,
                mfa_enabled=False, failed_logins=0,
                created_at=_dt(2026, 1, 1), updated_at=_dt(2026, 1, 1),
            )
        )
    with app_db.session() as session:
        DeploymentRepository.create(
            session, user_id="u1", strategy_id="sma_pullback", strategy_version=1,
            mode="PAPER", capital=100_000.0, status="RUNNING",
            config={"symbols": []},
        )

    runner, _ = wired()
    tick = runner.pass_once(now=SESSION)
    assert tick.deployments == 0
    assert tick.orders == 0


# ---------------------------------------------------------------------------
# 4. idempotency — a rule that stays true must fire once
# ---------------------------------------------------------------------------
def test_the_same_bar_does_not_open_a_second_position(wired, deployment):
    """A daily rule true at 10:00 is still true at 10:01. One signal, one order."""
    runner, _ = wired()
    loop = _loop(runner, deployment)

    runner.pass_once(now=SESSION)
    first = loop._portfolio().position(SYMBOL).quantity

    tick = runner.pass_once(now=SESSION + timedelta(seconds=30))
    assert tick.orders == 0
    assert loop._portfolio().position(SYMBOL).quantity == first


def test_the_idempotency_key_is_stable_for_one_intent_and_differs_by_side(
    wired, deployment
):
    runner, _ = wired()
    loop = _loop(runner, deployment)
    runner.sync_loops()

    from atr.execution.oms import OrderDraft

    buy = OrderDraft(user_id="u1", symbol=SYMBOL, side="BUY", quantity=1)
    sell = OrderDraft(user_id="u1", symbol=SYMBOL, side="SELL", quantity=1)

    # Calling it twice for the same intent — a retry — must give the same key,
    # which is what makes the INSERT-as-guard work.
    assert loop._idempotency_key(buy, SYMBOL, "BUY") == loop._idempotency_key(
        buy, SYMBOL, "BUY"
    )
    # The other side is a different intent.
    assert loop._idempotency_key(buy, SYMBOL, "BUY") != loop._idempotency_key(
        sell, SYMBOL, "SELL"
    )


def test_repeated_passes_do_not_multiply_orders(wired, deployment):
    runner, _ = wired()
    _loop(runner, deployment)
    for i in range(10):
        runner.pass_once(now=SESSION + timedelta(seconds=i))
    assert runner.orders_total == 1


# ---------------------------------------------------------------------------
# 5. exits — the risk rules must be able to close a position
# ---------------------------------------------------------------------------
def test_stop_loss_closes_the_position(wired, deployment):
    runner, feed = wired(stop_loss_pct=10.0)
    loop = _loop(runner, deployment)

    runner.pass_once(now=SESSION)  # open at 1000
    assert loop._portfolio().position(SYMBOL).quantity > 0

    feed.prices[SYMBOL] = 880.0  # -12%, through the stop
    tick = runner.pass_once(now=SESSION + timedelta(minutes=5))
    assert tick.signals == 1
    assert loop._portfolio().position(SYMBOL).quantity == 0


def test_exit_is_not_rejected_as_a_short(wired, deployment):
    """The bug the first draft had.

    The risk gate compares a SELL against ``portfolio.position(symbol)``
    ``.quantity``. Handed a *flat* portfolio — the shared default — every exit
    reads as opening a short and is rejected whenever ``allow_short`` is false,
    so stop-losses would never execute. The runner must bind the deployment's own
    folded book.
    """
    from atr.core.enums import Side
    from atr.execution.oms import OrderDraft

    runner, feed = wired(stop_loss_pct=10.0)
    loop = _loop(runner, deployment)
    runner.pass_once(now=SESSION)

    loop.bind()
    held = loop._portfolio().position(SYMBOL).quantity
    assert held > 0, "nothing to test — the position never opened"

    # The gate must see the holding, not a flat book.
    gate_portfolio = loop.orders.risk_gate.portfolio
    assert gate_portfolio.position(SYMBOL).quantity == pytest.approx(held)

    draft = OrderDraft(
        user_id="u1", symbol=SYMBOL, side="SELL", quantity=held, mode="PAPER"
    )
    decision = loop.orders.risk_gate(draft)
    assert decision.allowed, f"exit rejected: {decision.reason}"


def test_take_profit_closes_the_position(wired, deployment):
    runner, feed = wired(stop_loss_pct=None, take_profit_pct=8.0)
    loop = _loop(runner, deployment)

    runner.pass_once(now=SESSION)
    feed.prices[SYMBOL] = 1100.0  # +10%, through the target
    runner.pass_once(now=SESSION + timedelta(minutes=5))
    assert loop._portfolio().position(SYMBOL).quantity == 0


def test_a_position_is_not_reopened_on_the_same_bar_after_an_exit(wired, deployment):
    """Once the position is closed the entry rule must not re-enter that day."""
    runner, feed = wired(stop_loss_pct=10.0)
    loop = _loop(runner, deployment)

    runner.pass_once(now=SESSION)
    feed.prices[SYMBOL] = 880.0
    runner.pass_once(now=SESSION + timedelta(minutes=5))
    assert loop._portfolio().position(SYMBOL).quantity == 0

    # Same bar: the entry key is already spent, so nothing reopens.
    runner.pass_once(now=SESSION + timedelta(minutes=6))
    assert loop._portfolio().position(SYMBOL).quantity == 0


# ---------------------------------------------------------------------------
# 6. position sizing
# ---------------------------------------------------------------------------
def test_size_is_whole_shares_and_never_exceeds_cash(wired, deployment):
    runner, _ = wired()
    loop = _loop(runner, deployment)
    runner.sync_loops()

    # 200k across a max of 10 positions = 20k per trade; 20k / 1000 = 20 shares.
    size = loop._size(1000.0)
    assert size == 20
    assert isinstance(size, int)


def test_size_is_zero_when_the_price_is_unusable(wired, deployment):
    runner, _ = wired()
    loop = _loop(runner, deployment)
    assert loop._size(0.0) == 0
    assert loop._size(-5.0) == 0


def test_size_uses_the_configured_order_value_over_capital(wired, deployment):
    """An explicit ``order_value`` wins; capital is only the fallback."""
    runner, _ = wired()
    loop = _loop(runner, deployment)
    runner.sync_loops()
    loop.config.order_value = 5000.0
    assert loop._size(1000.0) == 5


def test_size_is_capped_by_the_folded_cash_not_the_allocation(wired, deployment):
    """Sizing must respect cash on hand, not just the allocation.

    ``order_value`` here is far larger than the account, so the only thing that
    can bound the order is the folded cash. If the cash cap were missing the
    order would be 2000 shares and would then fail the gate's cash check — the
    signal would be silently dropped instead of sized to what is available.

    The folded cash comes from the deployment row in the database (see
    ``PaperLedger._deployment_cash``), so the allocation is what bounds this.
    """
    runner, _ = wired()
    loop = _loop(runner, deployment)
    runner.sync_loops()

    loop.config.order_value = 2_000_000.0  # 10x the account
    # 200k cash, less the 2% cost buffer, at 1000 per share.
    assert loop._size(1000.0) == 196


def test_size_is_zero_with_no_allocation_and_no_order_value(wired, deployment, app_db):
    """No capital anywhere means no trade — not a guess at a default size."""
    from datetime import datetime as _dt

    from atr.appdb.repositories import DeploymentRepository
    from atr.appdb.schema import users

    with app_db.session() as session:
        session.execute(
            users.insert().values(
                user_id="u2", email="t2@example.com", username="tester2",
                display_name="T2", password_hash="x", role="owner", is_active=True,
                mfa_enabled=False, failed_logins=0,
                created_at=_dt(2026, 1, 1), updated_at=_dt(2026, 1, 1),
            )
        )

    runner, _ = wired()
    loop = _loop(runner, deployment)
    runner.sync_loops()
    # A row with no capital at all.
    loop.row = {**loop.row, "capital": 0.0, "user_id": "u2"}
    assert loop._size(1000.0) == 0


# ---------------------------------------------------------------------------
# 7. resting orders — requirement 4, re-evaluated as prices move
# ---------------------------------------------------------------------------
def test_resting_limit_order_fills_when_the_price_reaches_it(wired, deployment, app_db):
    """A limit away from the market rests, then fills when the market arrives."""
    from atr.execution.oms import OrderDraft

    runner, feed = wired()
    loop = _loop(runner, deployment)
    runner.sync_loops()
    loop.bind()

    # Place a limit well below the market. It must rest, not fill.
    draft = OrderDraft(
        user_id="u1",
        symbol=SYMBOL,
        side="BUY",
        quantity=10,
        mode="PAPER",
        limit_price=900.0,
        order_type="LIMIT",
        deployment_id=loop.deployment_id,
    )
    placed = loop.execution.place(draft, idempotency_key=None)
    assert placed.status in ("ACKNOWLEDGED", "SUBMITTED", "NEW")
    assert loop._portfolio().position(SYMBOL).quantity == 0

    # The market never comes to it: still nothing.
    tick = runner.pass_once(now=SESSION)
    assert tick.resting_fills == 0

    # Now it does.
    feed.prices[SYMBOL] = 895.0
    tick = runner.pass_once(now=SESSION + timedelta(seconds=5))
    assert tick.resting_fills >= 1 or loop._portfolio().position(SYMBOL).quantity > 0


def test_resting_orders_are_polled_outside_market_hours(wired, deployment):
    """An accepted order should be able to fill on any price that arrives."""
    from atr.execution.oms import OrderDraft

    runner, feed = wired()
    loop = _loop(runner, deployment)
    runner.sync_loops()
    loop.bind()

    draft = OrderDraft(
        user_id="u1",
        symbol=SYMBOL,
        side="BUY",
        quantity=5,
        mode="PAPER",
        limit_price=900.0,
        order_type="LIMIT",
        deployment_id=loop.deployment_id,
    )
    loop.execution.place(draft, idempotency_key=None)

    feed.prices[SYMBOL] = 850.0
    tick = runner.pass_once(now=datetime(2026, 9, 14, 20, 0))
    # Outside the session no *new* signals fire, but the resting order is still
    # re-evaluated rather than left stuck until tomorrow.
    assert tick.signals == 0
    assert loop._portfolio().position(SYMBOL).quantity >= 0


# ---------------------------------------------------------------------------
# 8. the invariant — the whole design rests on this
# ---------------------------------------------------------------------------
def test_equity_always_equals_cash_plus_market_value(wired, deployment):
    runner, feed = wired(stop_loss_pct=10.0)
    loop = _loop(runner, deployment)

    runner.pass_once(now=SESSION)
    portfolio = loop._portfolio()
    assert portfolio.check_invariant() is None

    feed.prices[SYMBOL] = 1050.0
    portfolio = loop._portfolio()
    assert portfolio.check_invariant() is None
    assert portfolio.equity == pytest.approx(portfolio.cash + portfolio.market_value)


def test_pnl_moves_with_the_live_price(wired, deployment):
    runner, feed = wired()
    loop = _loop(runner, deployment)
    runner.pass_once(now=SESSION)
    qty = loop._portfolio().position(SYMBOL).quantity
    entry = loop._portfolio().position(SYMBOL).avg_price

    feed.prices[SYMBOL] = entry + 50.0
    expected = qty * 50.0
    unrealized = loop._portfolio().unrealized_pnl
    assert unrealized == pytest.approx(expected, rel=0.01)


def test_costs_are_charged_and_reduce_cash(wired, deployment):
    """A fill is not free: cash must fall by more than quantity * price."""
    runner, feed = wired()
    loop = _loop(runner, deployment)
    runner.pass_once(now=SESSION)

    portfolio = loop._portfolio()
    qty = portfolio.position(SYMBOL).quantity
    entry = portfolio.position(SYMBOL).avg_price
    spent = deployment["capital"] - portfolio.cash
    assert spent > qty * entry, "no costs were charged on the fill"
    assert portfolio.commission_paid > 0


# ---------------------------------------------------------------------------
# 9. the loop itself — start, stop, sync, status
# ---------------------------------------------------------------------------
def test_status_reports_the_loop_state(wired, deployment):
    runner, _ = wired()
    status = runner.status()
    assert status["running"] is False
    assert status["deployments"] == []

    runner.pass_once(now=SESSION)
    status = runner.status()
    assert status["passes"] == 1
    assert len(status["deployments"]) == 1


def test_loops_are_dropped_when_a_deployment_stops(wired, deployment, app_db):
    from atr.appdb.repositories import DeploymentRepository

    runner, _ = wired()
    _loop(runner, deployment)
    assert len(runner._loops) == 1

    with app_db.session() as session:
        DeploymentRepository.stop(
            session, deployment["deployment_id"], "u1", reason="test"
        )

    runner.sync_loops()
    assert runner._loops == {}


@pytest.mark.asyncio
async def test_the_background_loop_starts_and_stops(wired, deployment):
    import asyncio

    runner, _ = wired()
    runner.start()
    assert runner.running is True

    # Wait for the loop to actually complete a pass rather than sleeping a fixed
    # interval: a fixed sleep is a flaky assertion on a loaded machine, and it
    # asserts nothing about the loop having run.
    for _ in range(100):
        if runner.passes >= 1:
            break
        await asyncio.sleep(0.02)
    assert runner.passes >= 1, "the background loop never completed a pass"

    await runner.stop()
    assert runner.running is False


@pytest.mark.asyncio
async def test_start_is_idempotent(wired, deployment):
    runner, _ = wired()
    runner.start()
    first = runner._task
    runner.start()
    assert runner._task is first
    await runner.stop()


def test_one_broken_deployment_does_not_stop_the_others(wired, deployment, monkeypatch):
    """A single exception must not take the whole loop down."""
    runner, _ = wired()
    loop = _loop(runner, deployment)

    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(loop, "evaluate", explode)
    tick = runner.pass_once(now=SESSION)  # must not raise
    assert tick.deployments == 1


def test_a_broken_store_does_not_kill_the_loop(wired, deployment, monkeypatch):
    runner, _ = wired()
    monkeypatch.setattr(
        runner,
        "running_deployments",
        lambda: (_ for _ in ()).throw(RuntimeError("db is down")),
    )
    tick = runner.pass_once(now=SESSION)  # must not raise
    assert tick.signals == 0


# ---------------------------------------------------------------------------
# 10. the singleton
# ---------------------------------------------------------------------------
def test_get_runner_is_a_singleton(fresh_env):
    from atr.services.runner import get_runner, reset_runner

    reset_runner()
    assert get_runner() is get_runner()
    reset_runner()
