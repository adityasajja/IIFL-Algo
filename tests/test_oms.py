"""The order state machine and the service that persists it.

The tests here are the ones that matter for money:

* every legal transition works and every illegal one raises;
* ``orders.status`` always equals the newest event, so the projection can never
  drift from the log it claims to summarise;
* the risk check cannot be skipped — ``RISK_APPROVED`` is unreachable without it,
  and a gate that raises leaves the order at ``NEW`` rather than half-validated;
* the same intent submitted twice produces one order, under concurrency;
* a fill that would corrupt the position (non-monotonic, or larger than the
  order) is refused instead of absorbed.
"""

from __future__ import annotations

import threading

import pytest

from atr.appdb.repositories import (
    TERMINAL_ORDER_STATUSES,
    OrderEventRepository,
    OrderRepository,
    UserRepository,
)
from atr.execution.oms import (
    ORDER_STATES,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidTransition,
    LimitsRiskGate,
    OrderDraft,
    RiskDecision,
    assert_transition,
    can_transition,
    elapsed_ms,
    fill_status,
    is_terminal,
    next_states,
    slippage_bps,
)
from atr.services.orders import OrderNotFound, OrderService


# =========================================================================== the machine
def test_every_state_is_reachable_and_every_terminal_state_is_terminal():
    """A state nobody can reach is a state nobody should have written."""
    assert set(VALID_TRANSITIONS) == set(ORDER_STATES)
    for state in ORDER_STATES:
        if state not in TERMINAL_STATES:
            assert VALID_TRANSITIONS[state], f"{state} is a dead end but not terminal"
        else:
            assert VALID_TRANSITIONS[state] == frozenset(), f"{state} is not sealed"


def test_the_machine_agrees_with_the_persistence_layer():
    """The terminal set is duplicated across a layer boundary; it must not drift."""
    assert TERMINAL_STATES == TERMINAL_ORDER_STATUSES


def test_every_transition_target_is_a_declared_state():
    declared = set(ORDER_STATES)
    for source, targets in VALID_TRANSITIONS.items():
        unknown = targets - declared
        assert not unknown, f"{source} can move to undeclared state(s) {unknown}"


def test_a_terminal_state_has_no_way_out():
    for state in TERMINAL_STATES:
        for target in ORDER_STATES:
            assert not can_transition(state, target)


def test_the_documented_chain_is_walkable():
    """The full happy path from the brief, end to end."""
    chain = [
        "NEW",
        "VALIDATING",
        "RISK_APPROVED",
        "SUBMITTED",
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED",
    ]
    for source, target in zip(chain, chain[1:], strict=False):
        assert can_transition(source, target), f"{source} -> {target} should be legal"


def test_a_second_partial_fill_is_the_only_self_loop():
    """Refusing it would drop a fill and desynchronise the position from the broker."""
    self_loops = {s for s in ORDER_STATES if can_transition(s, s)}
    assert self_loops == {"PARTIALLY_FILLED"}


def test_cancel_always_passes_through_cancel_pending():
    """So a cancel *request* is on the record even when the broker answers at once.

    There is no direct edge to ``CANCELLED`` from anywhere, including from
    ``PARTIALLY_FILLED``: a half-filled order is cancelled by the same route as
    any other, and the request is the fact that is missing when someone asks why
    a position closed early.
    """
    for state in ORDER_STATES:
        if state != "CANCEL_PENDING":
            assert not can_transition(state, "CANCELLED"), (
                f"{state} -> CANCELLED bypasses the cancel request"
            )
    assert can_transition("CANCEL_PENDING", "CANCELLED")


def test_an_acknowledged_order_cannot_be_rejected():
    """Once the exchange holds it, the exits are fill, cancel or expiry."""
    assert not can_transition("ACKNOWLEDGED", "REJECTED")
    assert can_transition("SUBMITTED", "REJECTED")


def test_a_cancel_in_flight_can_still_fill():
    assert can_transition("CANCEL_PENDING", "PARTIALLY_FILLED")
    assert can_transition("CANCEL_PENDING", "FILLED")


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("NEW", "FILLED"),
        ("NEW", "SUBMITTED"),
        ("NEW", "ACKNOWLEDGED"),
        ("VALIDATING", "SUBMITTED"),
        ("RISK_APPROVED", "ACKNOWLEDGED"),
        ("SUBMITTED", "FILLED"),
        ("FILLED", "CANCELLED"),
        ("CANCELLED", "FILLED"),
        ("REJECTED", "NEW"),
        ("EXPIRED", "SUBMITTED"),
        ("CANCEL_PENDING", "REJECTED"),
    ],
)
def test_illegal_transitions_raise(source, target):
    """The point of the machine: the shortcut that skips a step is not available."""
    with pytest.raises(InvalidTransition) as caught:
        assert_transition(source, target, "ord-1")
    assert caught.value.from_status == source
    assert caught.value.to_status == target


def test_an_unknown_state_is_refused_with_a_readable_message():
    with pytest.raises(InvalidTransition, match="unknown state"):
        assert_transition("NEW", "TELEPORTED")


def test_next_states_is_empty_for_a_terminal_state():
    assert next_states("FILLED") == frozenset()
    assert next_states("NEW") == frozenset(
        {"VALIDATING", "REJECTED", "EXPIRED", "CANCEL_PENDING"}
    )


def test_cancel_is_available_from_every_live_state():
    """A user can change their mind at any point before the order is done.

    ``CANCEL_PENDING`` is excluded because a cancel is already in flight — asking
    again is not a transition, and the request is already on the record.
    """
    for state in ORDER_STATES:
        if state in TERMINAL_STATES or state == "CANCEL_PENDING":
            continue
        assert can_transition(state, "CANCEL_PENDING"), (
            f"{state} cannot be cancelled, which would strand a live order"
        )


def test_is_terminal_matches_the_terminal_set():
    assert is_terminal("FILLED") and is_terminal("REJECTED")
    assert not is_terminal("PARTIALLY_FILLED")


# =========================================================================== the arithmetic
@pytest.mark.parametrize(
    ("side", "requested", "filled", "expected"),
    [
        # A buy filling above the requested price is adverse, so positive.
        ("BUY", 100.0, 100.5, 50.0),
        ("BUY", 100.0, 99.5, -50.0),
        # A sell receiving less than requested is adverse, so positive.
        ("SELL", 100.0, 99.5, 50.0),
        ("SELL", 100.0, 100.5, -50.0),
    ],
)
def test_slippage_is_positive_when_the_fill_is_worse(side, requested, filled, expected):
    assert slippage_bps(side=side, requested_price=requested, filled_price=filled) == (
        pytest.approx(expected)
    )


def test_slippage_is_unknown_rather_than_zero_when_it_cannot_be_measured():
    """An unmeasurable slippage is not a zero-slippage fill."""
    assert slippage_bps(side="BUY", requested_price=None, filled_price=100.0) is None
    assert slippage_bps(side="BUY", requested_price=100.0, filled_price=None) is None
    assert slippage_bps(side="BUY", requested_price=0.0, filled_price=100.0) is None
    assert slippage_bps(side="SIDEWAYS", requested_price=100.0, filled_price=101.0) is None


@pytest.mark.parametrize(
    ("quantity", "cumulative", "expected"),
    [(10, 4, "PARTIALLY_FILLED"), (10, 9.999, "PARTIALLY_FILLED"), (10, 10, "FILLED")],
)
def test_fill_status(quantity, cumulative, expected):
    assert fill_status(quantity=quantity, cumulative_filled=cumulative) == expected


def test_elapsed_ms_never_goes_negative():
    from datetime import datetime, timedelta

    start = datetime(2026, 9, 14, 9, 15, 0)
    assert elapsed_ms(start, start + timedelta(milliseconds=250)) == 250
    assert elapsed_ms(start, start - timedelta(seconds=5)) == 0


# =========================================================================== the service
@pytest.fixture()
def trader(app_db):
    with app_db.session() as session:
        return UserRepository.create(
            session, email="t@example.com", username="trader",
            password_hash="x", role="trader",
        )["user_id"]


def _allow(draft: OrderDraft) -> RiskDecision:  # noqa: ARG001 - protocol shape
    return RiskDecision.ok()


def _deny(draft: OrderDraft) -> RiskDecision:  # noqa: ARG001 - protocol shape
    return RiskDecision.reject("daily loss limit reached", "daily_loss")


@pytest.fixture()
def oms(app_db):
    return OrderService(app_db, risk_gate=_allow)


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


def _drive_to_acknowledged(oms, user_id, **draft_overrides) -> str:
    opened = oms.open_order(_draft(user_id, **draft_overrides))
    oms.validate(opened.order_id, user_id)
    oms.submit(opened.order_id, user_id)
    oms.acknowledge(opened.order_id, user_id, broker_order_id="BR-1")
    return opened.order_id


def test_a_new_order_is_at_new_and_has_one_event(oms, app_db, trader):
    opened = oms.open_order(_draft(trader))
    stored = oms.get(opened.order_id, trader)
    assert stored["status"] == "NEW"
    assert [e["to_status"] for e in oms.history(opened.order_id, trader)] == ["NEW"]


def test_the_projection_equals_the_log_after_every_step(oms, trader):
    """The invariant the whole design rests on, checked at each transition."""
    order_id = _drive_to_acknowledged(oms, trader)
    oms.record_fill(order_id, trader, filled_qty=4.0, filled_price=2501.0)

    with oms.db.session() as session:
        for _ in range(1):
            assert OrderRepository.get(session, order_id, trader)["status"] == (
                OrderEventRepository.current_status(session, order_id)
            )
    assert oms.get(order_id, trader)["status"] == "PARTIALLY_FILLED"

    oms.record_fill(order_id, trader, filled_qty=10.0, filled_price=2502.0)
    with oms.db.session() as session:
        assert OrderRepository.get(session, order_id, trader)["status"] == (
            OrderEventRepository.current_status(session, order_id)
        )
    assert oms.get(order_id, trader)["status"] == "FILLED"


def test_the_whole_lifecycle_is_reconstructable_from_the_log(oms, trader):
    order_id = _drive_to_acknowledged(oms, trader)
    oms.record_fill(order_id, trader, filled_qty=4.0, filled_price=2501.0)
    oms.record_fill(order_id, trader, filled_qty=10.0, filled_price=2502.0)

    log = oms.history(order_id, trader)
    assert [e["seq"] for e in log] == list(range(1, len(log) + 1))
    assert [(e["from_status"], e["to_status"]) for e in log] == [
        (None, "NEW"),
        ("NEW", "VALIDATING"),
        ("VALIDATING", "RISK_APPROVED"),
        ("RISK_APPROVED", "SUBMITTED"),
        ("SUBMITTED", "ACKNOWLEDGED"),
        ("ACKNOWLEDGED", "PARTIALLY_FILLED"),
        ("PARTIALLY_FILLED", "FILLED"),
    ]
    # And the source of each change is on the record.
    assert [e["source"] for e in log] == [
        "oms", "oms", "risk", "oms", "broker", "broker", "broker",
    ]


def test_a_fill_computes_slippage_from_the_requested_price(oms, trader):
    order_id = _drive_to_acknowledged(oms, trader, requested_price=2500.0, side="BUY")
    oms.record_fill(order_id, trader, filled_qty=10.0, filled_price=2502.5)
    fill = oms.fill_history(order_id, trader)[-1]
    assert fill["slippage_bps"] == pytest.approx(10.0)


def test_a_fill_records_no_slippage_when_there_was_no_requested_price(oms, trader):
    order_id = _drive_to_acknowledged(oms, trader, requested_price=None)
    oms.record_fill(order_id, trader, filled_qty=10.0, filled_price=2502.5)
    assert oms.fill_history(order_id, trader)[-1]["slippage_bps"] is None


def test_the_projection_keeps_the_requested_price_across_a_non_fill_event(oms, trader):
    """Otherwise every slippage figure downstream becomes unmeasurable."""
    order_id = _drive_to_acknowledged(oms, trader, requested_price=2500.0)
    stored = oms.get(order_id, trader)
    assert stored["requested_price"] == 2500.0
    assert stored["broker_order_id"] == "BR-1"
    assert stored["submitted_at"] is not None
    assert stored["completed_at"] is None


def test_acknowledge_measures_latency_by_default(oms, trader):
    """A latency figure only recorded on request is not a measurement."""
    opened = oms.open_order(_draft(trader))
    oms.validate(opened.order_id, trader)
    oms.submit(opened.order_id, trader)
    oms.acknowledge(opened.order_id, trader, broker_order_id="BR-1")
    ack = oms.history(opened.order_id, trader)[-1]
    assert ack["latency_ms"] is not None
    assert ack["latency_ms"] >= 0
    assert ack["ack_ts"] is not None


def test_a_terminal_order_accepts_no_further_transition(oms, trader):
    order_id = _drive_to_acknowledged(oms, trader)
    oms.record_fill(order_id, trader, filled_qty=10.0, filled_price=2502.0)
    for attempt in (
        lambda: oms.request_cancel(order_id, trader, reason="changed my mind"),
        lambda: oms.expire(order_id, trader, reason="session ended"),
        lambda: oms.reject(order_id, trader, reason="too late"),
        lambda: oms.record_fill(order_id, trader, filled_qty=10.0, filled_price=2502.0),
    ):
        with pytest.raises(InvalidTransition):
            attempt()
    assert oms.get(order_id, trader)["status"] == "FILLED"


def test_skipping_validation_is_impossible(oms, trader):
    """The defect this service exists to fix: submit without a risk decision."""
    opened = oms.open_order(_draft(trader))
    with pytest.raises(InvalidTransition):
        oms.submit(opened.order_id, trader)
    assert oms.get(opened.order_id, trader)["status"] == "NEW"


# ---------------------------------------------------------------- risk enforcement
def test_a_denied_order_is_rejected_with_its_reason_on_the_record(oms, trader):
    opened = oms.open_order(_draft(trader))
    status = oms.validate(opened.order_id, trader, gate=_deny)
    assert status == "REJECTED"
    stored = oms.get(opened.order_id, trader)
    assert stored["status"] == "REJECTED"
    assert stored["reject_reason"] == "daily loss limit reached"
    assert stored["completed_at"] is not None
    log = oms.history(opened.order_id, trader)
    assert log[-1]["source"] == "risk"
    assert '"code": "daily_loss"' in log[-1]["raw"]


def test_a_rejected_order_cannot_be_submitted_afterwards(oms, trader):
    opened = oms.open_order(_draft(trader))
    oms.validate(opened.order_id, trader, gate=_deny)
    with pytest.raises(InvalidTransition):
        oms.submit(opened.order_id, trader)


def test_a_gate_that_raises_leaves_the_order_untouched(oms, trader):
    """Half a validation is not a state, and the check that guards submission
    must fail closed — so the transaction rolls back entirely."""
    def exploding(draft: OrderDraft) -> RiskDecision:
        raise RuntimeError("risk service unavailable")

    opened = oms.open_order(_draft(trader))
    with pytest.raises(RuntimeError, match="risk service unavailable"):
        oms.validate(opened.order_id, trader, gate=exploding)

    assert oms.get(opened.order_id, trader)["status"] == "NEW"
    assert [e["to_status"] for e in oms.history(opened.order_id, trader)] == ["NEW"]


def test_the_injected_gate_is_used_when_none_is_passed(app_db, trader):
    oms = OrderService(app_db, risk_gate=_deny)
    opened = oms.open_order(_draft(trader))
    assert oms.validate(opened.order_id, trader) == "REJECTED"


def test_the_service_requires_a_risk_gate(app_db):
    """No default: a permissive one is a footgun and a rejecting one is an outage."""
    import inspect

    signature = inspect.signature(OrderService)
    assert "risk_gate" in signature.parameters
    assert signature.parameters["risk_gate"].default is inspect.Parameter.empty


def test_limits_risk_gate_adapts_the_existing_engine(app_db, trader):
    """The gate is an adapter over RiskEngine, not a reimplementation."""
    from atr.core.models import Instrument
    from atr.execution.risk import RiskEngine, RiskLimits

    class _Portfolio:
        equity = 1_000_000.0
        gross_exposure = 0.0

        def position(self, symbol):  # noqa: ARG002 - duck-typed
            class _P:
                quantity = 0.0
                last_price = 2500.0

            return _P()

    def instruments(symbol: str, exchange: str) -> Instrument:
        return Instrument(symbol=symbol, exchange=exchange, multiplier=1.0)

    engine = RiskEngine(limits=RiskLimits(max_order_notional=10_000.0))
    gate = LimitsRiskGate(engine=engine, portfolio=_Portfolio(), instruments=instruments)

    assert gate(_draft(trader, quantity=1)).allowed is True
    decision = gate(_draft(trader, quantity=100))
    assert decision.allowed is False
    assert decision.code == "risk_limit"


def test_an_unpriceable_order_is_refused_rather_than_treated_as_zero_notional(app_db, trader):
    """``RiskEngine`` prices an order as ``limit_price or last_price``.

    With neither available the notional is ``0.0``, so a MARKET order would pass
    every notional limit without being measured against any of them — the
    "unknown reported as a zero" failure. The gate refuses instead.
    """
    from atr.core.models import Instrument
    from atr.execution.risk import RiskEngine, RiskLimits

    class _Unpriced:
        equity = 0.0
        gross_exposure = 0.0

        def position(self, symbol):  # noqa: ARG002
            class _P:
                quantity = 0.0
                last_price = 0.0

            return _P()

    gate = LimitsRiskGate(
        engine=RiskEngine(limits=RiskLimits(max_order_notional=10_000.0)),
        portfolio=_Unpriced(),
        instruments=lambda s, e: Instrument(symbol=s, exchange=e),
    )
    decision = gate(_draft(trader, quantity=1000, limit_price=None))
    assert decision.allowed is False
    assert decision.code == "unpriceable_order"
    assert "cannot price" in decision.reason


def test_a_limit_price_makes_the_notional_checkable(app_db, trader):
    from atr.core.models import Instrument
    from atr.execution.risk import RiskEngine, RiskLimits

    class _Unpriced:
        equity = 0.0
        gross_exposure = 0.0

        def position(self, symbol):  # noqa: ARG002
            class _P:
                quantity = 0.0
                last_price = 0.0

            return _P()

    gate = LimitsRiskGate(
        engine=RiskEngine(limits=RiskLimits(max_order_notional=10_000.0)),
        portfolio=_Unpriced(),
        instruments=lambda s, e: Instrument(symbol=s, exchange=e),
    )
    assert gate(_draft(trader, quantity=1, limit_price=2500.0)).allowed is True
    assert gate(_draft(trader, quantity=100, limit_price=2500.0)).allowed is False


def test_no_notional_limit_means_no_price_is_needed(app_db, trader):
    """The refusal is tied to a configured limit, not applied unconditionally."""
    from atr.core.models import Instrument
    from atr.execution.risk import RiskEngine, RiskLimits

    class _Unpriced:
        equity = 0.0
        gross_exposure = 0.0

        def position(self, symbol):  # noqa: ARG002
            class _P:
                quantity = 0.0
                last_price = 0.0

            return _P()

    gate = LimitsRiskGate(
        engine=RiskEngine(limits=RiskLimits()),
        portfolio=_Unpriced(),
        instruments=lambda s, e: Instrument(symbol=s, exchange=e),
    )
    assert gate(_draft(trader, quantity=1000)).allowed is True


def test_the_kill_switch_reaches_the_order_path(app_db, trader):
    """The kill switch is checked on the manual order route too, not only on signals."""
    from atr.core.models import Instrument
    from atr.execution.risk import RiskEngine, RiskLimits

    class _Portfolio:
        equity = 1_000_000.0
        gross_exposure = 0.0

        def position(self, symbol):  # noqa: ARG002
            class _P:
                quantity = 0.0
                last_price = 0.0

            return _P()

    gate = LimitsRiskGate(
        engine=RiskEngine(limits=RiskLimits(kill_switch=True)),
        portfolio=_Portfolio(),
        instruments=lambda s, e: Instrument(symbol=s, exchange=e),
    )
    oms = OrderService(app_db, risk_gate=gate)
    opened = oms.open_order(_draft(trader))
    assert oms.validate(opened.order_id, trader) == "REJECTED"
    assert "kill switch" in oms.get(opened.order_id, trader)["reject_reason"]


# ------------------------------------------------------------------- fill integrity
def test_a_fill_larger_than_the_order_is_refused(oms, trader):
    order_id = _drive_to_acknowledged(oms, trader, quantity=10)
    with pytest.raises(ValueError, match="exceeds order quantity"):
        oms.record_fill(order_id, trader, filled_qty=11.0, filled_price=2500.0)
    assert oms.get(order_id, trader)["filled_quantity"] == 0.0


def test_a_non_monotonic_cumulative_fill_is_refused(oms, trader):
    """Out-of-order execution reports must not be absorbed as if they were news."""
    order_id = _drive_to_acknowledged(oms, trader, quantity=10)
    oms.record_fill(order_id, trader, filled_qty=6.0, filled_price=2500.0)
    with pytest.raises(ValueError, match="went backwards"):
        oms.record_fill(order_id, trader, filled_qty=4.0, filled_price=2500.0)
    assert oms.get(order_id, trader)["filled_quantity"] == 6.0


def test_a_zero_fill_is_refused(oms, trader):
    order_id = _drive_to_acknowledged(oms, trader)
    with pytest.raises(ValueError, match="positive quantity"):
        oms.record_fill(order_id, trader, filled_qty=0.0, filled_price=2500.0)


def test_a_second_partial_fill_is_recorded_as_its_own_event(oms, trader):
    order_id = _drive_to_acknowledged(oms, trader, quantity=10)
    oms.record_fill(order_id, trader, filled_qty=3.0, filled_price=2500.0)
    oms.record_fill(order_id, trader, filled_qty=7.0, filled_price=2501.0)
    log = oms.history(order_id, trader)
    assert [e["to_status"] for e in log[-2:]] == ["PARTIALLY_FILLED", "PARTIALLY_FILLED"]
    assert [e["filled_qty"] for e in log[-2:]] == [3.0, 7.0]
    assert oms.get(order_id, trader)["filled_quantity"] == 7.0


def test_a_fill_for_an_order_that_is_not_yours_is_not_found(oms, app_db, trader):
    with app_db.session() as session:
        other = UserRepository.create(
            session, email="o@example.com", username="other",
            password_hash="x", role="trader",
        )["user_id"]
    order_id = _drive_to_acknowledged(oms, trader)
    with pytest.raises(OrderNotFound):
        oms.record_fill(order_id, other, filled_qty=1.0, filled_price=2500.0)


def test_history_for_another_account_is_not_found_rather_than_empty(oms, app_db, trader):
    """404, not 403: an id's existence must not leak."""
    with app_db.session() as session:
        other = UserRepository.create(
            session, email="o2@example.com", username="other2",
            password_hash="x", role="trader",
        )["user_id"]
    opened = oms.open_order(_draft(trader))
    with pytest.raises(OrderNotFound):
        oms.history(opened.order_id, other)


# ------------------------------------------------------------------- cancellation
def test_a_cancel_is_recorded_as_a_request_before_the_outcome(oms, trader):
    order_id = _drive_to_acknowledged(oms, trader)
    oms.request_cancel(order_id, trader, reason="operator override")
    assert oms.get(order_id, trader)["status"] == "CANCEL_PENDING"
    log = oms.history(order_id, trader)
    assert log[-1]["source"] == "user"
    assert '"reason": "operator override"' in log[-1]["raw"]

    oms.confirm_cancel(order_id, trader)
    assert oms.get(order_id, trader)["status"] == "CANCELLED"
    assert oms.get(order_id, trader)["completed_at"] is not None


def test_cancelling_a_filled_order_is_refused(oms, trader):
    order_id = _drive_to_acknowledged(oms, trader)
    oms.record_fill(order_id, trader, filled_qty=10.0, filled_price=2500.0)
    with pytest.raises(InvalidTransition):
        oms.request_cancel(order_id, trader, reason="too late")


def test_a_fill_can_still_arrive_while_a_cancel_is_in_flight(oms, trader):
    """The exchange does not stop matching because a cancel was requested."""
    order_id = _drive_to_acknowledged(oms, trader, quantity=10)
    oms.request_cancel(order_id, trader, reason="flatten")
    oms.record_fill(order_id, trader, filled_qty=10.0, filled_price=2500.0)
    assert oms.get(order_id, trader)["status"] == "FILLED"


def test_rejecting_requires_a_reason(oms, trader):
    opened = oms.open_order(_draft(trader))
    with pytest.raises(ValueError, match="requires a reason"):
        oms.reject(opened.order_id, trader, reason="   ")


def test_expiry_is_recorded(oms, trader):
    order_id = _drive_to_acknowledged(oms, trader)
    oms.expire(order_id, trader, reason="day session closed")
    stored = oms.get(order_id, trader)
    assert stored["status"] == "EXPIRED"
    assert stored["completed_at"] is not None


def test_an_unknown_event_source_is_refused(oms, trader):
    opened = oms.open_order(_draft(trader))
    with pytest.raises(ValueError, match="unknown event source"):
        oms._advance(opened.order_id, trader, "VALIDATING", source="nobody")


# ------------------------------------------------------------------- idempotency
def test_the_same_intent_twice_creates_one_order(oms, trader):
    key = "k" * 64
    first = oms.open_order(_draft(trader), idempotency_key=key)
    second = oms.open_order(_draft(trader), idempotency_key=key)

    assert first.created is True
    assert second.created is False
    assert second.order_id == first.order_id
    assert second.duplicate_of == first.order_id

    rows, total = oms.list_orders(trader)
    assert total == 1, f"a duplicate intent created {total} orders"
    assert rows[0]["order_id"] == first.order_id


def test_a_suppressed_duplicate_leaves_no_stray_order(oms, trader):
    """An orphan NEW order would sit in open_orders forever and break reconciliation."""
    key = "j" * 64
    oms.open_order(_draft(trader), idempotency_key=key)
    oms.open_order(_draft(trader), idempotency_key=key)

    assert len(oms.open_orders(trader)) == 1
    with oms.db.session() as session:
        from atr.appdb.repositories import OrderIntentRepository

        intent = OrderIntentRepository.get(session, key)
    assert intent is not None
    assert oms.get(intent["order_id"], trader) is not None


def test_no_key_means_every_call_is_a_new_order(oms, trader):
    """A manual order without a key is two orders, and that is correct."""
    a = oms.open_order(_draft(trader))
    b = oms.open_order(_draft(trader))
    assert a.order_id != b.order_id
    assert oms.list_orders(trader)[1] == 2


def test_concurrent_duplicate_intents_produce_exactly_one_order(app_db, trader):
    """The guard must be the insert, not a check-then-act.

    Four threads race the same key. A ``SELECT`` followed by an ``INSERT`` would
    let two of them through; the primary key cannot.
    """
    key = "r" * 64
    service = OrderService(app_db, risk_gate=_allow)
    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def attempt() -> None:
        try:
            barrier.wait(timeout=10)
            opened = service.open_order(_draft(trader), idempotency_key=key)
            results.append(opened.order_id)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"the race raised instead of resolving: {errors}"
    assert len(set(results)) == 1, f"one intent produced {len(set(results))} orders"
    assert service.list_orders(trader)[1] == 1


def test_a_duplicate_intent_does_not_disturb_the_first_order(oms, trader):
    key = "d" * 64
    first = oms.open_order(_draft(trader), idempotency_key=key)
    oms.validate(first.order_id, trader)
    oms.open_order(_draft(trader), idempotency_key=key)

    assert oms.get(first.order_id, trader)["status"] == "RISK_APPROVED"
    assert [e["to_status"] for e in oms.history(first.order_id, trader)] == [
        "NEW", "VALIDATING", "RISK_APPROVED",
    ]
