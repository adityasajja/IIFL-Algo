"""Removing a strategy erases it and everything that came from it, unless a paper run is live."""

CONFIG = {
    "symbols": ["RELIANCE"],
    "exchange": "NSEEQ",
    "timeframe": "1d",
    "order_value": 250_000.0,
    "lookback_days": 400,
    "max_open_positions": 1,
}


def _ids(client):
    return {s["strategy_id"] for s in client.get("/api/v1/strategies").json()["strategies"]}


def _seed(client):
    response = client.post("/api/v1/strategies/seed")
    assert response.status_code == 201, response.text
    body = response.json()
    return (body.get("strategy") or body).get("strategy_id") or body["strategy"]["strategy_id"]


def test_a_removed_strategy_is_erased_with_its_versions(auth_client):
    strategy_id = _seed(auth_client)
    assert strategy_id in _ids(auth_client)

    removed = auth_client.delete(f"/api/v1/strategies/{strategy_id}")

    assert removed.status_code == 200 and removed.json()["deleted"] is True
    assert strategy_id not in _ids(auth_client)
    assert auth_client.get(f"/api/v1/strategies/{strategy_id}").status_code == 404
    assert auth_client.get(f"/api/v1/strategies/{strategy_id}/versions").status_code == 404


def test_removing_a_strategy_erases_its_stopped_paper_runs(auth_client):
    strategy_id = _seed(auth_client)
    deployment = auth_client.post(
        "/api/v1/paper/deployments",
        json={"strategy_id": strategy_id, "strategy_version": 1, "capital": 500_000.0, "config": CONFIG},
    )
    assert deployment.status_code == 201, deployment.text
    deployment_id = deployment.json().get("deployment_id") or deployment.json()["deployment"]["deployment_id"]
    stopped = auth_client.post(f"/api/v1/paper/deployments/{deployment_id}/stop", json={"reason": "test"})
    assert stopped.status_code == 200, stopped.text

    assert auth_client.delete(f"/api/v1/strategies/{strategy_id}").status_code == 200

    ids = {d["deployment_id"] for d in auth_client.get("/api/v1/paper/deployments").json()["deployments"]}
    assert deployment_id not in ids


def test_a_strategy_with_a_live_paper_run_cannot_be_removed(auth_client):
    strategy_id = _seed(auth_client)
    deployment = auth_client.post(
        "/api/v1/paper/deployments",
        json={"strategy_id": strategy_id, "strategy_version": 1, "capital": 500_000.0, "config": CONFIG},
    )
    assert deployment.status_code == 201, deployment.text

    refused = auth_client.delete(f"/api/v1/strategies/{strategy_id}")

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "strategy_in_use"
    assert "Paper page" in refused.json()["detail"]["detail"]  # says what to do next
    assert strategy_id in _ids(auth_client)  # still there


def test_removing_a_strategy_that_does_not_exist_is_a_404(auth_client):
    assert auth_client.delete("/api/v1/strategies/does-not-exist").status_code == 404


def test_removal_needs_a_signed_in_account(client):
    assert client.delete("/api/v1/strategies/anything").status_code == 401


def test_the_name_of_a_removed_strategy_can_be_used_again(auth_client):
    first = auth_client.post("/api/v1/strategies", json={"name": "ufur"})
    assert first.status_code == 201, first.text
    assert auth_client.delete(f"/api/v1/strategies/{first.json()['strategy_id']}").status_code == 200

    again = auth_client.post("/api/v1/strategies", json={"name": "ufur"})

    assert again.status_code == 201, again.text


def test_the_researched_stock_list_is_offered_for_a_paper_run(auth_client):
    body = auth_client.get("/api/v1/strategies/universe/researched").json()
    assert body["total"] == len(body["symbols"])


def test_a_name_the_feed_would_misread_is_given_its_exact_spelling(monkeypatch):
    from types import SimpleNamespace

    from atr.api.routers import strategies as router

    class Master:
        def load_cached(self, exchanges):
            pass

        def find(self, symbol, exchange):
            table = {"LT-EQ": 11483, "ABB-EQ": 5}
            if symbol not in table:
                raise KeyError(symbol)
            return SimpleNamespace(conid=table[symbol])

    monkeypatch.setattr("atr.brokers.iifl.contracts.InstrumentMaster", Master)
    # the prefix search finds nothing for LT, and the right contract for ABB
    monkeypatch.setattr(
        "atr.scanner.resolve_conid",
        lambda master, name, exchange: (_ for _ in ()).throw(KeyError(name)) if name == "LT" else 5,
    )

    assert router.broker_safe(["LT", "ABB", "NOTLISTED"]) == ["LT-EQ", "ABB", "NOTLISTED"]
