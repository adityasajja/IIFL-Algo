"""API tests for ``/api/v1/reconciliation``.

The wiring assertions: permissions are attached, another account's runs are a 404,
an unknown scope is a 400 with the valid list, and — the important one — a run that
cannot reach the broker comes back as a *recorded run that checked nothing* rather
than as an error. A control that returns 500 when it cannot check is a control that
stops running.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atr.api.main import app

LOOPBACK = ("127.0.0.1", 51234)


@pytest.fixture()
def owner(fresh_env, master) -> TestClient:
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


# =========================================================================== wiring
def test_the_surface_requires_authentication(client):
    assert client.get("/api/v1/reconciliation/status").status_code == 401
    assert client.post("/api/v1/reconciliation/run", json={}).status_code == 401


def test_a_viewer_can_read_but_not_run(viewer):
    """Running writes a run row and an audit row, so it is not a read."""
    assert viewer.get("/api/v1/reconciliation/status").status_code == 200
    denied = viewer.post("/api/v1/reconciliation/run", json={})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"


def test_the_scopes_are_served(owner):
    body = owner.get("/api/v1/reconciliation/scopes").json()
    assert set(body["scopes"]) == {"orders", "positions", "holdings", "funds"}


def test_a_fresh_install_reports_clear(owner):
    assert owner.get("/api/v1/reconciliation/status").json() == {"clear": True, "run": None}


def test_no_run_yet_is_a_404(owner):
    response = owner.get("/api/v1/reconciliation/runs/latest")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


# =========================================================================== running
def test_a_run_without_a_broker_session_is_recorded_not_an_error(owner):
    """The control has to keep running. A 500 here would be a control that stops.

    There is no IIFL session in a test, so the run cannot check anything — and the
    response says exactly that, with a severity that is not ``ok``.
    """
    response = owner.post("/api/v1/reconciliation/run", json={})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["run_id"]
    assert body["severity"] != "ok"
    assert body["error"]
    assert body["mismatches"] >= 1, "an unchecked run must not report zero problems"
    assert body["differences"] == 0, "nothing disagreed — nothing could be compared"
    assert body["differences"] == 0, "nothing disagreed — nothing could be compared"
    # Every scope is named as not compared, with a reason.
    assert {e["scope"] for e in body["incomparable"]} == {
        "orders", "positions", "holdings", "funds",
    }


def test_an_unchecked_run_raises_the_banner(owner):
    owner.post("/api/v1/reconciliation/run", json={})
    status = owner.get("/api/v1/reconciliation/status").json()
    assert status["clear"] is False
    assert status["run"]["severity"] != "ok"


def test_a_run_can_be_scoped(owner):
    body = owner.post(
        "/api/v1/reconciliation/run", json={"scopes": ["orders"], "notify": False}
    ).json()
    assert body["scope"] == "orders"
    assert [r["scope"] for r in body["results"]] == ["orders"]


def test_an_unknown_scope_is_a_400_with_the_valid_list(owner):
    response = owner.post(
        "/api/v1/reconciliation/run", json={"scopes": ["orders", "vibes"]}
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "unknown_scope"
    assert set(detail["valid"]) == {"orders", "positions", "holdings", "funds"}


def test_an_unknown_field_is_refused(owner):
    response = owner.post(
        "/api/v1/reconciliation/run", json={"scope": "orders"}
    )
    assert response.status_code == 422


# =========================================================================== history
def test_runs_are_listed_and_persisted(owner):
    owner.post("/api/v1/reconciliation/run", json={})
    owner.post("/api/v1/reconciliation/run", json={"scopes": ["orders"]})

    body = owner.get("/api/v1/reconciliation/runs").json()
    assert body["total"] == 2
    assert all(r["run_id"] for r in body["runs"])


def test_runs_can_be_filtered_by_severity(owner):
    owner.post("/api/v1/reconciliation/run", json={})
    body = owner.get(
        "/api/v1/reconciliation/runs", params={"severity": "critical"}
    ).json()
    assert body["total"] == 0  # an unreachable broker is a warning, not a critical

    body = owner.get("/api/v1/reconciliation/runs", params={"severity": "warning"}).json()
    assert body["total"] == 1


def test_an_invalid_severity_filter_is_refused(owner):
    response = owner.get("/api/v1/reconciliation/runs", params={"severity": "bad"})
    assert response.status_code == 422


def test_the_latest_run_is_returned_with_its_detail(owner):
    owner.post("/api/v1/reconciliation/run", json={"scopes": ["orders"]})
    body = owner.get("/api/v1/reconciliation/runs/latest").json()
    assert body["scope"] == "orders"
    assert body["detail"]


def test_runs_are_user_scoped(owner, viewer):
    owner.post("/api/v1/reconciliation/run", json={})
    assert viewer.get("/api/v1/reconciliation/runs").json()["total"] == 0
    assert viewer.get("/api/v1/reconciliation/status").json() == {
        "clear": True, "run": None,
    }


# =========================================================================== audit
def test_the_run_lands_in_the_audit_trail(owner):
    owner.post("/api/v1/reconciliation/run", json={"scopes": ["orders"]})
    events = owner.get(
        "/api/v1/audit/events", params={"action": "reconciliation.run"}
    ).json()
    assert events["total"] >= 1
    assert events["events"][0]["result"] == "failure", (
        "a run that could not check anything is not a success"
    )
