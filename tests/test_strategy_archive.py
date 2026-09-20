"""Removing a strategy hides it; it never erases the versions and runs that reference it."""

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


def test_a_removed_strategy_leaves_the_list_but_can_be_brought_back(auth_client):
    strategy_id = _seed(auth_client)
    assert strategy_id in _ids(auth_client)

    removed = auth_client.delete(f"/api/v1/strategies/{strategy_id}")

    assert removed.status_code == 200 and removed.json()["archived"] is True
    assert strategy_id not in _ids(auth_client)
    # Archived, not erased: it is still readable, so runs that pinned it are not orphaned.
    assert auth_client.get(f"/api/v1/strategies/{strategy_id}").status_code == 200
    assert auth_client.get("/api/v1/strategies?include_archived=true").json()["total"] >= 1

    restored = auth_client.post(f"/api/v1/strategies/{strategy_id}/restore")
    assert restored.status_code == 200
    assert strategy_id in _ids(auth_client)


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
    assert auth_client.post("/api/v1/strategies/does-not-exist/restore").status_code == 404


def test_removal_needs_a_signed_in_account(client):
    assert client.delete("/api/v1/strategies/anything").status_code == 401
