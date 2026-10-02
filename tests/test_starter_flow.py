"""The home page's one-click starter, replayed against an empty database: seed strategy → pick stocks
→ create a paper deployment → start it. The UI does exactly these four calls."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atr.api.main import app


@pytest.fixture()
def owner(fresh_env, master) -> TestClient:
    client = TestClient(app, client=("127.0.0.1", 51234))
    r = client.post(
        "/api/v1/auth/bootstrap",
        json={"email": "o@example.com", "username": "owner", "password": "Str0ngPassw0rd", "display_name": "O"},
    )
    assert r.status_code == 201, r.text
    client.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return client


def test_starter_flow_on_an_empty_database(owner, market_cache, monkeypatch):
    monkeypatch.setattr("atr.research.hunt.STOCKS", market_cache / "iifl_daily" / "NSEEQ")
    seeded = owner.post("/api/v1/strategies/seed")
    assert seeded.status_code in (200, 201), seeded.text
    body = seeded.json()
    universe = owner.get("/api/v1/strategies/universe/researched")
    assert universe.status_code == 200, universe.text
    symbols = universe.json()["symbols"]
    assert symbols, "the synthetic cache should offer stocks"

    made = owner.post(
        "/api/v1/paper/deployments",
        json={
            "strategy_id": body["strategy"]["strategy_id"],
            "strategy_version": body["version"]["version"],
            "capital": 500_000,
            "mode": "PAPER",
            "config": {"symbols": symbols, "exchange": "NSEEQ", "timeframe": "1d", "max_open_positions": 10},
        },
    )
    assert made.status_code == 201, made.text
    started = owner.post(f"/api/v1/paper/deployments/{made.json()['deployment_id']}/start")
    assert started.status_code == 200, started.text

    # Running it twice must not create a second strategy.
    again = owner.post("/api/v1/strategies/seed")
    assert again.status_code in (200, 201)
    assert again.json()["strategy"]["strategy_id"] == body["strategy"]["strategy_id"]
