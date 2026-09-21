"""Strategy authoring: create a strategy, append immutable versions, validate them.

What this suite protects
------------------------

``strategies`` and ``strategy_versions`` existed with a repository and no way to
reach it: ``GET /strategies`` reads the code registry, so a stored version could
only be produced by a script. A paper deployment pins ``(strategy_id,
strategy_version)`` and the runner refuses to trade a version it cannot resolve
— correctly, because the alternative is stamping a strategy's name onto rules it
never ran — so the platform's headline chain started at a row nobody could
create. These tests are the other end of that: the pair is authorable over HTTP,
and the version it produces is the version the runner trades.

Three properties the assertions are actually about:

* **A version is immutable.** No route updates a definition. Re-posting the same
  rules is a 409 naming the version that already holds them, not version 3 and 4
  of one strategy.
* **A stored version must be runnable.** Creation refuses a definition with
  structural errors, because a version cannot be edited afterwards and its
  failure mode — a deployment that reports itself running and places no orders —
  is indistinguishable from a quiet market.
* **Validation is structural, and says so.** ``ok: true`` means the definition
  will execute. Every validation response carries
  ``statistical_validation.performed: false`` with the reason, so a green tick
  cannot be read as a finding. Nothing in this file measured a return.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("fresh_env")

#: A definition the live loop can genuinely run: a breakout entry and two live
#: exits. Deliberately *not* the seed example, so a change to the example cannot
#: make these tests pass or fail for the wrong reason.
DEFINITION = {
    "engine_key": "signals_entry",
    "rules": {
        "entry": {
            "breakout_lookback": 20,
            "breakout_proximity_pct": 2.0,
            "volume_multiple": 1.5,
            "volume_lookback": 20,
            "min_history_bars": 60,
        },
        "exit": {"stop_loss_pct": 5.0, "take_profit_pct": 10.0},
    },
    "params": {
        "breakout_lookback": 20,
        "breakout_proximity_pct": 2.0,
        "volume_multiple": 1.5,
        "volume_lookback": 20,
        "min_history_bars": 60,
        "stop_loss_pct": 5.0,
        "take_profit_pct": 10.0,
    },
}

BASE = "/api/v1/strategies"


# --------------------------------------------------------------------- helpers
def _create(client, **overrides) -> dict:
    body = {"name": "Breakout A", "kind": "rules", "description": "authored in a test"}
    body.update(overrides)
    response = client.post(BASE, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _version(client, strategy_id: str, definition=DEFINITION, **overrides) -> dict:
    body = {"definition": definition, "change_note": "v1"}
    body.update(overrides)
    response = client.post(f"{BASE}/{strategy_id}/versions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def other_author(fresh_env, master):
    """A second account that *may* author, so ownership is the only thing tested.

    A viewer cannot reach the ownership check at all — the permission gate
    answers first — so proving "another account's strategy is a 404" needs a
    caller whose role would otherwise be allowed to do it.
    """
    from fastapi.testclient import TestClient

    from atr.api.main import app
    from atr.appdb.engine import get_app_db
    from atr.appdb.repositories import UserRepository
    from atr.auth.passwords import hash_password

    with get_app_db().session() as session:
        UserRepository.create(
            session,
            email="other@example.com",
            username="other",
            password_hash=hash_password("Str0ngPassw0rd"),
            role="owner",
        )
    client = TestClient(app, client=("127.0.0.1", 51234))
    response = client.post(
        "/api/v1/auth/login",
        json={"identifier": "other", "password": "Str0ngPassw0rd"},
    )
    assert response.status_code == 200, response.text
    client.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    return client


@pytest.fixture()
def viewer(fresh_env, master) -> object:
    """A read-only account, for the authorization assertions."""
    from fastapi.testclient import TestClient

    from atr.api.main import app
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
    client = TestClient(app, client=("127.0.0.1", 51234))
    response = client.post(
        "/api/v1/auth/login",
        json={"identifier": "viewer", "password": "Str0ngPassw0rd"},
    )
    assert response.status_code == 200, response.text
    client.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    return client


# =========================================================================== wiring
def test_the_authoring_surface_requires_authentication(client):
    assert client.get(BASE).status_code == 401
    assert client.post(BASE, json={"name": "x"}).status_code == 401
    assert client.post(f"{BASE}/seed").status_code == 401


def test_a_viewer_can_read_but_not_author(viewer):
    assert viewer.get(BASE).status_code == 200

    denied = viewer.post(BASE, json={"name": "Not mine"})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"

    denied = viewer.post(f"{BASE}/seed")
    assert denied.status_code == 403


# =========================================================================== create
def test_a_strategy_is_created_with_no_version(auth_client):
    created = _create(auth_client)

    assert created["strategy_id"]
    assert created["name"] == "Breakout A"
    assert created["kind"] == "rules"
    # Not an error: it is the strategy whose rules are still being written. What
    # matters is that it says so, and that a deployment cannot pin it.
    assert created["latest_version"] is None
    assert created["version_count"] == 0

    listed = auth_client.get(BASE).json()
    assert [s["strategy_id"] for s in listed["strategies"]] == [created["strategy_id"]]
    assert listed["total"] == 1


def test_a_duplicate_name_is_refused(auth_client):
    _create(auth_client)
    again = auth_client.post(BASE, json={"name": "breakout a", "kind": "rules"})
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "name_taken"


def test_an_unknown_kind_is_refused(auth_client):
    response = auth_client.post(BASE, json={"name": "x", "kind": "magic"})
    assert response.status_code == 422


def test_another_accounts_strategy_is_a_404_not_a_403(auth_client, viewer, other_author):
    created = _create(auth_client)

    # A viewer never reaches the ownership check — the permission gate answers
    # first, which is the right order: the role is not allowed to author at all.
    assert viewer.get(f"{BASE}/{created['strategy_id']}").status_code == 404
    assert viewer.post(
        f"{BASE}/{created['strategy_id']}/versions", json={"definition": DEFINITION}
    ).status_code == 403

    # A caller who *may* author still cannot see or extend another's strategy, and
    # a 404 rather than a 403 is deliberate: a 403 would confirm the id exists.
    assert other_author.get(f"{BASE}/{created['strategy_id']}").status_code == 404
    assert other_author.get(f"{BASE}/{created['strategy_id']}/versions").status_code == 404
    assert other_author.get(f"{BASE}/{created['strategy_id']}/versions/1").status_code == 404
    assert other_author.post(
        f"{BASE}/{created['strategy_id']}/versions", json={"definition": DEFINITION}
    ).status_code == 404
    assert other_author.post(
        f"{BASE}/{created['strategy_id']}/validate", json={"definition": DEFINITION}
    ).status_code == 404
    # And it is not in their listing.
    assert other_author.get(BASE).json()["total"] == 0


# ========================================================================== versions
def test_create_version_then_read_it_back(auth_client):
    created = _create(auth_client)
    version = _version(auth_client, created["strategy_id"])

    assert version["version"] == 1
    assert version["strategy_id"] == created["strategy_id"]
    assert version["change_note"] == "v1"
    assert version["deployable"] is True
    assert version["not_deployable_reason"] is None
    assert version["definition_hash"]

    # Retrieval by exact number is the property a deployment depends on.
    fetched = auth_client.get(f"{BASE}/{created['strategy_id']}/versions/1").json()
    assert fetched["version"] == 1
    assert fetched["definition"]["rules"]["exit"]["stop_loss_pct"] == 5.0
    assert fetched["definition_hash"] == version["definition_hash"]

    # The strategy now advertises it.
    assert auth_client.get(f"{BASE}/{created['strategy_id']}").json()["latest_version"] == 1


def test_a_missing_version_number_is_a_404_not_the_latest(auth_client):
    created = _create(auth_client)
    _version(auth_client, created["strategy_id"])

    response = auth_client.get(f"{BASE}/{created['strategy_id']}/versions/2")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "version_not_found"


def test_two_versions_are_two_subjects(auth_client):
    created = _create(auth_client)
    first = _version(auth_client, created["strategy_id"])

    tightened = {
        "rules": {
            "entry": DEFINITION["rules"]["entry"],
            "exit": {"stop_loss_pct": 2.5, "take_profit_pct": 10.0},
        }
    }
    second = _version(auth_client, created["strategy_id"], definition=tightened, change_note="tighten")

    assert second["version"] == 2
    assert second["definition_hash"] != first["definition_hash"]

    v1 = auth_client.get(f"{BASE}/{created['strategy_id']}/versions/1").json()
    assert v1["definition"]["rules"]["exit"]["stop_loss_pct"] == 5.0, "v1 was mutated"
    assert auth_client.get(
        f"{BASE}/{created['strategy_id']}/versions/2"
    ).json()["definition"]["rules"]["exit"]["stop_loss_pct"] == 2.5


def test_an_identical_definition_is_a_conflict_not_a_new_version(auth_client):
    """Two identical definitions are one strategy, not v1 and v2 of two names."""
    created = _create(auth_client)
    first = _version(auth_client, created["strategy_id"])

    again = auth_client.post(
        f"{BASE}/{created['strategy_id']}/versions",
        json={"definition": DEFINITION},
    )
    assert again.status_code == 409
    body = again.json()["detail"]
    assert body["code"] == "duplicate_definition"
    assert body["existing_version"] == first["version"]

    # Key order must not defeat the dedupe: the same rules re-serialised are the
    # same rules.
    reordered = {k: DEFINITION[k] for k in reversed(list(DEFINITION))}
    shuffled = auth_client.post(
        f"{BASE}/{created['strategy_id']}/versions", json={"definition": reordered}
    )
    assert shuffled.status_code == 409


def test_there_is_no_route_that_edits_a_version(auth_client):
    """A version's rules are never changed in place: it is created, or deleted, never patched.

    Deleting is allowed and tested in test_strategy_archive; editing means a new version.
    """
    created = _create(auth_client)
    _version(auth_client, created["strategy_id"])

    for method, path in (
        ("patch", f"{BASE}/{created['strategy_id']}/versions/1"),
        ("put", f"{BASE}/{created['strategy_id']}/versions/1"),
        ("patch", f"{BASE}/{created['strategy_id']}"),
    ):
        response = getattr(auth_client, method)(path)
        assert response.status_code in (404, 405), f"{method} {path} -> {response.status_code}"


def test_a_broken_definition_is_refused_before_it_is_stored(auth_client):
    """A typo must not become an immutable row the loop then refuses to trade."""
    created = _create(auth_client)
    response = auth_client.post(
        f"{BASE}/{created['strategy_id']}/versions",
        json={"definition": {"rules": {"entry": {"breakout_lokback": 20}, "exit": {"stop_loss_pct": 5.0}}}},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_definition"
    assert any(e["code"] == "unknown_rule_field" for e in detail["errors"])

    # Nothing was written.
    assert auth_client.get(f"{BASE}/{created['strategy_id']}/versions").json()["total"] == 0


def test_a_non_numeric_rule_value_is_refused(auth_client):
    created = _create(auth_client)
    response = auth_client.post(
        f"{BASE}/{created['strategy_id']}/versions",
        json={"definition": {"rules": {"exit": {"stop_loss_pct": "5%"}}}},
    )
    assert response.status_code == 422
    assert any(
        e["code"] == "non_numeric_rule_value" for e in response.json()["detail"]["errors"]
    )


def test_force_stores_a_broken_definition_and_the_listing_says_so(auth_client):
    """The escape hatch exists, and it is not quiet: the version reads as undeployable."""
    created = _create(auth_client)
    response = auth_client.post(
        f"{BASE}/{created['strategy_id']}/versions",
        json={
            "definition": {"rules": {"entry": {"not_a_field": 1}}},
            "force": True,
        },
    )
    assert response.status_code == 201, response.text

    listed = auth_client.get(f"{BASE}/{created['strategy_id']}/versions").json()
    assert listed["deployable_versions"] == []
    assert listed["versions"][0]["deployable"] is False
    assert listed["versions"][0]["not_deployable_reason"]


# ========================================================================== validate
def test_validate_checks_a_draft_without_saving_it(auth_client):
    created = _create(auth_client)
    response = auth_client.post(
        f"{BASE}/{created['strategy_id']}/validate",
        json={"definition": {"rules": {"entry": {"min_history_bars": 60}, "exit": {"stop_loss_pct": 4.0}}}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["checked"] == "draft"
    assert body["stored"] is False
    assert body["version"] is None

    # Validating a draft writes nothing — that is what makes it useful before the
    # definition becomes immutable.
    assert auth_client.get(f"{BASE}/{created['strategy_id']}/versions").json()["total"] == 0


def test_validate_reports_the_stored_version(auth_client):
    created = _create(auth_client)
    _version(auth_client, created["strategy_id"])

    body = auth_client.post(
        f"{BASE}/{created['strategy_id']}/validate", json={"version": 1}
    ).json()
    assert body["ok"] is True
    assert body["checked"] == "stored"
    assert body["stored"] is True
    assert body["version"] == 1
    assert body["paper"]["resolvable"] is True
    assert body["backtest"]["resolvable"] is True

    # Neither version nor definition: the latest, so a bare validate is useful.
    latest = auth_client.post(f"{BASE}/{created['strategy_id']}/validate", json={}).json()
    assert latest["version"] == 1


def test_validate_on_a_strategy_with_no_version_is_a_404(auth_client):
    created = _create(auth_client)
    response = auth_client.post(f"{BASE}/{created['strategy_id']}/validate", json={})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "version_not_found"


def test_every_validation_says_it_measured_nothing(auth_client):
    """The clause that stops a green tick being read as a finding."""
    created = _create(auth_client)
    for body in (
        auth_client.post(f"{BASE}/{created['strategy_id']}/validate", json={"definition": DEFINITION}).json(),
        auth_client.post(
            f"{BASE}/{created['strategy_id']}/validate",
            json={"definition": {"rules": {"exit": {}}}},
        ).json(),
    ):
        measured = body["statistical_validation"]
        assert measured["performed"] is False
        assert "not" in measured["reason"]
        assert measured["how_to_measure"]


def test_validation_warns_when_nothing_can_ever_close_a_position(auth_client):
    """The warning that matters for the forward-observation chain.

    A definition with no live exit rule opens positions and never closes one, so
    it produces no closed trade and no forward observation. It is executable, so
    it is a warning rather than an error — and it has to be said out loud,
    because the symptom is silence.
    """
    created = _create(auth_client)
    body = auth_client.post(
        f"{BASE}/{created['strategy_id']}/validate",
        json={"definition": {"rules": {"entry": {"min_history_bars": 60}, "exit": {}}}},
    ).json()

    assert body["ok"] is True
    codes = {w["code"] for w in body["warnings"]}
    assert "no_exit_rule" in codes
    assert "no closed trade" in next(
        w["message"] for w in body["warnings"] if w["code"] == "no_exit_rule"
    )


def test_validation_warns_when_the_two_paths_disagree(auth_client):
    """A backtest of rules the deployment does not run scores the wrong subject."""
    created = _create(auth_client)
    body = auth_client.post(
        f"{BASE}/{created['strategy_id']}/validate",
        json={
            "definition": {
                "engine_key": "signals_entry",
                "params": {"stop_loss_pct": 9.0},
                "rules": {"entry": {"min_history_bars": 60}, "exit": {"stop_loss_pct": 5.0}},
            }
        },
    ).json()
    assert "rules_and_params_disagree" in {w["code"] for w in body["warnings"]}


# ============================================================================== seed
def test_the_seed_creates_a_deployable_version(auth_client):
    response = auth_client.post(f"{BASE}/seed")
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["created"] is True
    assert body["version"]["version"] == 1
    # The whole reason it ships: this version can actually be traded.
    assert body["version"]["deployable"] is True
    assert body["version"]["validation"]["ok"] is True
    assert body["version"]["validation"]["statistical_validation"]["performed"] is False


def test_the_seed_is_idempotent(auth_client):
    """A command that fails the second time it runs is not a setup step."""
    first = auth_client.post(f"{BASE}/seed").json()
    second = auth_client.post(f"{BASE}/seed")
    assert second.status_code == 201
    body = second.json()

    assert body["created"] is False
    assert body["version"]["version"] == first["version"]["version"]
    assert body["strategy"]["strategy_id"] == first["strategy"]["strategy_id"]
    assert auth_client.get(f"{BASE}").json()["total"] == 1


def test_the_seed_refuses_a_name_it_does_not_own(auth_client):
    """Two strategies with one name is worse than a refusal."""
    _create(auth_client, name="Example breakout")
    response = auth_client.post(f"{BASE}/seed")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "seed_name_taken"


# ======================================================== the deployment boundary
def test_a_deployment_cannot_pin_a_version_that_does_not_exist(auth_client):
    """A typo used to produce a deployment that ran and never traded."""
    created = _create(auth_client)
    _version(auth_client, created["strategy_id"])

    response = auth_client.post(
        "/api/v1/paper/deployments",
        json={
            "strategy_id": created["strategy_id"],
            "strategy_version": 7,
            "capital": 250_000.0,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "version_not_found"


def test_a_deployment_cannot_pin_a_version_that_cannot_trade(auth_client):
    created = _create(auth_client)
    auth_client.post(
        f"{BASE}/{created['strategy_id']}/versions",
        json={"definition": {"engine_key": "signals_entry"}, "force": True},
    )

    response = auth_client.post(
        "/api/v1/paper/deployments",
        json={
            "strategy_id": created["strategy_id"],
            "strategy_version": 1,
            "capital": 250_000.0,
        },
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "version_not_deployable"
    assert "signal_rules" in detail["detail"]


def test_a_deployment_can_pin_the_exact_version_that_was_authored(auth_client):
    """The point of the whole surface: author it, then deploy *that* version."""
    created = _create(auth_client)
    version = _version(auth_client, created["strategy_id"])

    response = auth_client.post(
        "/api/v1/paper/deployments",
        json={
            "strategy_id": created["strategy_id"],
            "strategy_version": version["version"],
            "capital": 250_000.0,
            "config": {"symbols": ["RELIANCE"], "exchange": "NSEEQ"},
        },
    )
    assert response.status_code == 201, response.text
    deployment = response.json()
    assert deployment["strategy_id"] == created["strategy_id"]
    assert deployment["strategy_version"] == 1
    assert deployment["status"] == "PENDING"

    started = auth_client.post(
        f"/api/v1/paper/deployments/{deployment['deployment_id']}/start"
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "RUNNING"
