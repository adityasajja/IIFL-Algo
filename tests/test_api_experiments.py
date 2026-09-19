"""API tests for Strategy Experiment Lab endpoints (``/api/v1/experiments``).

Verifies:
- Experiment creation via POST /api/v1/experiments
- Listing experiments via GET /api/v1/experiments
- Fetching experiment details via GET /api/v1/experiments/{id}
- Running experiment via POST /api/v1/experiments/{id}/run
- Rejection workflow via POST /api/v1/experiments/{id}/reject
- Approval workflow via POST /api/v1/experiments/{id}/approve
- Applying experiment via POST /api/v1/experiments/{id}/apply -> immutable V(N+1)
- Permission gating & validation error handling
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
            "email": "owner_exp@example.com",
            "username": "owner_exp",
            "password": "Str0ngPassw0rd",
            "display_name": "Experiment Owner",
        },
    )
    assert response.status_code == 201, response.text
    client.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    return client


def test_experiments_api_workflow(owner: TestClient):
    # 1. Create a strategy
    strat_res = owner.post(
        "/api/v1/strategies",
        json={
            "name": "API Experiment Strategy",
            "kind": "rules",
        },
    )
    assert strat_res.status_code == 201, strat_res.text
    strat_id = strat_res.json()["strategy_id"]

    # 1b. Create version 1 with adaptive parameter
    ver_res = owner.post(
        f"/api/v1/strategies/{strat_id}/versions",
        json={
            "definition": {
                "rules": {
                    "entry": {"volume_multiple": 1.5, "rsi_threshold": 60},
                    "exit": {"stop_loss_pct": 2.0},
                },
                "adaptive_parameters": [
                    {
                        "name": "volume_multiple",
                        "current": 1.5,
                        "minimum": 1.0,
                        "maximum": 3.0,
                        "step": 0.1,
                        "adaptive": True,
                    }
                ],
            },
            "change_note": "Initial v1",
            "force": True,
        },
    )
    assert ver_res.status_code == 201, ver_res.text

    # 2. List experiments (initially empty)
    list_res = owner.get(f"/api/v1/experiments?strategy_id={strat_id}")
    assert list_res.status_code == 200
    assert list_res.json()["count"] == 0

    # 3. Create experiment
    create_res = owner.post(
        "/api/v1/experiments",
        json={
            "strategy_id": strat_id,
            "parameter_changes": {"volume_multiple": 1.9},
            "reason": "Test volume threshold expansion",
            "name": "Volume 1.9 test",
        },
    )
    assert create_res.status_code == 200, create_res.text
    exp = create_res.json()
    exp_id = exp["experiment_id"]
    assert exp["status"] == "CREATED"
    assert exp["source_version"] == 1
    assert "volume_multiple" in exp["parameter_changes"]

    # 4. Get experiment details
    get_res = owner.get(f"/api/v1/experiments/{exp_id}")
    assert get_res.status_code == 200
    assert get_res.json()["experiment_id"] == exp_id

    # 5. Run experiment
    run_res = owner.post(f"/api/v1/experiments/{exp_id}/run")
    assert run_res.status_code == 200, run_res.text
    run_data = run_res.json()
    assert run_data["status"] == "COMPLETED"
    assert "metrics_table" in run_data["results"]
    assert "explanation" in run_data

    # 6. Reject workflow on a new experiment
    reject_exp_res = owner.post(
        "/api/v1/experiments",
        json={
            "strategy_id": strat_id,
            "parameter_changes": {"volume_multiple": 2.8},
            "reason": "Extreme parameter test",
        },
    )
    reject_exp_id = reject_exp_res.json()["experiment_id"]
    owner.post(f"/api/v1/experiments/{reject_exp_id}/run")

    rej_action_res = owner.post(
        f"/api/v1/experiments/{reject_exp_id}/reject",
        json={"reason": "Excessive drawdown tolerance"},
    )
    assert rej_action_res.status_code == 200
    assert rej_action_res.json()["status"] == "REJECTED"

    # 7. Apply before approval should fail with 400
    bad_apply = owner.post(f"/api/v1/experiments/{exp_id}/apply")
    assert bad_apply.status_code == 400

    # 8. Approve experiment
    appr_res = owner.post(f"/api/v1/experiments/{exp_id}/approve")
    assert appr_res.status_code == 200
    assert appr_res.json()["status"] == "APPROVED"

    # 9. Apply experiment -> generates V(2)
    apply_res = owner.post(f"/api/v1/experiments/{exp_id}/apply")
    assert apply_res.status_code == 200
    apply_data = apply_res.json()
    assert apply_data["experiment"]["status"] == "APPLIED"
    assert apply_data["new_version"]["version"] == 2

    # 10. Verify V(1) unchanged and V(2) has 1.9
    v1_res = owner.get(f"/api/v1/strategies/{strat_id}/versions/1")
    assert v1_res.status_code == 200
    v1_data = v1_res.json()
    assert v1_data["definition"]["rules"]["entry"]["volume_multiple"] == 1.5

    v2_res = owner.get(f"/api/v1/strategies/{strat_id}/versions/2")
    assert v2_res.status_code == 200
    v2_data = v2_res.json()
    assert v2_data["definition"]["rules"]["entry"]["volume_multiple"] == 1.9

