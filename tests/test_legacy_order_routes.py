"""The legacy order routes, after they were rewired onto the OMS.

Two things changed and both are worth pinning, because both are the kind of
change that quietly reverts:

* **They require an account.** An order has an owner (``orders.user_id`` is a
  non-null foreign key), and an unattributed order is one nobody can be asked
  about. Read-only legacy routes stay open; the ones that can move money do not.
* **They no longer build a broker order themselves.** The signal-execute route
  used ``OrderType.SL_MARKET`` — a member that does not exist — on the *second*
  leg of a three-leg bracket, so the entry order had already been transmitted when
  the ``AttributeError`` fired: a live position with no stop-loss, and a 500 the
  operator could not interpret. Orders are now assembled in one place
  (``atr.services.execution``) and the venue is a parameter.

The source-level assertions at the bottom are deliberately blunt. A behavioural
test cannot catch a *new* call site that reintroduces the same bug; a grep can.
"""

from __future__ import annotations

from types import SimpleNamespace

_OPERATOR = SimpleNamespace(username="operator")
_ADITYA = SimpleNamespace(username="aditya")

import inspect

import pytest

from atr.api import main as api_main

BODY = {"symbol": "RELIANCE", "quantity": 10, "order_type": "MARKET"}


# =========================================================================== auth
def test_placing_a_manual_order_requires_an_account(client):
    """Previously an unauthenticated request could reach the exchange."""
    response = client.post("/orders", json=BODY)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authentication_required_for_orders"


def test_executing_a_signal_requires_an_account(client):
    response = client.post("/trade-signals/whatever/execute")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authentication_required_for_orders"


def test_read_only_legacy_routes_stay_open(client):
    """The dashboard must still render without signing in."""
    assert client.get("/health").status_code == 200
    assert client.get("/strategies").status_code == 200
    assert client.get("/risk/execution-mode").status_code == 200


def test_the_account_gate_comes_before_the_mode_gate(auth_client):
    """An authenticated caller in paper mode is refused by the *mode*, not by auth.

    Order matters for the message an operator sees: being told to sign in when you
    already have is a dead end.
    """
    response = auth_client.post("/orders", json=BODY)
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "paper" in str(detail).lower()
    assert "authentication" not in str(detail).lower()


def test_a_malformed_body_is_still_a_422(auth_client):
    response = auth_client.post("/orders", json={"symbol": "X"})
    assert response.status_code == 422


# =========================================================================== the bug
def _module_ast(module) -> object:
    """The parsed module, so assertions see code rather than prose.

    A plain text search cannot tell a call from a docstring that describes the call
    it replaced — which is exactly how these two assertions first failed. The AST
    has no such ambiguity, and it is the same approach
    ``tests/test_architecture.py`` uses for the layer rules.
    """
    import ast

    return ast.parse(inspect.getsource(module))


def _called_attributes(module) -> set[str]:
    import ast

    out: set[str] = set()
    for node in ast.walk(_module_ast(module)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            out.add(node.func.attr)
    return out


def _attribute_names(module) -> set[str]:
    import ast

    return {n.attr for n in ast.walk(_module_ast(module)) if isinstance(n, ast.Attribute)}


def test_the_signal_execute_route_no_longer_names_a_member_that_does_not_exist():
    """``OrderType.SL_MARKET`` never existed. This is the regression pin."""
    assert "SL_MARKET" not in _attribute_names(api_main)


def test_the_stop_loss_leg_resolves_to_a_real_enum_member():
    """The leg's spelling, through the same parse the venue uses."""
    from atr.core.enums import OrderType

    assert OrderType.parse("SL-M") is OrderType.STOP


def test_no_legacy_route_calls_the_broker_directly_any_more():
    """Every transmission goes through the execution service.

    A behavioural test cannot catch a *new* call site; this can. ``_live_broker()``
    is still allowed — it is how the service is constructed — but building an
    ``Order`` and calling ``place_order`` in this module is what the rewiring
    removed, and reintroducing it would put a route back in front of the broker
    with no risk gate and no event log.
    """
    assert "place_order" not in _called_attributes(api_main), (
        "api/main.py calls a broker's place_order directly; route it through "
        "atr.services.execution so the risk gate and the event log cannot be skipped"
    )


def test_the_manual_route_goes_through_the_execution_service():
    source = inspect.getsource(api_main.place_order)
    assert "ExecutionService" in source
    assert "OrderDraft" in source
    assert "get_order_service" in source


def test_the_signal_route_keys_each_leg_so_a_double_click_cannot_place_twice():
    """There was no guard at all before: two clicks placed two brackets."""
    source = inspect.getsource(api_main.trade_signals_execute)
    assert "idempotency_key_for" in source
    assert "leg_index" in source


def test_the_signal_route_puts_the_trigger_where_the_broker_reads_it():
    """``IiflBroker`` takes the trigger from ``order.stop_price``.

    The old leg set ``limit_price`` and a ``broker_params["triggerPrice"]`` that
    nothing reads, so the stop would have reached the exchange with no trigger.
    """
    import ast

    keywords = {
        kw.arg
        for node in ast.walk(_module_ast(api_main))
        if isinstance(node, ast.Call)
        for kw in node.keywords
    }
    assert "stop_price" in keywords
    assert "triggerPrice" not in keywords


def test_the_signal_route_reports_which_leg_failed(auth_client):
    """A half-placed bracket must say what is live and what is not."""
    source = inspect.getsource(api_main.trade_signals_execute)
    assert "venue_failed" in source
    assert '"placed": placed' in source


# =========================================================================== the switch
def test_the_legacy_kill_switch_reaches_the_durable_state(app_db, monkeypatch):
    """It used to write a module-level dict, so a restart re-armed trading."""
    from atr.services.risk import RiskStateService

    api_main.kill_switch(True, reason="legacy route test", principal=_OPERATOR)
    assert RiskStateService(app_db).kill_switch_engaged() is True

    api_main.kill_switch(False, reason="legacy route test done", principal=_OPERATOR)
    assert RiskStateService(app_db).kill_switch_engaged() is False


def test_the_legacy_kill_switch_requires_a_reason():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        api_main.kill_switch(True, reason="", principal=_OPERATOR)
    assert exc.value.detail["code"] == "reason_required"


def test_the_legacy_mode_endpoint_writes_the_durable_state(app_db):
    from atr.services.risk import RiskStateService

    api_main.set_execution_mode("live", reason="legacy route test", principal=_OPERATOR)
    assert RiskStateService(app_db).execution_mode() == "live"
    api_main.set_execution_mode("paper", reason="", principal=_OPERATOR)


def test_an_unreadable_risk_store_reports_paper_not_live(monkeypatch):
    """The failure direction matters: an unreadable mode must never read as 'live'."""
    from atr.services import risk as risk_module

    def explode(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(risk_module.RiskStateService, "snapshot", explode)
    state = api_main._risk_state()
    assert state["mode"] == "paper"
    assert state["live"] is False


# ================================================================= legacy write auth
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/risk/kill-switch?engaged=true&reason=x"),
        ("post", "/risk/execution-mode?mode=live&reason=x"),
        ("put", "/trade-signals/settings"),
        ("post", "/briefing/send"),
        ("post", "/alerts/rules"),
        ("post", "/backtest"),
    ],
)
def test_legacy_write_routes_refuse_an_anonymous_caller(client, method, path):
    """With auth required, an anonymous request must not reach a write handler.

    The kill switch and execution mode used to answer anyone, and took the audit
    ``actor`` from the query string, so an anonymous caller could release the
    switch and sign the trail as someone else.
    """
    response = getattr(client, method)(path, json={})
    assert response.status_code == 401, response.text


def test_the_kill_switch_actor_is_the_authenticated_user_not_a_query_param(auth_client):
    response = auth_client.post(
        "/risk/kill-switch",
        params={"engaged": True, "reason": "halt", "actor": "someone-else"},
    )
    assert response.status_code == 200, response.text
    entries = auth_client.get("/audit").json()["entries"]
    assert entries[0]["actor"] == "owner"


@pytest.mark.skipif(
    not (api_main._WEB_DIST / "index.html").exists(), reason="frontend not built"
)
@pytest.mark.parametrize(
    "path", ["/%2e%2e/package.json", "/%2e%2e/%2e%2e/pyproject.toml", "/..%2f..%2fpyproject.toml"]
)
def test_the_spa_fallback_cannot_read_files_outside_the_build(client, path):
    """`%2e%2e` decodes to `..`; an unchecked join served .env and the broker token."""
    response = client.get(path)
    assert "[project]" not in response.text
    assert '"name": "atr-web"' not in response.text


def test_audit_limit_zero_is_not_the_whole_file(tmp_path, monkeypatch):
    """`lines[-0:]` is every line; a zero limit must not dump the full trail."""
    import json

    path = tmp_path / "audit.jsonl"
    path.write_text("\n".join(json.dumps({"n": i}) for i in range(50)) + "\n", encoding="utf-8")
    monkeypatch.setattr(api_main, "_AUDIT_PATH", path)
    assert len(api_main._read_audit(0)) == 1
    assert [e["n"] for e in api_main._read_audit(3)] == [49, 48, 47]


def test_query_limits_are_bounded(auth_client):
    assert auth_client.get("/audit?limit=0").status_code == 422
    assert auth_client.get("/audit?limit=999999").status_code == 422
    assert auth_client.get("/ticks/history?symbol=X&limit=10000000").status_code == 422


def test_the_tick_websocket_refuses_a_foreign_origin(client):
    """A page on another site must not be able to open the tick stream."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/ticks", headers={"origin": "https://evil.example"}):
            pass
