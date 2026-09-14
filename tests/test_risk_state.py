"""Durable risk state: the kill switch, the execution mode, the limits.

The two properties worth testing are the ones the in-memory dict failed:

* **it survives a restart** — a fresh service over the same database still sees
  the kill switch engaged and the mode set to live;
* **the kill switch reaches the order path** — the gate the OMS is given is built
  from this state, so an order is refused without any route having to ask.

Plus the friction the brief asks for: the consequential transitions require a
non-empty reason, and the service enforces that rather than the route.
"""

from __future__ import annotations

from datetime import time

import pytest

from atr.services.risk import (
    CONFIGURABLE_LIMITS,
    RiskStateError,
    RiskStateService,
    limits_from_dict,
)


@pytest.fixture()
def risk(app_db) -> RiskStateService:
    return RiskStateService(app_db)


# =========================================================================== defaults
def test_the_defaults_are_safe(risk):
    """Paper, kill switch off, no limits configured. A fresh install cannot trade live."""
    state = risk.snapshot()
    assert state.execution_mode == "paper"
    assert state.live is False
    assert state.kill_switch is False
    assert state.changed_at is None


def test_an_unset_state_reads_as_paper_not_as_live(risk):
    """The failure direction matters: an unreadable mode must not mean 'live'."""
    assert risk.execution_mode() == "paper"
    assert risk.kill_switch_engaged() is False


# =========================================================================== kill switch
def test_the_kill_switch_can_be_engaged_and_released(risk):
    engaged = risk.set_kill_switch(True, reason="broker acting up", actor="operator")
    assert engaged.kill_switch is True
    assert risk.kill_switch_engaged() is True

    released = risk.set_kill_switch(False, reason="broker confirmed healthy", actor="operator")
    assert released.kill_switch is False


def test_the_kill_switch_requires_a_reason_in_both_directions(risk):
    """Releasing is the more consequential direction — it re-enables trading."""
    for engaged in (True, False):
        with pytest.raises(RiskStateError) as caught:
            risk.set_kill_switch(engaged, reason="   ", actor="operator")
        assert caught.value.code == "reason_required"


def test_the_kill_switch_records_who_and_why(risk):
    """An operator action with no actor is not an audit trail."""
    from atr.appdb.repositories import SystemStateRepository

    risk.set_kill_switch(True, reason="daily loss breached", actor="alice")
    with risk.db.session() as session:
        stored = SystemStateRepository.get(session, "risk.kill_switch")
    assert stored["updated_by"] == "alice"
    assert stored["reason"] == "daily loss breached"


def test_the_kill_switch_survives_a_restart(app_db, risk):
    """The bug the in-memory dict had: a bounce silently re-armed trading."""
    risk.set_kill_switch(True, reason="market dislocation", actor="operator")
    restarted = RiskStateService(app_db)
    assert restarted.kill_switch_engaged() is True


# =========================================================================== execution mode
def test_going_live_requires_a_reason(risk):
    with pytest.raises(RiskStateError) as caught:
        risk.set_execution_mode("live", reason="", actor="operator")
    assert caught.value.code == "reason_required"
    assert risk.execution_mode() == "paper"


def test_coming_back_to_paper_does_not_require_a_reason(risk):
    """Reducing risk must never be harder than taking it on."""
    risk.set_execution_mode("live", reason="validated on paper for 6 weeks", actor="operator")
    assert risk.execution_mode() == "live"
    back = risk.set_execution_mode("paper", reason="", actor="operator")
    assert back.execution_mode == "paper"


def test_an_unknown_mode_is_refused(risk):
    with pytest.raises(RiskStateError) as caught:
        risk.set_execution_mode("turbo", reason="why not", actor="operator")
    assert caught.value.code == "bad_mode"


def test_the_mode_change_records_the_previous_value(risk):
    risk.set_execution_mode("live", reason="going live for the session", actor="alice")
    state = risk.snapshot()
    assert state.changed_by == "alice"
    assert state.reason == "going live for the session"
    assert state.changed_at is not None
    assert state.changed_at.endswith("Z"), "the API attaches Z; storing a naive stamp would double it"


def test_the_execution_mode_survives_a_restart(app_db, risk):
    risk.set_execution_mode("live", reason="session start", actor="operator")
    assert RiskStateService(app_db).execution_mode() == "live"


# =========================================================================== limits
def test_limits_round_trip(risk):
    state = risk.set_limits(
        {
            "max_order_notional": 250_000,
            "max_daily_loss": 50_000,
            "max_daily_trades": 20,
            "allow_short": False,
            "allowed_symbols": ["RELIANCE", "TCS"],
        },
        actor="operator",
    )
    assert state.limits.max_order_notional == 250_000
    assert state.limits.max_daily_loss == 50_000
    assert state.limits.max_daily_trades == 20
    assert state.limits.allow_short is False
    assert state.limits.allowed_symbols == {"RELIANCE", "TCS"}


def test_the_default_unlimited_position_notional_is_not_serialised_as_infinity(risk):
    """``inf`` is not valid JSON and a JS client would fail on it."""
    import json

    payload = risk.snapshot().as_dict()
    assert payload["limits"]["max_position_notional"] is None
    json.dumps(payload)  # must not raise
    assert "Infinity" not in json.dumps(payload)


def test_an_unknown_limit_is_refused(risk):
    with pytest.raises(RiskStateError) as caught:
        risk.set_limits({"max_fun": 10}, actor="operator")
    assert caught.value.code == "unknown_limit"


def test_an_invalid_limit_value_is_refused_before_it_is_stored(risk):
    """A limit set that cannot be rebuilt would poison every later read."""
    with pytest.raises(RiskStateError) as caught:
        risk.set_limits({"max_daily_loss": "not a number"}, actor="operator")
    assert caught.value.code == "invalid_limit"
    # And the previous state is intact.
    assert risk.snapshot().limits.max_daily_loss is None


def test_limits_survive_a_restart(app_db, risk):
    risk.set_limits({"max_order_notional": 100_000}, actor="operator")
    assert RiskStateService(app_db).limits().max_order_notional == 100_000


def test_a_trading_window_round_trips(risk):
    state = risk.set_limits({"trading_window": ["09:15", "15:30"]}, actor="operator")
    assert state.limits.trading_window == (time(9, 15), time(15, 30))
    assert risk.snapshot().as_dict()["limits"]["trading_window"] == ["09:15:00", "15:30:00"]


def test_an_unparseable_trading_window_is_dropped_not_fatal():
    """A bad window must not take the whole risk state down with it."""
    assert limits_from_dict({"trading_window": "09:15-15:30"}).trading_window is None
    assert limits_from_dict({"trading_window": ["nonsense", "15:30"]}).trading_window is None
    assert limits_from_dict({"trading_window": ["15:30", "09:15"]}).trading_window is None


def test_a_stored_key_that_no_longer_exists_is_ignored():
    """Otherwise a stale row would raise on construction and lose the whole state."""
    limits = limits_from_dict({"max_daily_loss": 1000, "max_something_removed": 5})
    assert limits.max_daily_loss == 1000


def test_none_means_no_limit_rather_than_zero():
    """Zero would forbid every order; the two must not be confused."""
    limits = limits_from_dict({"max_order_notional": None, "max_daily_loss": 0})
    assert limits.max_order_notional is None
    assert limits.max_daily_loss == 0


def test_every_configurable_limit_is_a_real_field():
    """The allowlist and the dataclass cannot drift apart."""
    from atr.execution.risk import RiskLimits

    fields = set(RiskLimits.__dataclass_fields__)
    assert set(CONFIGURABLE_LIMITS) <= fields


# =========================================================================== the gate
class _Portfolio:
    equity = 1_000_000.0
    gross_exposure = 0.0

    def position(self, symbol):  # noqa: ARG002 - duck-typed
        class _P:
            quantity = 0.0
            last_price = 2500.0

        return _P()


def _instruments(symbol: str, exchange: str):
    from atr.core.models import Instrument

    return Instrument(symbol=symbol, exchange=exchange, multiplier=1.0)


def test_the_gate_refuses_everything_while_the_kill_switch_is_engaged(risk):
    """The fix for defect 4.2: the kill switch reaches the order path."""
    from atr.execution.oms import OrderDraft

    risk.set_kill_switch(True, reason="testing", actor="operator")
    gate = risk.gate(portfolio=_Portfolio(), instruments=_instruments)
    decision = gate(
        OrderDraft(user_id="u", symbol="RELIANCE", side="BUY", quantity=1, limit_price=2500.0)
    )
    assert decision.allowed is False
    assert "kill switch" in decision.reason


def test_the_gate_allows_an_order_when_nothing_is_wrong(risk):
    from atr.execution.oms import OrderDraft

    gate = risk.gate(portfolio=_Portfolio(), instruments=_instruments)
    decision = gate(
        OrderDraft(user_id="u", symbol="RELIANCE", side="BUY", quantity=1, limit_price=2500.0)
    )
    assert decision.allowed is True


def test_the_gate_applies_the_configured_limits(risk):
    from atr.execution.oms import OrderDraft

    risk.set_limits({"max_order_notional": 10_000}, actor="operator")
    gate = risk.gate(portfolio=_Portfolio(), instruments=_instruments)
    assert gate(
        OrderDraft(user_id="u", symbol="RELIANCE", side="BUY", quantity=1, limit_price=2500.0)
    ).allowed is True
    assert gate(
        OrderDraft(user_id="u", symbol="RELIANCE", side="BUY", quantity=100, limit_price=2500.0)
    ).allowed is False


def test_the_gate_respects_a_symbol_restriction(risk):
    from atr.execution.oms import OrderDraft

    risk.set_limits({"allowed_symbols": ["TCS"]}, actor="operator")
    gate = risk.gate(portfolio=_Portfolio(), instruments=_instruments)
    decision = gate(
        OrderDraft(user_id="u", symbol="INFY", side="BUY", quantity=1, limit_price=1500.0)
    )
    assert decision.allowed is False
    assert "allowed universe" in decision.reason
