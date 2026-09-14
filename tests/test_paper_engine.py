"""The paper engine: the matching rules, and the ledger that folds the fills.

Two properties are worth more than the rest:

* **A paper order must not fill when the market has not reached it.** Filling a buy
  limit above its limit is the classic paper-trading lie — it manufactures edge the
  market never offered. The same goes for a stop that has not triggered.
* **The ledger is a fold over ``order_events`` and holds no state.** So a paper
  position cannot drift from the orders that produced it, and a restart changes
  nothing. The test for it replays the log and asserts the ``Portfolio`` invariant
  (``equity == cash + market value``) that the backtester already relies on.

The costs are also tested for *which* model was used: the IBKR-style default
understates NSE delivery by roughly 28x, so a paper account costed with it reports
an edge that does not exist.
"""

from __future__ import annotations

import pytest

from atr.appdb.repositories import DeploymentRepository, UserRepository
from atr.backtest.costs import CommissionModel, IndianDeliveryCosts, SlippageModel
from atr.execution.oms import OrderDraft, RiskDecision
from atr.services.execution import VENUE_ACCEPTED, VENUE_FILLED, VENUE_REJECTED, ExecutionService
from atr.services.orders import OrderService
from atr.services.paper import (
    PaperLedger,
    PaperVenue,
    fixed_price_source,
    match_order,
)


@pytest.fixture()
def trader(app_db):
    with app_db.session() as session:
        return UserRepository.create(
            session, email="p@example.com", username="paper",
            password_hash="x", role="trader",
        )["user_id"]


def _venue(prices: dict[str, float], **kwargs) -> PaperVenue:
    kwargs.setdefault("slippage", SlippageModel(bps=0.0))
    return PaperVenue(prices=fixed_price_source(prices), **kwargs)


def _draft(user_id: str, **overrides) -> OrderDraft:
    kwargs = {
        "user_id": user_id,
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 10,
        "mode": "PAPER",
        "requested_price": 2500.0,
    }
    kwargs.update(overrides)
    return OrderDraft(**kwargs)


def _service(app_db, venue) -> ExecutionService:
    def allow(draft: OrderDraft) -> RiskDecision:  # noqa: ARG001
        return RiskDecision.ok()

    return ExecutionService(
        orders=OrderService(app_db, risk_gate=allow), venue=venue
    )


def _open_acknowledged(app_db, trader, **overrides):
    """An order sitting at ACKNOWLEDGED with nothing filled.

    Built through the OMS rather than through the venue, so a test can drive the
    fills itself — which is what the fold tests need, since a PaperVenue fills
    immediately.
    """
    def allow(draft: OrderDraft) -> RiskDecision:  # noqa: ARG001
        return RiskDecision.ok()

    service = OrderService(app_db, risk_gate=allow)
    opened = service.open_order(_draft(trader, **overrides))
    service.validate(opened.order_id, trader)
    service.submit(opened.order_id, trader)
    service.acknowledge(opened.order_id, trader, broker_order_id="PAPER-1")
    return service, opened.order_id


# =========================================================================== matching
def test_a_market_order_fills_at_the_reference_price():
    decision = match_order(order_type="MARKET", side="BUY", reference=2500.0)
    assert decision.fills is True
    assert decision.price == 2500.0


def test_a_buy_limit_below_the_market_rests_rather_than_filling():
    """The classic paper-trading lie: filling a buy limit above its own limit."""
    decision = match_order(
        order_type="LIMIT", side="BUY", reference=2510.0, limit_price=2500.0
    )
    assert decision.fills is False
    assert "has not reached the limit" in decision.reason


def test_a_buy_limit_above_the_market_fills_at_the_market():
    """Price improvement is real: you fill at the market, not at your limit."""
    decision = match_order(
        order_type="LIMIT", side="BUY", reference=2490.0, limit_price=2500.0
    )
    assert decision.fills is True
    assert decision.price == 2490.0


def test_a_sell_limit_above_the_market_rests():
    decision = match_order(
        order_type="LIMIT", side="SELL", reference=2490.0, limit_price=2500.0
    )
    assert decision.fills is False


def test_a_sell_limit_below_the_market_fills():
    decision = match_order(
        order_type="LIMIT", side="SELL", reference=2510.0, limit_price=2500.0
    )
    assert decision.fills is True
    assert decision.price == 2510.0


@pytest.mark.parametrize(
    ("reference", "expected"),
    [(2450.0, True), (2451.0, False), (2400.0, True)],
)
def test_a_sell_stop_triggers_when_the_market_falls_to_it(reference, expected):
    decision = match_order(
        order_type="SL-M", side="SELL", reference=reference, stop_price=2450.0
    )
    assert decision.fills is expected


@pytest.mark.parametrize(
    ("reference", "expected"),
    [(2560.0, True), (2540.0, False)],
)
def test_a_buy_stop_triggers_when_the_market_rises_to_it(reference, expected):
    decision = match_order(
        order_type="STOP", side="BUY", reference=reference, stop_price=2550.0
    )
    assert decision.fills is expected


def test_a_stop_limit_can_be_triggered_and_still_refuse():
    """The trigger fired but the price gapped past the limit."""
    decision = match_order(
        order_type="SL", side="SELL", reference=2400.0,
        stop_price=2450.0, limit_price=2440.0,
    )
    assert decision.fills is False
    assert "skipped past" in decision.reason


def test_an_order_that_cannot_be_priced_is_not_fillable():
    decision = match_order(order_type="MARKET", side="BUY", reference=None)
    assert decision.fills is False
    assert decision.reason == "no price available"
    assert match_order(order_type="MARKET", side="BUY", reference=0.0).fills is False


def test_a_limit_order_without_a_limit_price_is_refused():
    decision = match_order(order_type="LIMIT", side="BUY", reference=2500.0)
    assert decision.fills is False
    assert "needs a limit price" in decision.reason


def test_a_stop_order_without_a_trigger_is_refused():
    decision = match_order(order_type="SL-M", side="SELL", reference=2500.0)
    assert decision.fills is False
    assert "needs a trigger price" in decision.reason


# =========================================================================== the venue
def test_slippage_is_applied_against_the_order(app_db, trader):
    venue = _venue({"RELIANCE": 2500.0}, slippage=SlippageModel(bps=10.0))
    bought = venue.submit(_draft(trader, side="BUY"))
    sold = venue.submit(_draft(trader, side="SELL"))

    assert bought.filled_price == pytest.approx(2502.5)
    assert sold.filled_price == pytest.approx(2497.5)


def test_the_cost_model_is_the_indian_one_not_the_ibkr_default(app_db, trader):
    """The default understates NSE delivery by ~28x, which invents edge."""
    indian = _venue({"RELIANCE": 2500.0}).submit(_draft(trader, quantity=100))
    ibkr = _venue({"RELIANCE": 2500.0}, costs=CommissionModel()).submit(
        _draft(trader, quantity=100)
    )
    assert indian.commission > 20 * ibkr.commission, (
        f"Indian costs {indian.commission} vs IBKR {ibkr.commission}"
    )
    # STT alone is 0.1% of a Rs 2.5L notional = Rs 250.
    assert indian.commission > 250.0


def test_a_sell_pays_the_dp_charge(app_db, trader):
    """DP is charged per sell, so the two sides are not symmetric."""
    venue = _venue({"RELIANCE": 2500.0})
    buy = venue.submit(_draft(trader, side="BUY", quantity=10))
    sell = venue.submit(_draft(trader, side="SELL", quantity=10))
    assert sell.commission > buy.commission


def test_an_unpriceable_symbol_is_rejected_not_filled_at_zero(app_db, trader):
    """A fill at zero would give the position a zero cost basis and an infinite return."""
    venue = _venue({})  # nothing has a price
    outcome = venue.submit(_draft(trader, symbol="GHOST"))
    assert outcome.status == VENUE_REJECTED
    assert "cannot be priced" in outcome.reject_reason


def test_a_resting_limit_order_is_accepted_and_not_filled(app_db, trader):
    venue = _venue({"RELIANCE": 2510.0})
    outcome = venue.submit(
        _draft(trader, order_type="LIMIT", limit_price=2500.0)
    )
    assert outcome.status == VENUE_ACCEPTED
    assert outcome.filled_qty == 0.0
    assert "resting_reason" in outcome.raw


def test_a_resting_order_fills_later_when_the_market_reaches_it(app_db, trader):
    """So a paper order behaves like one that is live, not one that fills always."""
    venue = _venue({"RELIANCE": 2510.0})
    venue.submit(_draft(trader, order_type="LIMIT", limit_price=2500.0))

    venue.prices = fixed_price_source({"RELIANCE": 2495.0})
    outcome = venue.match(
        {
            "symbol": "RELIANCE", "exchange": "NSEEQ", "side": "BUY",
            "order_type": "LIMIT", "limit_price": 2500.0, "stop_price": None,
            "quantity": 10.0, "filled_quantity": 0.0, "broker_order_id": "PAPER-1",
        }
    )
    assert outcome.status == VENUE_FILLED
    assert outcome.filled_price == pytest.approx(2495.0)
    assert outcome.raw["matched_resting_order"] is True


def test_matching_a_partially_filled_order_uses_the_remainder(app_db, trader):
    venue = _venue({"RELIANCE": 2500.0})
    outcome = venue.match(
        {
            "symbol": "RELIANCE", "exchange": "NSEEQ", "side": "BUY",
            "order_type": "MARKET", "limit_price": None, "stop_price": None,
            "quantity": 10.0, "filled_quantity": 4.0,
        }
    )
    assert outcome.filled_qty == pytest.approx(10.0), "cumulative, not incremental"
    assert outcome.status == VENUE_FILLED


def test_a_partial_fill_ratio_makes_partial_fills_part_of_the_simulation(app_db, trader):
    venue = _venue({"RELIANCE": 2500.0}, fill_ratio=0.4)
    outcome = venue.submit(_draft(trader, quantity=10))
    assert outcome.filled_qty == pytest.approx(4.0)


def test_a_zero_fill_ratio_rests_rather_than_filling_nothing(app_db, trader):
    """A fill of zero quantity is not a fill; it must not be recorded as one."""
    venue = _venue({"RELIANCE": 2500.0}, fill_ratio=0.0)
    outcome = venue.submit(_draft(trader, quantity=10))
    assert outcome.status == VENUE_ACCEPTED
    assert outcome.filled_qty == 0.0


# =========================================================================== the ledger
def test_the_ledger_replays_a_paper_round_trip(app_db, trader):
    venue = _venue({"RELIANCE": 2500.0}, slippage=SlippageModel(bps=0.0))
    service = _service(app_db, venue)

    service.place(_draft(trader, side="BUY", quantity=10))
    ledger = PaperLedger(app_db)
    snapshot = ledger.snapshot(trader)

    assert snapshot["positions"][0]["symbol"] == "RELIANCE"
    assert snapshot["positions"][0]["quantity"] == 10.0
    assert snapshot["positions"][0]["avg_price"] == pytest.approx(2500.0)
    assert snapshot["realized_pnl"] == 0.0
    assert snapshot["commission_paid"] > 0.0


def test_the_portfolio_invariant_holds_after_the_fold(app_db, trader):
    """``equity == initial + realised + unrealised - commission``.

    The identity the backtester already asserts after every fill. If the paper
    fold gets the cash or the position arithmetic wrong, this is where it shows.
    """
    service = _service(app_db, _venue({"RELIANCE": 2500.0}))
    service.place(_draft(trader, side="BUY", quantity=10))

    portfolio = PaperLedger(app_db).portfolio(trader, initial_cash=1_000_000.0)
    portfolio.mark(__import__("datetime").datetime(2026, 9, 14), {"RELIANCE": 2520.0})
    portfolio.check_invariant()


def test_a_closed_round_trip_realises_the_pnl_net_of_costs(app_db, trader):
    venue = _venue({"RELIANCE": 2500.0}, slippage=SlippageModel(bps=0.0))
    service = _service(app_db, venue)
    service.place(_draft(trader, side="BUY", quantity=10))

    venue.prices = fixed_price_source({"RELIANCE": 2550.0})
    service.place(_draft(trader, side="SELL", quantity=10))

    snapshot = PaperLedger(app_db).snapshot(trader, initial_cash=1_000_000.0)
    assert snapshot["positions"] == []
    assert snapshot["realized_pnl"] == pytest.approx(500.0)
    assert snapshot["commission_paid"] > 0.0
    assert snapshot["net_realized_pnl"] == pytest.approx(
        500.0 - snapshot["commission_paid"]
    )
    assert snapshot["total_pnl"] == pytest.approx(snapshot["net_realized_pnl"])


def test_two_partial_fills_are_counted_once_each(app_db, trader):
    """``filled_qty`` is cumulative, so the fold must difference it."""
    service, order_id = _open_acknowledged(app_db, trader, quantity=10)
    service.record_fill(order_id, trader, filled_qty=4.0, filled_price=2500.0)
    service.record_fill(order_id, trader, filled_qty=10.0, filled_price=2500.0)

    portfolio = PaperLedger(app_db).portfolio(trader, initial_cash=1_000_000.0)
    assert portfolio.position("RELIANCE").quantity == pytest.approx(10.0), (
        "the second fill's cumulative 10 must add 6, not 10"
    )


def test_a_repeated_cumulative_figure_is_not_double_counted(app_db, trader):
    """A replayed execution report is not a new fill."""
    service, order_id = _open_acknowledged(app_db, trader, quantity=10)
    service.record_fill(order_id, trader, filled_qty=6.0, filled_price=2500.0)

    # The same report again, written by a reconciler that did not know it had been
    # seen. The OMS allows the self-loop, so the fold has to be the guard.
    from atr.appdb.repositories import OrderEventRepository

    with app_db.session() as session:
        OrderEventRepository.append(
            session, order_id=order_id, from_status="PARTIALLY_FILLED",
            to_status="PARTIALLY_FILLED", filled_qty=6.0, filled_price=2500.0,
        )

    portfolio = PaperLedger(app_db).portfolio(trader, initial_cash=1_000_000.0)
    assert portfolio.position("RELIANCE").quantity == pytest.approx(6.0)


def test_the_commission_comes_from_the_event_not_from_today_s_rates(app_db, trader):
    """Recomputing would restate history whenever a rate changed."""
    service, order_id = _open_acknowledged(app_db, trader, quantity=10)
    service.record_fill(
        order_id, trader, filled_qty=10.0, filled_price=2500.0, commission=1234.5
    )

    fills = PaperLedger(app_db).fills(trader)
    assert [f.commission for f in fills] == [1234.5]


def test_the_recorded_slippage_is_what_the_fill_carries(app_db, trader):
    """``Portfolio.slippage_cost`` multiplies a per-unit figure, so the fold converts."""
    service, order_id = _open_acknowledged(
        app_db, trader, quantity=10, requested_price=2500.0
    )
    service.record_fill(
        order_id, trader, filled_qty=10.0, filled_price=2502.5, slippage=10.0
    )

    fill = PaperLedger(app_db).fills(trader)[0]
    assert fill.slippage == pytest.approx(2.5), "10 bps of 2502.5 per unit"


def test_a_paper_account_with_no_deployment_has_no_invented_capital(app_db, trader):
    """Zero rather than a guess: a starting cash nobody chose is not a measurement."""
    snapshot = PaperLedger(app_db).snapshot(trader)
    assert snapshot["initial_cash"] == 0.0
    assert snapshot["equity"] == 0.0


# --------------------------------------------------- unpriced positions are named
def test_an_unpriced_position_is_shown_with_an_unknown_valuation(app_db, trader):
    """Omitting the row would say "you hold nothing" — a different lie.

    ``Portfolio`` leaves an unmarked position at ``last_price = 0``, and
    ``unrealized_pnl`` would then report ``(0 - avg) * qty``: a fabricated loss of
    the entire cost basis, presented as a measurement.
    """
    _service(app_db, _venue({"RELIANCE": 2500.0})).place(_draft(trader, quantity=10))
    snapshot = PaperLedger(app_db).snapshot(trader, prices={})  # no marks available

    row = snapshot["positions"][0]
    assert row["symbol"] == "RELIANCE"
    assert row["quantity"] == 10.0, "the position is real and must still be shown"
    assert row["avg_price"] == pytest.approx(2500.0)
    assert row["last_price"] is None
    assert row["market_value"] is None
    assert row["unrealized_pnl"] is None, "an unknown valuation is not a negative number"
    assert row["priced"] is False

    assert snapshot["complete"] is False
    assert snapshot["unpriced_symbols"] == ["RELIANCE"]


def test_the_totals_exclude_an_unpriced_position_and_say_so(app_db, trader):
    _service(app_db, _venue({"RELIANCE": 2500.0})).place(_draft(trader, quantity=10))
    snapshot = PaperLedger(app_db).snapshot(trader, initial_cash=1_000_000.0, prices={})

    # Cash is a fact and is reported; the position's contribution is not counted.
    assert snapshot["cash"] < 1_000_000.0
    assert snapshot["unrealized_pnl"] == 0.0
    assert snapshot["complete"] is False


def test_a_priced_position_makes_the_snapshot_complete(app_db, trader):
    _service(app_db, _venue({"RELIANCE": 2500.0})).place(_draft(trader, quantity=10))
    snapshot = PaperLedger(app_db).snapshot(
        trader, initial_cash=1_000_000.0, prices={"RELIANCE": 2550.0}
    )
    assert snapshot["complete"] is True
    assert snapshot["unpriced_symbols"] == []
    assert snapshot["positions"][0]["priced"] is True
    assert snapshot["unrealized_pnl"] == pytest.approx(500.0)
    assert snapshot["equity"] == pytest.approx(1_000_500.0 - snapshot["commission_paid"])


# =========================================================== the deployment service
def test_a_deployment_allocates_capital_to_a_strategy_version(app_db, trader):
    from atr.services.paper import DeploymentService

    service = DeploymentService(app_db)
    created = service.create(
        trader, strategy_id="s1", strategy_version=1, capital=250_000.0
    )
    assert created["status"] == "PENDING"
    assert created["mode"] == "PAPER"

    started = service.start(trader, created["deployment_id"])
    assert started["status"] == "RUNNING"


def test_a_deployment_is_not_visible_to_another_account(app_db, trader):
    from atr.services.paper import DeploymentError, DeploymentService

    with app_db.session() as session:
        stranger = UserRepository.create(
            session, email="x@example.com", username="stranger2",
            password_hash="x", role="trader",
        )["user_id"]
    created = DeploymentService(app_db).create(
        trader, strategy_id="s1", strategy_version=1, capital=100_000.0
    )
    with pytest.raises(DeploymentError) as caught:
        DeploymentService(app_db).get(stranger, created["deployment_id"])
    assert caught.value.code == "not_found"
    assert caught.value.status == 404


def test_a_stopped_deployment_cannot_be_resumed(app_db, trader):
    """Restarting a stopped strategy is a new deployment, not a silent resume."""
    from atr.services.paper import DeploymentError, DeploymentService

    service = DeploymentService(app_db)
    created = service.create(trader, strategy_id="s1", strategy_version=1, capital=100_000.0)
    service.start(trader, created["deployment_id"])
    service.stop(trader, created["deployment_id"], reason="done for the day")

    with pytest.raises(DeploymentError) as caught:
        service.start(trader, created["deployment_id"])
    assert caught.value.code == "not_found"


def test_pausing_and_resuming(app_db, trader):
    from atr.services.paper import DeploymentService

    service = DeploymentService(app_db)
    created = service.create(trader, strategy_id="s1", strategy_version=1, capital=100_000.0)
    service.start(trader, created["deployment_id"])

    paused = service.pause(trader, created["deployment_id"], reason="waiting for the open")
    assert paused["status"] == "PAUSED"
    assert paused["stop_reason"] == "waiting for the open"
    assert service.start(trader, created["deployment_id"])["status"] == "RUNNING"


def test_pausing_requires_a_reason(app_db, trader):
    from atr.services.paper import DeploymentError, DeploymentService

    service = DeploymentService(app_db)
    created = service.create(trader, strategy_id="s1", strategy_version=1, capital=100_000.0)
    service.start(trader, created["deployment_id"])
    with pytest.raises(DeploymentError) as caught:
        service.pause(trader, created["deployment_id"], reason="  ")
    assert caught.value.code == "reason_required"


def test_reset_returns_a_new_deployment_and_keeps_the_old_history(app_db, trader):
    """``order_events`` is append-only, so a reset cannot erase anything."""
    from atr.services.paper import DeploymentService

    service = DeploymentService(app_db)
    original = service.create(
        trader, strategy_id="s1", strategy_version=1, capital=100_000.0
    )
    service.start(trader, original["deployment_id"])
    _service(app_db, _venue({"RELIANCE": 2500.0})).place(
        _draft(trader, quantity=10, deployment_id=original["deployment_id"])
    )

    fresh = service.reset(trader, original["deployment_id"], reason="strategy tweaked")

    assert fresh["deployment_id"] != original["deployment_id"]
    assert fresh["reset_from"] == original["deployment_id"]
    assert fresh["capital"] == 100_000.0
    # The old deployment is stopped, and its fills are still there.
    assert service.get(trader, original["deployment_id"])["status"] == "STOPPED"
    assert service.orders(trader, original["deployment_id"])
    # The new one starts clean.
    assert service.orders(trader, fresh["deployment_id"]) == []
    assert PaperLedger(app_db).snapshot(
        trader, deployment_id=fresh["deployment_id"]
    )["positions"] == []


def test_reset_requires_a_reason(app_db, trader):
    from atr.services.paper import DeploymentError, DeploymentService

    service = DeploymentService(app_db)
    created = service.create(trader, strategy_id="s1", strategy_version=1, capital=100_000.0)
    with pytest.raises(DeploymentError) as caught:
        service.reset(trader, created["deployment_id"], reason="")
    assert caught.value.code == "reason_required"


def test_a_deployment_list_carries_its_pnl(app_db, trader):
    from atr.services.paper import DeploymentService

    service = DeploymentService(app_db)
    created = service.create(trader, strategy_id="s1", strategy_version=1, capital=100_000.0)
    _service(app_db, _venue({"RELIANCE": 2500.0})).place(
        _draft(trader, quantity=10, deployment_id=created["deployment_id"])
    )

    rows = service.list(trader)
    assert rows[0]["pnl"]["initial_cash"] == 100_000.0
    assert rows[0]["pnl"]["positions"][0]["symbol"] == "RELIANCE"


def test_a_deployment_supplies_the_starting_capital(app_db, trader):
    with app_db.session() as session:
        deployment = DeploymentRepository.create(
            session, user_id=trader, strategy_id="s1", strategy_version=1,
            mode="PAPER", capital=500_000.0,
        )
        deployment_id = deployment["deployment_id"]

    snapshot = PaperLedger(app_db).snapshot(trader, deployment_id=deployment_id)
    assert snapshot["initial_cash"] == 500_000.0
    assert snapshot["equity"] == 500_000.0


def test_the_ledger_is_scoped_to_a_deployment(app_db, trader):
    """Per-strategy capital means per-strategy positions."""
    with app_db.session() as session:
        first = DeploymentRepository.create(
            session, user_id=trader, strategy_id="s1", strategy_version=1,
            mode="PAPER", capital=100_000.0,
        )["deployment_id"]
        second = DeploymentRepository.create(
            session, user_id=trader, strategy_id="s2", strategy_version=1,
            mode="PAPER", capital=100_000.0,
        )["deployment_id"]

    service = _service(app_db, _venue({"RELIANCE": 2500.0}))
    service.place(_draft(trader, quantity=10, deployment_id=first))

    ledger = PaperLedger(app_db)
    assert ledger.snapshot(trader, deployment_id=first)["positions"]
    assert ledger.snapshot(trader, deployment_id=second)["positions"] == []


def test_the_ledger_is_user_scoped(app_db, trader):
    other = UserRepository.create
    with app_db.session() as session:
        stranger = UserRepository.create(
            session, email="s@example.com", username="stranger",
            password_hash="x", role="trader",
        )["user_id"]
    _service(app_db, _venue({"RELIANCE": 2500.0})).place(_draft(trader, quantity=10))

    assert PaperLedger(app_db).snapshot(stranger)["positions"] == []
    assert other is not None


# =========================================================================== end to end
def test_a_paper_order_goes_through_the_whole_lifecycle(app_db, trader):
    """Same OMS, same risk gate, same event log as live — only the venue differs."""
    venue = _venue({"RELIANCE": 2500.0}, slippage=SlippageModel(bps=4.0))
    service = _service(app_db, venue)
    result = service.place(_draft(trader, quantity=10))

    assert result.status == "FILLED"
    events = service.orders.history(result.order_id, trader)
    assert [(e["from_status"], e["to_status"]) for e in events] == [
        (None, "NEW"),
        ("NEW", "VALIDATING"),
        ("VALIDATING", "RISK_APPROVED"),
        ("RISK_APPROVED", "SUBMITTED"),
        ("SUBMITTED", "ACKNOWLEDGED"),
        ("ACKNOWLEDGED", "FILLED"),
    ]
    # The fill carries its frictions, and the slippage is measurable.
    fill = events[-1]
    assert fill["commission"] > 0
    assert fill["slippage_bps"] == pytest.approx(4.0)


def test_a_paper_order_is_refused_by_risk_exactly_like_a_live_one(app_db, trader):
    """The gate is shared, so paper cannot be a way around it."""
    venue = _venue({"RELIANCE": 2500.0})
    service = ExecutionService(
        orders=OrderService(
            app_db,
            risk_gate=lambda d: RiskDecision.reject("kill switch engaged", "kill_switch"),
        ),
        venue=venue,
    )
    result = service.place(_draft(trader, quantity=10))
    assert result.status == "REJECTED"
    assert result.reject_reason == "kill switch engaged"
    assert PaperLedger(app_db).snapshot(trader)["positions"] == []


def test_a_paper_account_costed_with_indian_rates_earns_less_than_one_costed_free(
    app_db, trader
):
    """The point of the cost model, stated as a test.

    A 10-point move on 10 shares is Rs 100. Two delivery legs on a Rs 25,000
    notional cost roughly Rs 60 of that, so most of a small scalp is frictions —
    which is exactly the finding the research layer keeps reproducing.
    """
    class Free(CommissionModel):
        def compute(self, quantity, price, instrument, side=None):  # noqa: ARG002
            return 0.0

    def run(costs) -> float:
        # A deployment per run, so the second does not accumulate on the first's
        # positions and the comparison is like for like.
        with app_db.session() as session:
            deployment_id = DeploymentRepository.create(
                session, user_id=trader, strategy_id="s1", strategy_version=1,
                mode="PAPER", capital=1_000_000.0,
            )["deployment_id"]

        venue = PaperVenue(
            prices=fixed_price_source({"RELIANCE": 2500.0}),
            slippage=SlippageModel(bps=0.0),
            costs=costs,
        )
        service = _service(app_db, venue)
        service.place(_draft(trader, side="BUY", quantity=10, deployment_id=deployment_id))
        venue.prices = fixed_price_source({"RELIANCE": 2510.0})
        service.place(_draft(trader, side="SELL", quantity=10, deployment_id=deployment_id))

        snapshot = PaperLedger(app_db).snapshot(trader, deployment_id=deployment_id)
        return snapshot["total_pnl"]

    gross = run(Free())
    net = run(IndianDeliveryCosts())
    assert gross == pytest.approx(100.0)
    assert net < gross
    assert gross - net > 30.0, "delivery frictions on a Rs 25k round trip are material"
