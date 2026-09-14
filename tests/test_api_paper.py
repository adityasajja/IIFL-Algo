"""API tests for ``/api/v1/paper``.

These exercise the wiring the service tests cannot: that the permission dependency
is attached, that another account's deployment is a 404 rather than a 403, that the
reason-required rule survives the HTTP layer as a 400 with a code, and that the
honesty fields (``complete`` / ``unpriced_symbols``) reach the client.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atr.api.main import app

LOOPBACK = ("127.0.0.1", 51234)


@pytest.fixture()
def prices(monkeypatch):
    """A deterministic price source for paper fills.

    The venue's default reads the operator's real daily cache, which a test must
    not depend on: "it passed because the cache had data today" is not a test. The
    seam exists for exactly this substitution.
    """
    from atr.services import paper as paper_service
    from atr.services.paper import fixed_price_source

    table = {"RELIANCE": 2500.0, "TCS": 3000.0, "INFY": 1500.0}
    monkeypatch.setattr(
        paper_service, "default_price_source",
        lambda: fixed_price_source(table),
    )
    return table


@pytest.fixture()
def owner(fresh_env, master, prices) -> TestClient:
    client = TestClient(app, client=LOOPBACK)
    response = client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "owner@example.com", "username": "owner",
            "password": "Str0ngPassw0rd", "display_name": "Owner",
        },
    )
    assert response.status_code == 201, response.text
    client.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    return client


@pytest.fixture()
def viewer(fresh_env, master, owner) -> TestClient:
    """Read-only, for the authorization assertions."""
    from atr.appdb.engine import get_app_db
    from atr.appdb.repositories import UserRepository
    from atr.auth.passwords import hash_password

    with get_app_db().session() as session:
        UserRepository.create(
            session, email="viewer@example.com", username="viewer",
            password_hash=hash_password("Str0ngPassw0rd"), role="viewer",
        )
    client = TestClient(app, client=LOOPBACK)
    response = client.post(
        "/api/v1/auth/login",
        json={"identifier": "viewer", "password": "Str0ngPassw0rd"},
    )
    assert response.status_code == 200, response.text
    client.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    return client


def _deploy(client: TestClient, **overrides) -> dict:
    body = {"strategy_id": "s1", "strategy_version": 1, "capital": 250_000.0}
    body.update(overrides)
    response = client.post("/api/v1/paper/deployments", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# =========================================================================== wiring
def test_the_paper_surface_requires_authentication(client):
    assert client.get("/api/v1/paper/deployments").status_code == 401
    assert client.get("/api/v1/paper/account").status_code == 401


def test_a_viewer_can_read_but_not_deploy(viewer):
    assert viewer.get("/api/v1/paper/deployments").status_code == 200
    denied = viewer.post(
        "/api/v1/paper/deployments",
        json={"strategy_id": "s1", "strategy_version": 1, "capital": 1000.0},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"


def test_a_deployment_is_created_pending(owner):
    created = _deploy(owner)
    assert created["status"] == "PENDING"
    assert created["mode"] == "PAPER"
    assert created["capital"] == 250_000.0
    assert created["strategy_version"] == 1


def test_an_invalid_deployment_is_refused(owner):
    assert owner.post(
        "/api/v1/paper/deployments",
        json={"strategy_id": "s1", "strategy_version": 0, "capital": 1000.0},
    ).status_code == 422
    assert owner.post(
        "/api/v1/paper/deployments",
        json={"strategy_id": "s1", "strategy_version": 1, "capital": -5},
    ).status_code == 422


def test_an_unknown_field_is_refused_rather_than_ignored(owner):
    """Silently dropping a field the caller set is worse than a 422."""
    response = owner.post(
        "/api/v1/paper/deployments",
        json={"strategy_id": "s1", "strategy_version": 1, "capital": 1000.0,
              "leverage": 10},
    )
    assert response.status_code == 422


def test_another_accounts_deployment_is_a_404_not_a_403(owner, viewer):
    created = _deploy(owner)
    response = viewer.get(f"/api/v1/paper/deployments/{created['deployment_id']}")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
    assert viewer.get(
        f"/api/v1/paper/deployments/{created['deployment_id']}/pnl"
    ).status_code == 404


def test_the_list_only_shows_your_own(owner, viewer):
    _deploy(owner)
    assert owner.get("/api/v1/paper/deployments").json()["total"] == 1
    assert viewer.get("/api/v1/paper/deployments").json()["total"] == 0


# =========================================================================== lifecycle
def test_start_pause_resume_stop(owner):
    deployment_id = _deploy(owner)["deployment_id"]

    assert owner.post(
        f"/api/v1/paper/deployments/{deployment_id}/start"
    ).json()["status"] == "RUNNING"

    paused = owner.post(
        f"/api/v1/paper/deployments/{deployment_id}/pause",
        json={"reason": "waiting for the opening range"},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "PAUSED"
    assert paused.json()["stop_reason"] == "waiting for the opening range"

    assert owner.post(
        f"/api/v1/paper/deployments/{deployment_id}/start"
    ).json()["status"] == "RUNNING"

    stopped = owner.post(
        f"/api/v1/paper/deployments/{deployment_id}/stop",
        json={"reason": "session over"},
    )
    assert stopped.json()["status"] == "STOPPED"


def test_stop_and_pause_require_a_reason(owner):
    deployment_id = _deploy(owner)["deployment_id"]
    owner.post(f"/api/v1/paper/deployments/{deployment_id}/start")

    for action in ("pause", "stop"):
        response = owner.post(
            f"/api/v1/paper/deployments/{deployment_id}/{action}", json={"reason": ""}
        )
        assert response.status_code == 422, action  # pydantic min_length

    # And a whitespace-only reason is refused by the service, as a 400 with a code.
    response = owner.post(
        f"/api/v1/paper/deployments/{deployment_id}/pause", json={"reason": "   "}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "reason_required"


def test_a_stopped_deployment_cannot_be_restarted(owner):
    deployment_id = _deploy(owner)["deployment_id"]
    owner.post(f"/api/v1/paper/deployments/{deployment_id}/start")
    owner.post(f"/api/v1/paper/deployments/{deployment_id}/stop", json={"reason": "done"})

    response = owner.post(f"/api/v1/paper/deployments/{deployment_id}/start")
    assert response.status_code == 404  # the filter is "startable", so nothing matched


def test_pausing_a_deployment_that_is_not_running_is_a_409(owner):
    deployment_id = _deploy(owner)["deployment_id"]
    response = owner.post(
        f"/api/v1/paper/deployments/{deployment_id}/pause", json={"reason": "why not"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_running"


def test_reset_returns_a_new_deployment_and_names_the_old_one(owner):
    original = _deploy(owner)["deployment_id"]
    owner.post(f"/api/v1/paper/deployments/{original}/start")

    fresh = owner.post(
        f"/api/v1/paper/deployments/{original}/reset",
        json={"reason": "parameters retuned"},
    )
    assert fresh.status_code == 200
    body = fresh.json()
    assert body["deployment_id"] != original
    assert body["reset_from"] == original
    assert body["capital"] == 250_000.0

    assert owner.get(f"/api/v1/paper/deployments/{original}").json()["status"] == "STOPPED"


def test_reset_requires_a_reason(owner):
    deployment_id = _deploy(owner)["deployment_id"]
    response = owner.post(
        f"/api/v1/paper/deployments/{deployment_id}/reset", json={"reason": ""}
    )
    assert response.status_code == 422


# =========================================================================== reads
def test_a_fresh_deployment_has_its_capital_and_no_positions(owner):
    deployment_id = _deploy(owner)["deployment_id"]
    body = owner.get(f"/api/v1/paper/deployments/{deployment_id}/pnl").json()

    assert body["initial_cash"] == 250_000.0
    assert body["cash"] == 250_000.0
    assert body["equity"] == 250_000.0
    assert body["positions"] == []
    assert body["realized_pnl"] == 0.0
    assert body["complete"] is True


def test_positions_and_orders_are_scoped_to_the_deployment(owner):
    first = _deploy(owner)["deployment_id"]
    second = _deploy(owner, strategy_id="s2")["deployment_id"]

    # Place a paper order through the OMS so the ledger has something to fold.
    created = owner.post(
        f"/api/v1/paper/deployments/{first}/orders",
        json={"symbol": "RELIANCE", "side": "BUY", "quantity": 10,
              "requested_price": 2500.0, "limit_price": 2500.0},
    )
    assert created.status_code == 201, created.text

    assert owner.get(
        f"/api/v1/paper/deployments/{first}/orders"
    ).json()["total"] == 1
    assert owner.get(
        f"/api/v1/paper/deployments/{second}/orders"
    ).json()["total"] == 0


def test_the_account_view_works_without_a_deployment(owner):
    """``initial_cash`` is zero rather than an invented default."""
    body = owner.get("/api/v1/paper/account").json()
    assert body["initial_cash"] == 0.0
    assert body["equity"] == 0.0
    assert body["positions"] == []


def test_marks_can_be_supplied_explicitly(owner):
    """So the panel can value a position without the local cache."""
    deployment_id = _deploy(owner)["deployment_id"]
    owner.post(
        f"/api/v1/paper/deployments/{deployment_id}/orders",
        json={"symbol": "RELIANCE", "side": "BUY", "quantity": 10,
              "requested_price": 2500.0, "limit_price": 2500.0},
    )
    response = owner.post(
        f"/api/v1/paper/deployments/{deployment_id}/positions",
        json={"prices": {"RELIANCE": 2550.0}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["complete"] is True
    assert body["positions"][0]["priced"] is True
    assert body["positions"][0]["last_price"] == 2550.0


def test_unpriced_positions_are_named_not_valued_at_zero(owner):
    """The honesty contract, at the HTTP layer."""
    deployment_id = _deploy(owner)["deployment_id"]
    owner.post(
        f"/api/v1/paper/deployments/{deployment_id}/orders",
        json={"symbol": "RELIANCE", "side": "BUY", "quantity": 10,
              "requested_price": 2500.0, "limit_price": 2500.0},
    )
    # `marks=false` skips the cache, so nothing can be priced.
    body = owner.get(
        f"/api/v1/paper/deployments/{deployment_id}/pnl", params={"marks": "false"}
    ).json()

    assert body["complete"] is False
    assert body["unpriced_symbols"] == ["RELIANCE"]
    row = body["positions"][0]
    assert row["quantity"] == 10.0
    assert row["priced"] is False
    assert row["unrealized_pnl"] is None
