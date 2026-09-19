"""Champion vs challenger over HTTP: launch refusals and the read-only comparison.

The service tests prove the payloads are right. These prove the wiring: that
launching copies the champion's conditions rather than trusting the caller to,
that every refusal carries its own code, and that the comparison surface can
report readiness without being able to promote, pause, edit or deploy
anything — there is deliberately no route that could.
"""

from __future__ import annotations

CONFIG = {
    "symbols": ["RELIANCE"],
    "exchange": "NSEEQ",
    "timeframe": "1d",
    "order_value": 250_000.0,
    "lookback_days": 400,
    "max_open_positions": 1,
}

V1 = {
    "engine_key": None,
    "rules": {
        "entry": {
            "breakout_lookback": 63,
            "breakout_proximity_pct": 2.0,
            "volume_multiple": 1.5,
            "volume_lookback": 20,
            "min_history_bars": 70,
        },
        "exit": {"stop_loss_pct": 3.0, "min_history_bars": 70},
    },
}
V2 = {
    "engine_key": None,
    "rules": {
        "entry": {
            "breakout_lookback": 63,
            "breakout_proximity_pct": 2.0,
            "volume_multiple": 1.9,
            "volume_lookback": 20,
            "min_history_bars": 70,
        },
        "exit": {"stop_loss_pct": 3.0, "min_history_bars": 70},
    },
}


def _owner_id() -> str:
    from atr.appdb.engine import get_app_db
    from atr.appdb.repositories import UserRepository

    with get_app_db().session() as session:
        user = UserRepository.get_by_identifier(session, "owner@example.com")
    assert user is not None
    return user["user_id"]


def _saved_strategy(definitions=(V1, V2), *, name: str = "Breakout live") -> str:
    from atr.appdb.engine import get_app_db
    from atr.appdb.repositories import StrategyRepository

    user_id = _owner_id()
    with get_app_db().session() as session:
        created = StrategyRepository.create(
            session, user_id=user_id, name=name, kind="rules"
        )
        for definition in definitions:
            StrategyRepository.create_version(
                session,
                strategy_id=created["strategy_id"],
                author_user_id=user_id,
                definition=definition,
            )
    return created["strategy_id"]


def _config_of(deployment: dict) -> dict:
    import json

    raw = deployment.get("config")
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def _deploy(auth_client, strategy_id: str, version: int, *, start: bool = False) -> dict:
    response = auth_client.post(
        "/api/v1/paper/deployments",
        json={
            "strategy_id": strategy_id,
            "strategy_version": version,
            "capital": 500_000.0,
            "config": CONFIG,
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    if start:
        started = auth_client.post(
            f"/api/v1/paper/deployments/{created['deployment_id']}/start"
        )
        assert started.status_code == 200, started.text
    return created


def _start(auth_client, deployment_id: str) -> None:
    response = auth_client.post(f"/api/v1/paper/deployments/{deployment_id}/start")
    assert response.status_code == 200, response.text


def _launch(auth_client, strategy_id: str, champion_id: str, version: int):
    return auth_client.post(
        "/api/v1/paper/challengers",
        json={
            "strategy_id": strategy_id,
            "champion_deployment_id": champion_id,
            "challenger_version": version,
        },
    )


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------


def test_launch_copies_the_champions_conditions(auth_client):
    strategy_id = _saved_strategy()
    champion = _deploy(auth_client, strategy_id, 1, start=True)

    response = _launch(auth_client, strategy_id, champion["deployment_id"], 2)
    assert response.status_code == 201, response.text
    challenger = response.json()
    assert challenger["strategy_version"] == 2
    assert challenger["mode"] == "PAPER"
    assert challenger["capital"] == champion["capital"] == 500_000.0
    assert _config_of(challenger) == _config_of(champion) == CONFIG
    parity = challenger["parity"]
    assert parity["champion_deployment_id"] == champion["deployment_id"]
    assert parity["venue_shared"] is True


def test_launch_refuses_the_champions_own_version(auth_client):
    strategy_id = _saved_strategy()
    champion = _deploy(auth_client, strategy_id, 1, start=True)

    response = _launch(auth_client, strategy_id, champion["deployment_id"], 1)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "challenger_is_champion"


def test_launch_refuses_a_champion_that_is_not_running(auth_client):
    strategy_id = _saved_strategy()
    champion = _deploy(auth_client, strategy_id, 1, start=False)

    response = _launch(auth_client, strategy_id, champion["deployment_id"], 2)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "champion_not_running"


def test_launch_refuses_an_unknown_version(auth_client):
    strategy_id = _saved_strategy()
    champion = _deploy(auth_client, strategy_id, 1, start=True)

    response = _launch(auth_client, strategy_id, champion["deployment_id"], 9)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "version_not_found"


def test_launch_refuses_a_second_challenger_for_one_version(auth_client):
    strategy_id = _saved_strategy()
    champion = _deploy(auth_client, strategy_id, 1, start=True)
    first = _launch(auth_client, strategy_id, champion["deployment_id"], 2)
    assert first.status_code == 201, first.text
    auth_client.post(
        f"/api/v1/paper/deployments/{first.json()['deployment_id']}/start"
    )

    response = _launch(auth_client, strategy_id, champion["deployment_id"], 2)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "challenger_already_running"


def test_launch_refuses_a_champion_of_another_strategy(auth_client):
    strategy_id = _saved_strategy()
    other_id = _saved_strategy(name="Breakout other")
    champion = _deploy(auth_client, other_id, 1, start=True)

    response = _launch(auth_client, strategy_id, champion["deployment_id"], 2)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "champion_mismatch"


def test_launch_requires_authentication(client):
    assert client.post("/api/v1/paper/challengers", json={}).status_code in (401, 403, 422)


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def test_compare_with_no_trades_reports_insufficient_not_zero(auth_client):
    strategy_id = _saved_strategy()
    champion = _deploy(auth_client, strategy_id, 1, start=True)
    launched = _launch(auth_client, strategy_id, champion["deployment_id"], 2)
    assert launched.status_code == 201, launched.text
    _start(auth_client, launched.json()["deployment_id"])

    response = auth_client.get(
        "/api/v1/paper/champions/compare",
        params={"strategy_id": strategy_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["champion_version"] == 1
    assert body["challenger_version"] == 2
    assert body["comparison"]["verdict"] == "INSUFFICIENT EVIDENCE"
    assert body["comparison"]["champion"]["n"] == 0
    assert body["comparison"]["challenger"]["n"] == 0
    assert [(t["version"], t["role"]) for t in body["timeline"]] == [
        (1, "CHAMPION"),
        (2, "CHALLENGER"),
    ]
    assert [t["forward_observations"] for t in body["timeline"]] == [0, 0]
    changes = body["definition_diff"]["changes"]
    assert changes == [
        {
            "parameter": "rules.entry.volume_multiple",
            "champion": 1.5,
            "challenger": 1.9,
        }
    ]
    assert body["parity"]["identical_capital"] is True
    assert body["parity"]["identical_config"] is True
    assert body["advisory_only"] is True
    assert body["applies_changes"] is False
    assert body["promotes"] is False


def test_compare_needs_two_running_arms(auth_client):
    strategy_id = _saved_strategy()
    _deploy(auth_client, strategy_id, 1, start=True)

    response = auth_client.get(
        "/api/v1/paper/champions/compare", params={"strategy_id": strategy_id}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "needs_two_arms"


def test_compare_rejects_a_version_against_itself(auth_client):
    strategy_id = _saved_strategy()

    response = auth_client.get(
        "/api/v1/paper/champions/compare",
        params={"strategy_id": strategy_id, "champion_version": 1, "challenger_version": 1},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "same_version"


def test_compare_rejects_an_unknown_version(auth_client):
    strategy_id = _saved_strategy()

    response = auth_client.get(
        "/api/v1/paper/champions/compare",
        params={"strategy_id": strategy_id, "champion_version": 1, "challenger_version": 9},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "version_not_found"


def test_compare_requires_authentication(client):
    assert client.get("/api/v1/paper/champions/compare").status_code in (401, 403, 422)
