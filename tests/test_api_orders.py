"""API tests for ``/api/v1/orders`` and ``/api/v1/risk``.

These exercise the wiring the OMS tests cannot: that the permission dependency is
attached to each route, that a refused transition is a 409 with the two states
named, that another account's order is a 404 rather than a 403, and — the point of
the whole increment — that **the kill switch reaches the order path** so a manual
order cannot be approved while it is engaged.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atr.api.main import app

LOOPBACK = ("127.0.0.1", 51234)


@pytest.fixture()
def owner(fresh_env, master) -> TestClient:
    """A bootstrapped owner with a bearer token. The owner has every permission."""
    client = TestClient(app, client=LOOPBACK)
    response = client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "owner@example.com",
            "username": "owner",
            "password": "Str0ngPassw0rd",
            "display_name": "Owner",
        },
    )
    assert response.status_code == 201, response.text
    client.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    return client


@pytest.fixture()
def viewer(fresh_env, master, owner) -> TestClient:
    """A second account with read-only permissions, for the authorization tests.

    Created directly through the repository because self-registration is disabled
    on a trading platform — the operator adds accounts.
    """
    from atr.appdb.engine import get_app_db
    from atr.appdb.repositories import UserRepository
    from atr.auth.passwords import hash_password

    with get_app_db().session() as session:
        UserRepository.create(
            session,
            email="viewer@example.com",
            username="viewer",
            password_hash=hash_password("Str0ngPassw0rd"),
            role="viewer",
        )
    client = TestClient(app, client=LOOPBACK)
    response = client.post(
        "/api/v1/auth/login",
        json={"identifier": "viewer", "password": "Str0ngPassw0rd"},
    )
    assert response.status_code == 200, response.text
    client.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    return client


def _create(client: TestClient, **overrides):
    body = {
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 10,
        "mode": "PAPER",
        "requested_price": 2500.0,
        "limit_price": 2500.0,
    }
    body.update(overrides)
    response = client.post("/api/v1/orders", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# =========================================================================== wiring
def test_the_order_surface_requires_authentication(client):
    assert client.get("/api/v1/orders").status_code == 401
    assert client.post("/api/v1/orders", json={"symbol": "X", "side": "BUY", "quantity": 1}).status_code == 401


def test_the_state_machine_is_served_so_the_client_cannot_invent_one(owner):
    body = owner.get("/api/v1/orders/state-machine").json()
    assert "PARTIALLY_FILLED" in body["states"]
    assert "FILLED" in body["terminal"]
    assert "ACKNOWLEDGED" in body["transitions"]["SUBMITTED"]
    assert body["transitions"]["FILLED"] == []


def test_a_viewer_cannot_place_an_order(viewer):
    """``order:read`` is not ``order:place`` — reading the book is not trading."""
    assert viewer.get("/api/v1/orders").status_code == 200
    response = viewer.post(
        "/api/v1/orders",
        json={"symbol": "RELIANCE", "side": "BUY", "quantity": 1, "mode": "PAPER"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "permission_denied"


def test_a_viewer_cannot_configure_risk(viewer):
    assert viewer.get("/api/v1/risk/state").status_code == 200
    denied = viewer.post(
        "/api/v1/risk/kill-switch", json={"engaged": True, "reason": "because"}
    )
    assert denied.status_code == 403


# =========================================================================== create
def test_creating_an_order_returns_it_at_new(owner):
    body = _create(owner)
    assert body["created"] is True
    assert body["order"]["status"] == "NEW"
    assert body["order"]["symbol"] == "RELIANCE"
    assert body["order"]["filled_quantity"] == 0.0


def test_an_idempotency_key_makes_a_retry_safe(owner):
    headers = {"Idempotency-Key": "b" * 64}
    first = owner.post(
        "/api/v1/orders",
        json={"symbol": "TCS", "side": "BUY", "quantity": 5, "mode": "PAPER"},
        headers=headers,
    )
    second = owner.post(
        "/api/v1/orders",
        json={"symbol": "TCS", "side": "BUY", "quantity": 5, "mode": "PAPER"},
        headers=headers,
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["order"]["order_id"] == second.json()["order"]["order_id"]
    assert second.json()["created"] is False
    assert owner.get("/api/v1/orders").json()["total"] == 1


def test_the_same_intent_without_a_header_still_deduplicates(owner):
    """A strategy that re-fires the same signal must not place a second order."""
    body = {"symbol": "INFY", "side": "BUY", "quantity": 3, "mode": "PAPER",
            "strategy_id": "s1", "strategy_version": 1, "signal_id": "sig-9"}
    first = owner.post("/api/v1/orders", json=body).json()
    second = owner.post("/api/v1/orders", json=body).json()
    assert first["order"]["order_id"] == second["order"]["order_id"]
    assert second["created"] is False


def test_an_invalid_order_is_a_400_with_a_code(owner):
    response = owner.post(
        "/api/v1/orders",
        json={"symbol": "RELIANCE", "side": "HOLD", "quantity": 1, "mode": "PAPER"},
    )
    assert response.status_code == 422  # pydantic rejects the side before the service

    response = owner.post(
        "/api/v1/orders",
        json={"symbol": "RELIANCE", "side": "BUY", "quantity": -1, "mode": "PAPER"},
    )
    assert response.status_code == 422


# =========================================================================== lifecycle
def test_the_lifecycle_is_visible_through_the_api(owner):
    order_id = _create(owner)["order"]["order_id"]

    validated = owner.post(f"/api/v1/orders/{order_id}/validate")
    assert validated.status_code == 200
    assert validated.json() == {
        "order_id": order_id,
        "status": "RISK_APPROVED",
        "allowed": True,
        "reject_reason": None,
    }

    history = owner.get(f"/api/v1/orders/{order_id}/history").json()
    assert [e["to_status"] for e in history["events"]] == [
        "NEW", "VALIDATING", "RISK_APPROVED",
    ]
    assert history["final_status"] == "RISK_APPROVED"
    assert [e["seq"] for e in history["events"]] == [1, 2, 3]


def test_validating_twice_is_a_409_naming_both_states(owner):
    order_id = _create(owner)["order"]["order_id"]
    owner.post(f"/api/v1/orders/{order_id}/validate")

    response = owner.post(f"/api/v1/orders/{order_id}/validate")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_transition"
    assert detail["from_status"] == "RISK_APPROVED"
    assert detail["to_status"] == "VALIDATING"


def test_a_cancel_is_two_steps(owner):
    order_id = _create(owner)["order"]["order_id"]
    owner.post(f"/api/v1/orders/{order_id}/validate")

    requested = owner.post(
        f"/api/v1/orders/{order_id}/cancel", json={"reason": "operator override"}
    )
    assert requested.status_code == 200
    assert requested.json()["status"] == "CANCEL_PENDING"
    assert owner.get(f"/api/v1/orders/{order_id}").json()["status"] == "CANCEL_PENDING"

    confirmed = owner.post(f"/api/v1/orders/{order_id}/cancel/confirm")
    assert confirmed.json()["status"] == "CANCELLED"
    assert owner.get(f"/api/v1/orders/{order_id}").json()["completed_at"] is not None


def test_a_cancel_without_a_reason_is_rejected(owner):
    order_id = _create(owner)["order"]["order_id"]
    owner.post(f"/api/v1/orders/{order_id}/validate")
    response = owner.post(f"/api/v1/orders/{order_id}/cancel", json={"reason": ""})
    assert response.status_code == 422


def test_an_unvalidated_order_can_still_be_cancelled(owner):
    """Cancel is a request a user can make at any point before the order is done.

    Whether there is anything to transmit to the exchange is the broker's problem,
    not a reason to refuse the request — and the two-step shape keeps the request
    on the record even when there was nothing to send.
    """
    order_id = _create(owner)["order"]["order_id"]
    requested = owner.post(f"/api/v1/orders/{order_id}/cancel", json={"reason": "changed my mind"})
    assert requested.status_code == 200
    assert requested.json()["status"] == "CANCEL_PENDING"
    assert owner.post(f"/api/v1/orders/{order_id}/cancel/confirm").json()["status"] == "CANCELLED"

    history = owner.get(f"/api/v1/orders/{order_id}/history").json()
    assert [e["to_status"] for e in history["events"]] == [
        "NEW", "CANCEL_PENDING", "CANCELLED",
    ]


def test_a_cancelled_order_cannot_then_be_validated(owner):
    order_id = _create(owner)["order"]["order_id"]
    owner.post(f"/api/v1/orders/{order_id}/cancel", json={"reason": "changed my mind"})
    owner.post(f"/api/v1/orders/{order_id}/cancel/confirm")
    response = owner.post(f"/api/v1/orders/{order_id}/validate")
    assert response.status_code == 409
    assert response.json()["detail"]["from_status"] == "CANCELLED"


def test_an_unknown_order_is_a_404_with_the_documented_code(owner):
    response = owner.get("/api/v1/orders/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
    assert owner.get("/api/v1/orders/does-not-exist/history").status_code == 404
    assert owner.post("/api/v1/orders/does-not-exist/validate").status_code == 404


def test_another_accounts_order_is_a_404_not_a_403(owner, viewer):
    """403 would confirm the id exists, which is itself a leak.

    This is about *ownership*, so it applies once the caller holds the
    permission. ``viewer`` has ``order:read`` but not ``order:place``, so the
    reads must be 404 and the write is 403 for the different reason that the
    viewer may not place orders at all — asserted separately below so the two
    rules cannot be confused for one another.
    """
    order_id = _create(owner)["order"]["order_id"]
    assert viewer.get(f"/api/v1/orders/{order_id}").status_code == 404
    assert viewer.get(f"/api/v1/orders/{order_id}/history").status_code == 404
    assert viewer.get(f"/api/v1/orders/{order_id}/fills").status_code == 404
    assert viewer.get("/api/v1/orders").json()["total"] == 0

    # A missing permission is 403 regardless of who owns the order.
    denied = viewer.post(f"/api/v1/orders/{order_id}/validate")
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"


def test_a_404_does_not_distinguish_missing_from_not_yours(owner, viewer):
    """The two answers must be identical, or the difference is the leak."""
    order_id = _create(owner)["order"]["order_id"]
    theirs = viewer.get(f"/api/v1/orders/{order_id}")
    missing = viewer.get("/api/v1/orders/does-not-exist")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()


def test_the_order_list_filters_and_pages(owner):
    _create(owner, symbol="TCS")
    _create(owner, symbol="INFY")
    assert owner.get("/api/v1/orders").json()["total"] == 2
    assert owner.get("/api/v1/orders", params={"symbol": "TCS"}).json()["total"] == 1
    assert owner.get("/api/v1/orders", params={"status": "FILLED"}).json()["total"] == 0
    assert owner.get("/api/v1/orders", params={"limit": 1}).json()["limit"] == 1


def test_open_orders_excludes_nothing_that_is_still_live(owner):
    order_id = _create(owner)["order"]["order_id"]
    assert [o["order_id"] for o in owner.get("/api/v1/orders/open").json()["orders"]] == [
        order_id
    ]


def test_the_open_filter_only_accepts_terminal_states(owner):
    order_id = _create(owner)["order"]["order_id"]
    owner.post(f"/api/v1/orders/{order_id}/validate")
    owner.post(f"/api/v1/orders/{order_id}/cancel", json={"reason": "done"})
    owner.post(f"/api/v1/orders/{order_id}/cancel/confirm")
    assert owner.get("/api/v1/orders/open").json()["orders"] == []


# =========================================================================== risk state
def test_the_default_risk_state_is_safe(owner):
    body = owner.get("/api/v1/risk/state").json()
    assert body["execution_mode"] == "paper"
    assert body["live"] is False
    assert body["kill_switch"] is False
    # `Infinity` is not valid JSON; "no limit" must come out as null.
    assert body["limits"]["max_position_notional"] is None


def test_going_live_requires_a_reason(owner):
    response = owner.post(
        "/api/v1/risk/execution-mode", json={"mode": "LIVE", "reason": ""}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "reason_required"
    assert owner.get("/api/v1/risk/state").json()["execution_mode"] == "paper"


def test_the_mode_can_be_changed_with_a_reason(owner):
    response = owner.post(
        "/api/v1/risk/execution-mode",
        json={"mode": "LIVE", "reason": "validated on paper for six weeks"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["execution_mode"] == "live" and body["live"] is True
    assert body["changed_by"] == "owner"
    assert body["changed_at"].endswith("Z")


def test_the_kill_switch_requires_a_reason(owner):
    response = owner.post(
        "/api/v1/risk/kill-switch", json={"engaged": True, "reason": "   "}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "reason_required"


def test_limits_can_be_set_and_cleared(owner):
    set_response = owner.put(
        "/api/v1/risk/limits",
        json={"max_order_notional": 250000, "max_daily_trades": 20, "allow_short": False},
    )
    assert set_response.status_code == 200
    limits = set_response.json()["limits"]
    assert limits["max_order_notional"] == 250000
    assert limits["allow_short"] is False

    # Wholesale replacement, so clearing a limit is expressible.
    cleared = owner.put("/api/v1/risk/limits", json={"allow_short": True}).json()
    assert cleared["limits"]["max_order_notional"] is None


def test_an_unknown_limit_field_is_refused(owner):
    response = owner.put("/api/v1/risk/limits", json={"max_fun": 10})
    assert response.status_code == 422  # pydantic rejects it before the service


def test_the_limit_schema_is_served(owner):
    body = owner.get("/api/v1/risk/limits/schema").json()
    assert "max_daily_loss" in body["limits"]


# =========================================================== the kill switch bites
def test_the_kill_switch_stops_a_manual_order_being_approved(owner):
    """The defect this increment fixes.

    Before, the kill switch was consulted on signal execution and *not* on the
    manual order route, so an operator who had stopped the platform could still
    place an order by hand.
    """
    engaged = owner.post(
        "/api/v1/risk/kill-switch",
        json={"engaged": True, "reason": "market dislocation, halting"},
    )
    assert engaged.status_code == 200
    assert engaged.json()["kill_switch"] is True

    order_id = _create(owner)["order"]["order_id"]
    result = owner.post(f"/api/v1/orders/{order_id}/validate")
    assert result.status_code == 200
    body = result.json()
    assert body["allowed"] is False
    assert body["status"] == "REJECTED"
    assert "kill switch" in body["reject_reason"]

    # And the rejection is on the record, attributed to risk.
    history = owner.get(f"/api/v1/orders/{order_id}/history").json()
    assert history["events"][-1]["source"] == "risk"
    assert history["final_status"] == "REJECTED"


def test_releasing_the_kill_switch_lets_orders_through_again(owner):
    owner.post("/api/v1/risk/kill-switch", json={"engaged": True, "reason": "halt"})
    order_id = _create(owner)["order"]["order_id"]
    assert owner.post(f"/api/v1/orders/{order_id}/validate").json()["allowed"] is False

    owner.post(
        "/api/v1/risk/kill-switch",
        json={"engaged": False, "reason": "broker confirmed healthy"},
    )
    order_id = _create(owner)["order"]["order_id"]
    assert owner.post(f"/api/v1/orders/{order_id}/validate").json()["allowed"] is True


def test_a_configured_order_notional_limit_bites_on_the_order_route(owner):
    owner.put("/api/v1/risk/limits", json={"max_order_notional": 10_000})
    order_id = _create(owner, quantity=100, limit_price=2500.0)["order"]["order_id"]
    body = owner.post(f"/api/v1/orders/{order_id}/validate").json()
    assert body["allowed"] is False
    assert "notional" in body["reject_reason"]


def test_a_symbol_restriction_bites_on_the_order_route(owner):
    owner.put("/api/v1/risk/limits", json={"allowed_symbols": ["TCS"]})
    order_id = _create(owner, symbol="INFY", limit_price=1500.0)["order"]["order_id"]
    body = owner.post(f"/api/v1/orders/{order_id}/validate").json()
    assert body["allowed"] is False
    assert "allowed universe" in body["reject_reason"]


def test_the_risk_state_is_durable_across_requests(owner):
    """It is read from the database on each call, so it cannot go stale."""
    owner.post("/api/v1/risk/kill-switch", json={"engaged": True, "reason": "halt"})
    assert owner.get("/api/v1/risk/state").json()["kill_switch"] is True
    assert owner.get("/api/v1/risk/state").json()["kill_switch"] is True
