"""Live-order protections that do not depend on the operator having remembered to configure them.

A risk engine whose limits are all opt-in protects nobody on day one: with nothing configured a
mistyped quantity is unbounded, and an order can be sent on a Sunday. These pin the defaults, the
market-hours guard, and the duplicate guard for manual orders.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from atr.appdb.repositories import UserRepository
from atr.execution.oms import OrderDraft, RiskDecision, manual_idempotency_key
from atr.market_calendar import IST
from atr.services.execution import VENUE_ACCEPTED, ExecutionService, VenueOutcome
from atr.services.orders import OrderService
from atr.services.risk import LiveGuards, RiskStateService, nse_accepting_orders

MON_10AM = datetime(2026, 7, 6, 10, 30, tzinfo=IST)  # a trading Monday


def at(day, h, m=0):
    return datetime(2026, 7, day, h, m, tzinfo=IST)


@pytest.fixture()
def trader(app_db):
    with app_db.session() as session:
        return UserRepository.create(session, email="t@example.com", username="t", password_hash="x", role="trader")["user_id"]


def _draft(user_id, **kw):
    base = {"user_id": user_id, "symbol": "RELIANCE", "side": "BUY", "quantity": 10, "mode": "LIVE",
            "order_type": "LIMIT", "limit_price": 2500.0, "requested_price": 2500.0}
    base.update(kw)
    return OrderDraft(**base)


class TestMarketHours:
    @pytest.mark.parametrize(("when", "open_"), [
        (at(6, 10, 30), True),     # Monday mid-session
        (at(6, 9, 0), True),       # pre-open opens the window
        (at(6, 15, 30), True),
        (at(6, 8, 59), False),
        (at(6, 15, 31), False),
        (at(4, 11, 0), False),     # a Saturday
        (at(5, 11, 0), False),     # a Sunday
    ])
    def test_the_window(self, when, open_):
        assert nse_accepting_orders(when) is open_

    def test_an_exchange_holiday_is_closed_even_in_hours(self):
        assert nse_accepting_orders(datetime(2026, 10, 2, 11, 0, tzinfo=IST)) is False  # Gandhi Jayanti

    def test_a_utc_timestamp_is_read_in_ist(self):
        assert nse_accepting_orders(datetime(2026, 7, 6, 5, 0, tzinfo=UTC)) is True    # 10:30 IST
        assert nse_accepting_orders(datetime(2026, 7, 6, 11, 0, tzinfo=UTC)) is False  # 16:30 IST

    def _guard(self, now, enforce=True):
        seen = []
        inner = lambda d: (seen.append(d), RiskDecision.ok())[1]  # noqa: E731
        return LiveGuards(inner=inner, enforce_market_hours=enforce, now=lambda: now), seen

    def test_a_live_order_when_closed_is_refused_before_anything_else_runs(self):
        guard, seen = self._guard(at(4, 11, 0))
        verdict = guard(_draft("u"))
        assert (verdict.allowed, verdict.code) == (False, "market_closed") and seen == []

    def test_a_live_order_in_hours_reaches_the_limits_gate(self):
        guard, seen = self._guard(MON_10AM)
        assert guard(_draft("u")).allowed and len(seen) == 1

    def test_a_paper_order_is_never_blocked_by_market_hours(self):
        guard, seen = self._guard(at(4, 11, 0))
        assert guard(_draft("u", mode="PAPER")).allowed and len(seen) == 1

    def test_the_guard_can_be_switched_off_deliberately(self):
        guard, _ = self._guard(at(4, 11, 0), enforce=False)
        assert guard(_draft("u")).allowed


class TestDefaultCap:
    def _service(self, app_db, trader):
        risk = RiskStateService(app_db)
        risk.set_execution_mode("live", reason="test", actor="t")
        sent = []

        class Venue:
            def submit(self, d):
                sent.append(d)
                return VenueOutcome(status=VENUE_ACCEPTED, broker_order_id="B1")

        gate = risk.gate(portfolio=_Flat(), instruments=_instruments, now=lambda: MON_10AM)
        return ExecutionService(orders=OrderService(app_db, risk_gate=gate), venue=Venue()), sent, risk

    def test_with_no_limit_set_a_big_live_order_is_refused(self, app_db, trader):
        svc, sent, _ = self._service(app_db, trader)
        r = svc.place(_draft(trader, quantity=1000, limit_price=2500.0))  # 25 lakh
        assert r.status == "REJECTED" and sent == []
        assert "exceeds" in (r.reject_reason or "")

    def test_a_small_live_order_is_allowed(self, app_db, trader):
        svc, sent, _ = self._service(app_db, trader)
        assert svc.place(_draft(trader, quantity=10)).status != "REJECTED" and len(sent) == 1

    def test_a_market_order_with_no_price_is_refused_not_waved_through(self, app_db, trader):
        svc, sent, _ = self._service(app_db, trader)
        r = svc.place(_draft(trader, order_type="MARKET", limit_price=None, requested_price=None))
        assert r.status == "REJECTED" and sent == []

    def test_the_operators_own_limit_replaces_the_default(self, app_db, trader):
        svc, sent, risk = self._service(app_db, trader)
        risk.set_limits({"max_order_notional": 3_000_000}, actor="t")
        svc2, sent2, _ = self._service(app_db, trader)
        assert svc2.place(_draft(trader, quantity=1000)).status != "REJECTED" and len(sent2) == 1

    def test_paper_orders_are_not_capped(self, app_db, trader):
        risk = RiskStateService(app_db)  # paper by default
        gate = risk.gate(portfolio=_Flat(), instruments=_instruments, now=lambda: at(4, 11, 0))
        assert gate(_draft(trader, mode="PAPER", quantity=100000)).allowed


class TestWhatTheUiIsTold:
    def test_live_reports_the_default_cap_and_the_missing_daily_loss_limit(self, app_db):
        risk = RiskStateService(app_db)
        risk.set_execution_mode("live", reason="t", actor="t")
        prot = risk.snapshot().as_dict()["live_protections"]
        assert prot["defaults_applied"] == {"max_order_notional": 200_000.0}
        assert prot["warnings"] == ["no_daily_loss_limit"]

    def test_setting_the_limits_clears_the_default_and_the_warning(self, app_db):
        risk = RiskStateService(app_db)
        risk.set_limits({"max_order_notional": 500_000, "max_daily_loss": 25_000}, actor="t")
        prot = risk.snapshot().as_dict()["live_protections"]
        assert prot["defaults_applied"] == {} and prot["warnings"] == []


class TestDuplicateManualOrders:
    def test_the_same_client_id_is_the_same_order(self):
        a = manual_idempotency_key(user_id="u", client_order_id="abc-12345")
        assert a == manual_idempotency_key(user_id="u", client_order_id="abc-12345")
        assert a != manual_idempotency_key(user_id="u", client_order_id="abc-12346")
        assert a != manual_idempotency_key(user_id="v", client_order_id="abc-12345")

    def test_a_double_click_places_one_order_and_a_second_deliberate_order_places_another(self, app_db, trader):
        sent = []

        class Venue:
            def submit(self, d):
                sent.append(d)
                return VenueOutcome(status=VENUE_ACCEPTED, broker_order_id=f"B{len(sent)}")

        svc = ExecutionService(orders=OrderService(app_db, risk_gate=lambda d: RiskDecision.ok()), venue=Venue())
        key = manual_idempotency_key(user_id=trader, client_order_id="click-0001")
        first = svc.place(_draft(trader), idempotency_key=key)
        again = svc.place(_draft(trader), idempotency_key=key)
        assert first.created and again.duplicate and again.order_id == first.order_id and len(sent) == 1
        other = svc.place(_draft(trader), idempotency_key=manual_idempotency_key(user_id=trader, client_order_id="click-0002"))
        assert other.created and other.order_id != first.order_id and len(sent) == 2


# -- helpers -----------------------------------------------------------------
class _Flat:
    """A portfolio holding nothing, which is all the notional checks need."""

    def position(self, symbol):
        from atr.core.models import Instrument, Position

        return Position(instrument=Instrument(symbol=symbol))


def _instruments(symbol, exchange):
    from atr.core.models import Instrument

    return Instrument(symbol=symbol, exchange=exchange, currency="INR")


# -- daily loss enforcement on the live gate ----------------------------------
class _Book:
    def __init__(self, held=0.0, pnl=0.0, readable=True):
        self.held, self.pnl, self.readable = held, pnl, readable

    def position(self, symbol):
        from atr.core.models import Instrument, Position

        return Position(instrument=Instrument(symbol=symbol), quantity=self.held, avg_price=100.0, last_price=100.0)

    def day_pnl(self):
        return self.pnl if self.readable else None


def _gate(book, limit):
    from atr.execution.oms import LimitsRiskGate
    from atr.execution.risk import RiskEngine, RiskLimits

    return LimitsRiskGate(engine=RiskEngine(RiskLimits(max_daily_loss=limit)), portfolio=book, instruments=_instruments)


def _d(side="BUY"):
    return OrderDraft(user_id="u", symbol="INFY", exchange="NSE", side=side, quantity=1, order_type="LIMIT", limit_price=100.0)


class TestDailyLoss:
    def test_new_buy_refused_after_limit(self):
        r = _gate(_Book(pnl=-5000), 5000)(_d())
        assert not r.allowed and r.code == "daily_loss_limit"

    def test_exit_allowed(self):
        assert _gate(_Book(held=10, pnl=-9000), 5000)(_d("SELL")).allowed

    def test_under_limit_and_no_limit_allowed(self):
        assert _gate(_Book(pnl=-100), 5000)(_d()).allowed
        assert _gate(_Book(pnl=-9e9), None)(_d()).allowed

    def test_unreadable_book_fails_closed(self):
        r = _gate(_Book(readable=False), 5000)(_d())
        assert not r.allowed and r.code == "daily_loss_unknown"


class TestPriceBand:
    def test_far_limit_refused_near_allowed(self):
        far = OrderDraft(user_id="u", symbol="INFY", exchange="NSE", side="BUY", quantity=1, order_type="LIMIT", limit_price=1000.0)
        r = _gate(_Book(), None)(far)
        assert not r.allowed and r.code == "price_band"
        assert _gate(_Book(), None)(_d()).allowed


class _Multi(_Book):
    def __init__(self, held_map, gross=0.0):
        super().__init__()
        self.held_map, self.gross_exposure = held_map, gross

    @property
    def positions(self):
        return {s: self.position(s) for s in self.held_map}

    def position(self, symbol):
        from atr.core.models import Instrument, Position

        return Position(instrument=Instrument(symbol=symbol), quantity=self.held_map.get(symbol, 0.0), avg_price=100.0, last_price=100.0)


def _gate2(book, **limits):
    from atr.execution.oms import LimitsRiskGate
    from atr.execution.risk import RiskEngine, RiskLimits

    return LimitsRiskGate(engine=RiskEngine(RiskLimits(**limits)), portfolio=book, instruments=_instruments)


class TestBookLimits:
    def test_open_positions_cap(self):
        book = _Multi({"A": 1, "B": 1})
        r = _gate2(book, max_open_positions=2)(_d())
        assert not r.allowed and r.code == "max_open_positions"
        assert _gate2(_Multi({"A": 1}), max_open_positions=2)(_d()).allowed

    def test_adding_to_existing_name_is_not_a_new_position(self):
        assert _gate2(_Multi({"INFY": 5, "B": 1}), max_open_positions=2)(_d()).allowed

    def test_gross_exposure_cap_and_exit(self):
        r = _gate2(_Multi({"A": 1}, gross=99_950.0), max_gross_exposure=100_000.0)(_d())
        assert not r.allowed and r.code == "max_gross_exposure"
        assert _gate2(_Multi({"INFY": 5}, gross=99_950.0), max_gross_exposure=100_000.0)(_d("SELL")).allowed

    def test_unreadable_book_fails_closed(self):
        book = _Multi({})
        book.readable = False
        r = _gate2(book, max_open_positions=3)(_d())
        assert not r.allowed and r.code == "book_unknown"
