"""Reconciliation: the comparisons, the severity rules, and the refusals.

The property under test throughout is **asymmetry**. Reconciliation is the only
control that compares the platform's belief against an outside source, so the tests
care most about the cases where the platform is wrong and would otherwise never find
out:

* an order the broker holds and the platform does not know about — the worst case,
  because something is live that nothing is managing;
* a position that disagrees, because every risk limit is measured against it;
* a scope that could not be compared, which must **not** come back as ``ok``.

That last one is the test that matters most. A run that reports clean while quietly
skipping half its scopes is worse than one that reports a problem, because it is
believed.
"""

from __future__ import annotations

import pytest

from atr.appdb.repositories import UserRepository
from atr.core.models import Funds, Holding, Instrument, Order, Position
from atr.services.reconcile import (
    SCOPE_FUNDS,
    SCOPE_HOLDINGS,
    SCOPE_ORDERS,
    SCOPE_POSITIONS,
    SCOPES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARNING,
    ReconciliationService,
    compare_funds,
    compare_holdings,
    compare_orders,
    compare_positions,
)


@pytest.fixture()
def trader(app_db):
    with app_db.session() as session:
        return UserRepository.create(
            session, email="r@example.com", username="recon",
            password_hash="x", role="trader",
        )["user_id"]


def _instrument(symbol: str) -> Instrument:
    return Instrument(symbol=symbol, exchange="NSEEQ")


def _order(symbol: str, broker_order_id: str | None) -> Order:
    return Order(
        instrument=_instrument(symbol),
        side=__import__("atr.core.enums", fromlist=["Side"]).Side.BUY,
        quantity=1,
        broker_order_id=broker_order_id,
    )


def _position(symbol: str, quantity: float) -> Position:
    return Position(instrument=_instrument(symbol), quantity=quantity)


def _holding(symbol: str, quantity: float) -> Holding:
    return Holding(instrument=_instrument(symbol), quantity=quantity)


# =========================================================================== orders
def test_matching_open_orders_report_nothing():
    result = compare_orders(
        [{"broker_order_id": "B1", "symbol": "RELIANCE"}], [_order("RELIANCE", "B1")]
    )
    assert result.compared is True
    assert result.differences == []
    assert result.severity == SEVERITY_OK


def test_an_order_the_platform_does_not_know_about_is_critical():
    """The worst case: something is live that nothing is managing."""
    result = compare_orders([], [_order("RELIANCE", "B1")])
    assert result.severity == SEVERITY_CRITICAL
    assert result.differences[0].kind == "unknown_to_platform"
    assert "nothing is managing it" in result.differences[0].detail


def test_an_order_the_broker_does_not_have_is_critical():
    result = compare_orders([{"broker_order_id": "B9", "symbol": "TCS"}], [])
    assert result.severity == SEVERITY_CRITICAL
    assert result.differences[0].kind == "missing_at_broker"


def test_an_unacknowledged_order_is_not_counted():
    """Otherwise every order in flight would report a mismatch, which is most of
    the time — and a control that fires constantly is a control nobody reads."""
    result = compare_orders(
        [
            {"broker_order_id": None, "symbol": "RELIANCE"},
            {"broker_order_id": "B1", "symbol": "TCS"},
        ],
        [_order("TCS", "B1")],
    )
    assert result.differences == []
    assert result.platform_count == 1


def test_both_directions_are_reported_at_once():
    result = compare_orders(
        [{"broker_order_id": "A", "symbol": "X"}], [_order("Y", "B")]
    )
    assert len(result.differences) == 2
    assert {d.kind for d in result.differences} == {
        "unknown_to_platform", "missing_at_broker",
    }


# =========================================================================== positions
def test_matching_positions_report_nothing():
    result = compare_positions({"RELIANCE": 10.0}, [_position("RELIANCE", 10.0)])
    assert result.differences == []
    assert result.severity == SEVERITY_OK


def test_a_quantity_difference_is_critical():
    """Every risk limit is measured against the platform's figure."""
    result = compare_positions({"RELIANCE": 10.0}, [_position("RELIANCE", 12.0)])
    assert result.severity == SEVERITY_CRITICAL
    difference = result.differences[0]
    assert difference.platform == 10.0
    assert difference.broker == 12.0
    assert "every risk limit is measured against" in difference.detail


def test_a_position_only_the_broker_has_is_critical():
    result = compare_positions({}, [_position("INFY", 5.0)])
    assert result.severity == SEVERITY_CRITICAL
    assert result.differences[0].key == "INFY"


def test_a_position_only_the_platform_has_is_critical():
    result = compare_positions({"INFY": 5.0}, [])
    assert result.severity == SEVERITY_CRITICAL


def test_a_flat_broker_position_is_not_a_position():
    result = compare_positions({}, [_position("INFY", 0.0)])
    assert result.differences == []


def test_floating_point_noise_is_not_a_mismatch():
    result = compare_positions({"RELIANCE": 10.0}, [_position("RELIANCE", 10.0 + 1e-12)])
    assert result.differences == []


# =========================================================================== holdings
def test_a_holding_difference_is_a_warning_not_a_critical():
    """T+1 settlement makes this legitimate for a day after every trade."""
    result = compare_holdings({"RELIANCE": 10.0}, [_holding("RELIANCE", 0.0)])
    assert result.severity == SEVERITY_WARNING
    assert result.differences[0].severity == SEVERITY_WARNING
    assert "settlement" in result.differences[0].detail


def test_matching_holdings_report_nothing():
    result = compare_holdings({"RELIANCE": 10.0}, [_holding("RELIANCE", 10.0)])
    assert result.differences == []


# =========================================================================== funds
def test_funds_are_not_comparable_without_a_platform_cash_ledger():
    """The honest answer, not a clean comparison. The platform keeps no live cash."""
    result = compare_funds(None, Funds(available_cash=100_000.0))
    assert result.compared is False
    assert result.severity == SEVERITY_WARNING
    assert "no cash ledger" in result.skipped_reason


def test_funds_compare_when_both_sides_have_a_figure():
    assert compare_funds(100_000.0, Funds(available_cash=100_000.0)).differences == []
    mismatch = compare_funds(100_000.0, Funds(available_cash=90_000.0))
    assert mismatch.severity == SEVERITY_CRITICAL
    assert mismatch.differences[0].kind == "cash"


def test_rounding_is_not_a_cash_mismatch():
    assert compare_funds(100_000.0, Funds(available_cash=100_000.5)).differences == []


def test_funds_are_not_comparable_when_the_broker_is_silent():
    result = compare_funds(100_000.0, None)
    assert result.compared is False
    assert "did not report funds" in result.skipped_reason


# =========================================================================== the run
class _Broker:
    """A scripted broker. Every read can be made to fail independently."""

    def __init__(
        self,
        *,
        orders: list[Order] | None = None,
        positions: list[Position] | None = None,
        holdings: list[Holding] | None = None,
        funds: Funds | None = None,
        no_holdings: bool = False,
        no_funds: bool = False,
        raises: Exception | None = None,
    ) -> None:
        self._orders = orders or []
        self._positions = positions or []
        self._holdings = holdings or []
        self._funds = funds
        self._no_holdings = no_holdings
        self._no_funds = no_funds
        self._raises = raises

    def _check(self) -> None:
        if self._raises is not None:
            raise self._raises

    def open_orders(self) -> list[Order]:
        self._check()
        return self._orders

    def positions(self) -> list[Position]:
        self._check()
        return self._positions

    def holdings(self) -> list[Holding]:
        if self._no_holdings:
            raise NotImplementedError("this broker does not expose holdings")
        self._check()
        return self._holdings

    def funds(self) -> Funds | None:
        if self._no_funds:
            raise NotImplementedError("this broker does not expose funds")
        self._check()
        return self._funds


def test_a_clean_run_is_recorded_as_ok(app_db, trader):
    service = ReconciliationService(app_db)
    report = service.run(
        trader, broker=_Broker(no_holdings=True, no_funds=True), scopes=[SCOPE_ORDERS]
    )
    assert report.severity == SEVERITY_OK
    assert report.mismatch_count == 0
    assert report.run_id
    assert service.status(trader)["clear"] is True


def test_a_critical_run_raises_the_banner(app_db, trader):
    service = ReconciliationService(app_db)
    report = service.run(
        trader,
        broker=_Broker(orders=[_order("RELIANCE", "B1")]),
        scopes=[SCOPE_ORDERS],
    )
    assert report.severity == SEVERITY_CRITICAL

    status = service.status(trader)
    assert status["clear"] is False
    assert status["run"]["severity"] == SEVERITY_CRITICAL
    assert status["run"]["mismatches"] == 1


def test_a_later_clean_run_clears_the_banner(app_db, trader):
    service = ReconciliationService(app_db)
    service.run(trader, broker=_Broker(orders=[_order("X", "B1")]), scopes=[SCOPE_ORDERS])
    assert service.status(trader)["clear"] is False

    service.run(trader, broker=_Broker(), scopes=[SCOPE_ORDERS])
    assert service.status(trader)["clear"] is True


def test_an_incomparable_scope_is_never_recorded_as_ok(app_db, trader):
    """The failure this control exists to prevent: a clean-looking run that skipped
    half its scopes."""
    service = ReconciliationService(app_db)
    report = service.run(
        trader,
        broker=_Broker(no_holdings=True, no_funds=True),
        scopes=[SCOPE_ORDERS, SCOPE_HOLDINGS, SCOPE_FUNDS],
    )
    assert report.severity == SEVERITY_WARNING
    assert {e["scope"] for e in report.incomparable} == {SCOPE_HOLDINGS, SCOPE_FUNDS}

    stored = service.latest(trader)
    assert stored["severity"] == SEVERITY_WARNING
    # The reason is on the row, not just in the response.
    assert "holdings" in stored["detail"]
    assert service.status(trader)["clear"] is False


def test_a_broker_that_cannot_be_reached_is_recorded_not_raised(app_db, trader):
    """Reconciliation that throws is reconciliation that stops running."""
    service = ReconciliationService(app_db)
    report = service.run(
        trader, broker=_Broker(raises=ConnectionError("socket closed")),
        scopes=[SCOPE_ORDERS],
    )
    assert report.severity == SEVERITY_WARNING
    assert "socket closed" in report.error
    assert report.run_id
    assert service.status(trader)["clear"] is False


def test_a_failed_run_does_not_lose_its_scopes(app_db, trader):
    service = ReconciliationService(app_db)
    report = service.run(
        trader, broker=_Broker(raises=TimeoutError("timed out")), scopes=list(SCOPES)
    )
    assert len(report.results) == len(SCOPES)
    assert all(not r.compared for r in report.results)


def test_an_unknown_scope_is_refused(app_db, trader):
    service = ReconciliationService(app_db)
    with pytest.raises(ValueError, match="unknown reconciliation scope"):
        service.run(trader, broker=_Broker(), scopes=["orders", "vibes"])


def test_the_platform_position_fold_is_used(app_db, trader):
    """The platform's side is the order-event fold, the same one the risk gate gets."""
    from atr.execution.oms import OrderDraft, RiskDecision
    from atr.services.orders import OrderService

    service = OrderService(app_db, risk_gate=lambda d: RiskDecision.ok())
    opened = service.open_order(
        OrderDraft(user_id=trader, symbol="RELIANCE", side="BUY", quantity=10)
    )
    service.validate(opened.order_id, trader)
    service.submit(opened.order_id, trader)
    service.acknowledge(opened.order_id, trader, broker_order_id="B1")
    service.record_fill(opened.order_id, trader, filled_qty=10.0, filled_price=2500.0)

    assert ReconciliationService(app_db).platform_positions(trader) == {"RELIANCE": 10.0}

    # And the fold makes a broker disagreement visible.
    report = ReconciliationService(app_db).run(
        trader, broker=_Broker(positions=[_position("RELIANCE", 7.0)]),
        scopes=[SCOPE_POSITIONS],
    )
    assert report.severity == SEVERITY_CRITICAL
    assert report.differences[0].platform == 10.0


def test_the_audit_trail_records_the_run(app_db, trader):
    from atr.appdb.repositories import AuditRepository

    ReconciliationService(app_db).run(
        trader, broker=_Broker(orders=[_order("X", "B1")]), scopes=[SCOPE_ORDERS],
        actor="alice",
    )
    with app_db.session() as session:
        events, _ = AuditRepository.query(session, action="reconciliation.run")
    assert events
    assert events[0]["result"] == "failure"
    assert events[0]["actor"] == "alice"
    assert "orders/B1" in events[0]["detail"]


def test_a_clean_run_audits_as_success(app_db, trader):
    from atr.appdb.repositories import AuditRepository

    ReconciliationService(app_db).run(
        trader, broker=_Broker(), scopes=[SCOPE_ORDERS], actor="alice"
    )
    with app_db.session() as session:
        events, _ = AuditRepository.query(session, action="reconciliation.run")
    assert events[0]["result"] == "success"


def test_a_notifier_gets_the_report_and_cannot_break_the_run(app_db, trader):
    seen: list[str] = []

    def notifier(report) -> None:
        seen.append(report.severity)

    def exploding(report) -> None:  # noqa: ARG001
        raise RuntimeError("notification channel down")

    service = ReconciliationService(app_db)
    service.run(trader, broker=_Broker(orders=[_order("X", "B1")]),
                scopes=[SCOPE_ORDERS], notifier=notifier)
    assert seen == [SEVERITY_CRITICAL]

    # A failing notifier must not lose the run.
    report = service.run(trader, broker=_Broker(), scopes=[SCOPE_ORDERS],
                         notifier=exploding)
    assert report.run_id
    assert report.severity == SEVERITY_OK


def test_runs_are_user_scoped(app_db, trader):
    with app_db.session() as session:
        stranger = UserRepository.create(
            session, email="s@example.com", username="stranger",
            password_hash="x", role="trader",
        )["user_id"]
    service = ReconciliationService(app_db)
    service.run(trader, broker=_Broker(orders=[_order("X", "B1")]), scopes=[SCOPE_ORDERS])

    assert service.latest(stranger) is None
    assert service.history(stranger)[1] == 0
    assert service.status(stranger) == {"clear": True, "run": None}


def test_history_can_be_filtered_by_severity(app_db, trader):
    service = ReconciliationService(app_db)
    service.run(trader, broker=_Broker(), scopes=[SCOPE_ORDERS])
    service.run(trader, broker=_Broker(orders=[_order("X", "B1")]), scopes=[SCOPE_ORDERS])

    assert service.history(trader)[1] == 2
    assert service.history(trader, severity=SEVERITY_CRITICAL)[1] == 1
    assert service.history(trader, severity=SEVERITY_OK)[1] == 1
