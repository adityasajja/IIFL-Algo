"""The monitoring API surface, over HTTP.

The service tests in ``test_paper_monitoring.py`` prove the payload is right.
These prove it is *reachable* — that the routes exist, that they are
owner-scoped, and that what they return survives JSON encoding.

That last one is not a formality. The risk limits are a dataclass holding an
``inf`` sentinel for "unbounded"; a route that returned it naively would either
raise on encode or ship ``Infinity``, which is not valid JSON and which a browser
will not parse. The test asserts the shape a client can actually consume.
"""

from __future__ import annotations


def _bootstrap_second_user(client) -> str:
    """A second account, to prove owner scoping from outside."""
    from atr.appdb.engine import get_app_db
    from atr.appdb.repositories import UserRepository
    from atr.auth.passwords import hash_password

    db = get_app_db()
    with db.session() as session:
        UserRepository.create(
            session,
            email="other@example.com",
            username="other",
            password_hash=hash_password("Str0ngPassw0rd"),
            role="owner",
        )
    # A fresh login for the second account.
    response = client.post(
        "/api/v1/auth/login",
        json={"identifier": "other@example.com", "password": "Str0ngPassw0rd"},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _deployment(auth_client) -> str:
    """A real strategy version and a deployment onto it.

    Both halves now go through the authoring routes. This helper used to create a
    strategy and then deploy *version 1* without ever creating one — which worked
    only because ``POST /paper/deployments`` did not check the pinned version. A
    deployment onto a version that does not exist reports itself ``RUNNING`` and
    places no orders, so the route refuses it now (``422 version_not_found``) and
    this fixture has to build the pair it claims to be deploying.
    """
    strategy = auth_client.post(
        "/api/v1/strategies",
        json={"name": "Monitor API", "kind": "rules"},
    )
    assert strategy.status_code == 201, strategy.text
    strategy_id = strategy.json()["strategy_id"]

    version = auth_client.post(
        f"/api/v1/strategies/{strategy_id}/versions",
        json={
            "definition": {
                "rules": {
                    "entry": {"breakout_lookback": 63, "min_history_bars": 70},
                    "exit": {"stop_loss_pct": 5.0, "min_history_bars": 70},
                }
            }
        },
    )
    assert version.status_code == 201, version.text
    version_number = version.json()["version"]

    created = auth_client.post(
        "/api/v1/paper/deployments",
        json={
            "strategy_id": strategy_id,
            "strategy_version": version_number,
            "capital": 500_000.0,
            "mode": "PAPER",
            "config": {"symbols": ["RELIANCE"], "exchange": "NSEEQ"},
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["deployment_id"]


def test_every_monitoring_route_exists(auth_client):
    """The screen's six views plus the overview must all be routed.

    Read off the live OpenAPI document rather than ``app.routes``: the versioned
    routers are included late in ``atr.api.main``, and an import-time snapshot of
    the route table would be taken before that ran.
    """
    spec = auth_client.get("/openapi.json").json()
    paths = set(spec["paths"])
    for expected in (
        "/api/v1/monitor/deployments/{deployment_id}",
        "/api/v1/monitor/deployments/{deployment_id}/status",
        "/api/v1/monitor/deployments/{deployment_id}/pnl",
        "/api/v1/monitor/deployments/{deployment_id}/timeline",
        "/api/v1/monitor/deployments/{deployment_id}/signals",
        "/api/v1/monitor/deployments/{deployment_id}/trades",
        "/api/v1/monitor/deployments/{deployment_id}/risk",
    ):
        assert expected in paths, f"{expected} is not routed"


def test_the_overview_serves_a_client_consumable_payload(auth_client):
    """The whole screen in one call, and it must parse as JSON."""
    deployment_id = _deployment(auth_client)

    response = auth_client.get(f"/api/v1/monitor/deployments/{deployment_id}")
    assert response.status_code == 200, response.text

    body = response.json()
    for key in (
        "status", "pnl", "positions", "orders", "fills",
        "signals", "trades", "risk", "timeline",
    ):
        assert key in body, f"{key} missing from the overview"

    # ``inf`` is not valid JSON. A limits payload that carried it would break
    # every browser that tried to parse this.
    assert "Infinity" not in response.text
    assert not isinstance(body["risk"]["limits"], str)
    assert body["risk"]["limits"]["kill_switch"] in (True, False)


def test_the_timeline_route_reports_its_stages(auth_client):
    """The UI draws a pipeline rail, so the rail's definition comes from the API."""
    deployment_id = _deployment(auth_client)

    response = auth_client.get(
        f"/api/v1/monitor/deployments/{deployment_id}/timeline"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["events"] == []
    assert body["total"] == 0
    assert body["stages"] == ["signal", "risk", "order", "fill", "position"]


def test_an_idle_deployment_reports_zeroes_not_nulls(auth_client):
    """The empty case must be honest: no positions, and equity at capital."""
    deployment_id = _deployment(auth_client)

    response = auth_client.get(f"/api/v1/monitor/deployments/{deployment_id}/pnl")
    assert response.status_code == 200, response.text
    pnl = response.json()
    assert pnl["positions"] == []
    assert pnl["equity"] == 500_000.0
    assert pnl["capital"] == 500_000.0
    # No prior equity exists, so today's figure is null rather than a fake zero.
    assert pnl["today_pnl"] is None


def test_monitoring_is_owner_scoped_over_http(auth_client):
    """Another account must not be able to read this deployment."""
    deployment_id = _deployment(auth_client)
    other_token = _bootstrap_second_user(auth_client)

    response = auth_client.get(
        f"/api/v1/monitor/deployments/{deployment_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "not_found"


def test_an_unknown_deployment_is_a_404(auth_client):
    response = auth_client.get("/api/v1/monitor/deployments/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


def test_monitoring_requires_authentication(client):
    """No token, no monitoring."""
    response = client.get("/api/v1/monitor/deployments/anything")
    assert response.status_code in (401, 403), response.status_code
