"""The strategy registry that feeds the deploy form: owner-scoped, and populated.

Two bugs lived here, and both were found by driving the real UI rather than by
the suite. They are worth pinning because **neither one raised**.

**The saved half was empty for everyone.** ``strategy_registry`` called
``StrategyRepository.list_for_user`` and unpacked the result as ``(rows, total)``
— a shape ``OrderRepository.list_for_user`` returns but ``StrategyRepository``
does not. The unpack raised ``ValueError``, and the surrounding ``except
Exception`` (whose only job is "an unreachable database must not stop the
built-in list rendering") swallowed it into a ``logger.debug``. Every user, in
every build, saw only the built-ins.

**The list was not owner-scoped.** ``/backtests/options`` had no principal, so
``strategy_registry`` fell through to ``_any_user_id`` — the first row of the
``users`` table. One account saw another account's strategies and not its own.

Why these matter beyond tidiness: **only ``kind: "saved"`` strategies carry
versions**, and a paper deployment must pin an immutable version. So the two bugs
compound. A user with a saved strategy saw a list that omitted it, and the paper
form — which filters to saved strategies — rendered "Cannot deploy yet — pick a
saved strategy" while their strategy sat in the database. The failure presents as
an empty form, which reads as "I have not created one", not as "the server is
hiding it".

The tests below seed strategies through the repository rather than the API: the
bugs were in the repository/service seam, and a test that went around it would
not have caught either. Accounts are made through the real register and login
routes, so the requests under test carry a genuine session for the right user.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("fresh_env", "master")

PASSWORD = "Str0ngPassw0rd"


# ─── helpers ────────────────────────────────────────────────────────────────


def _owner(client) -> str:
    """The user id of the account ``auth_client`` is signed in as.

    Read from ``/auth/me`` rather than bootstrapped here: the fixture has already
    created the owner account, and ``bootstrap`` refuses a second call ("this
    instance is already set up"). Asking the API who the caller is also keeps this
    helper honest about which accounts the tests below actually act as.
    """
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["user_id"]


def _register(client, *, username: str) -> str:
    """Create a second account and return its user id.

    Through ``AuthService.register`` rather than ``POST /auth/register``: the
    public route is gated by the ``allow_signup`` setting, which is off by
    default, and a test that had to flip an instance-wide setting to make a
    second account would be testing the setting as much as the registry. The
    service method is the same one the route calls.

    The password is real, so ``_as`` can sign in through the actual login route —
    the goal is a genuine session for the right user, not a forged one.
    """
    from atr.auth.service import get_auth_service

    user = get_auth_service().register(
        email=f"{username}@example.com",
        username=username,
        password=PASSWORD,
        display_name=username.title(),
    )
    return user["user_id"]


def _as(client, username: str):
    """A second client, signed in as ``username``.

    A separate client rather than a header swap: the first client holds a session
    cookie from ``bootstrap``, and cookies are sent whether or not a header is
    also present. Two clients make "who is asking" unambiguous instead of
    depending on which credential the dependency prefers.
    """
    from fastapi.testclient import TestClient

    from atr.api.main import app

    other = TestClient(app, client=("127.0.0.1", 51235))
    response = other.post(
        "/api/v1/auth/login",
        json={"identifier": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    other.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    return other


def _add_strategy(user_id: str, name: str, *, engine_key: str = "signals_entry"):
    """A saved strategy with one immutable version. Returns ``(id, version)``."""
    from atr.appdb.engine import get_app_db
    from atr.appdb.repositories import StrategyRepository

    with get_app_db().session() as session:
        row = StrategyRepository.create(
            session,
            user_id=user_id,
            name=name,
            kind="rules",
            description=f"{name} — seeded for the registry tests",
            engine_key=engine_key,
        )
        version = StrategyRepository.create_version(
            session,
            strategy_id=row["strategy_id"],
            author_user_id=user_id,
            definition={
                "entry": {"breakout_lookback": 63, "volume_multiple": 1.5},
                "exit": {"stop_loss_pct": 3.0},
            },
            change_note="seeded",
        )
    return row["strategy_id"], version["version"]


def _saved(strategies: list[dict]) -> list[dict]:
    return [s for s in strategies if s.get("kind") == "saved"]


def _options(client) -> list[dict]:
    response = client.get("/api/v1/backtests/options")
    assert response.status_code == 200, response.text
    return response.json()["strategies"]


# ─── the registry actually lists saved strategies ───────────────────────────


def test_the_registry_lists_saved_strategies_at_all(auth_client):
    """The bug that hid every saved strategy behind a swallowed ValueError.

    A regression here is silent: the response is a perfectly valid list of
    built-ins, and nothing in it says a row is missing.
    """
    user_id = _owner(auth_client)
    strategy_id, version = _add_strategy(user_id, "Registry Probe")

    strategies = _options(auth_client)

    found = [s for s in _saved(strategies) if s["strategy_id"] == strategy_id]
    assert found, (
        "the saved strategy did not appear in the registry — the saved half is "
        f"empty again; kinds present were "
        f"{sorted({s['kind'] for s in strategies})}"
    )
    assert found[0]["name"] == "Registry Probe"
    assert found[0]["versions"], "a saved strategy must carry its versions"
    assert [v["version"] for v in found[0]["versions"]] == [version]


def test_the_builtins_are_still_listed(auth_client):
    """The saved-list fix must not have displaced the built-in half."""
    _owner(auth_client)

    builtin = [s for s in _options(auth_client) if s["kind"] == "builtin"]

    assert builtin, "no built-in strategies were listed"
    assert all(s["strategy_id"] is None for s in builtin)
    # A built-in has no version history, which is exactly why it is not
    # deployable and why the deploy form filters to `saved`.
    assert all(s["versions"] == [] for s in builtin)


def test_versions_are_served_newest_first(auth_client):
    """The order the deploy form's "preselect the newest" logic depends on.

    This is pinned because the panel got it wrong *because* the order was never
    written down: it took ``versions[length - 1]``, which on a newest-first list
    is the **oldest**. The comment said "newest", the code did the opposite, and
    the version is what a deployment is pinned to — so the user deployed the
    rules they least likely meant while the screen said otherwise.
    """
    from atr.appdb.engine import get_app_db
    from atr.appdb.repositories import StrategyRepository

    user_id = _owner(auth_client)
    strategy_id, _ = _add_strategy(user_id, "Ordering Probe")

    with get_app_db().session() as session:
        StrategyRepository.create_version(
            session,
            strategy_id=strategy_id,
            author_user_id=user_id,
            definition={
                "entry": {"breakout_lookback": 63},
                "exit": {"stop_loss_pct": 18.0},
            },
            change_note="second version",
        )

    row = next(s for s in _saved(_options(auth_client)) if s["strategy_id"] == strategy_id)
    numbers = [v["version"] for v in row["versions"]]

    assert numbers == sorted(numbers, reverse=True), (
        f"versions were served as {numbers}; this order is what the deploy form's "
        "preselect reads, so it must be written down rather than assumed"
    )
    assert max(numbers) == 2


# ─── owner scoping ──────────────────────────────────────────────────────────


def test_the_registry_shows_only_the_callers_strategies(auth_client):
    """The bug that showed one account another account's strategies.

    Two accounts, one strategy each. Each must see its own and *not* the
    other's — and specifically, the **first row of the users table** must not
    decide what anybody else sees.
    """
    owner_id = _owner(auth_client)
    other_id = _register(auth_client, username="other")
    other = _as(auth_client, "other")

    mine, _ = _add_strategy(owner_id, "Owner Strategy")
    theirs, _ = _add_strategy(other_id, "Other Strategy")

    owner_ids = {s["strategy_id"] for s in _saved(_options(auth_client))}
    other_ids = {s["strategy_id"] for s in _saved(_options(other))}

    assert mine in owner_ids, "the owner could not see its own strategy"
    assert theirs not in owner_ids, (
        "the owner saw another account's strategy — the registry is resolving an "
        "arbitrary user again"
    )
    assert theirs in other_ids, "the second account could not see its own strategy"
    assert mine not in other_ids, "the second account saw the owner's strategy"


def test_the_first_user_does_not_determine_what_others_see(auth_client):
    """The scoping bug stated as its own assertion, from the non-first account.

    ``_any_user_id`` returns the first row of the ``users`` table. When the route
    passes a principal that fallback must never be consulted — otherwise the
    account created first decides the strategy list for everybody. That is what
    happened here: the probe's account was *not* the first user, so it saw none
    of its own strategies and the deploy form looked empty.
    """
    first_id = _owner(auth_client)
    later_id = _register(auth_client, username="later")
    later = _as(auth_client, "later")

    mine, _ = _add_strategy(first_id, "First Strategy")
    theirs, _ = _add_strategy(later_id, "Later Strategy")

    later_ids = {s["strategy_id"] for s in _saved(_options(later))}

    assert theirs in later_ids, (
        "the later account saw none of its own strategies — the first user is "
        "determining the list again"
    )
    assert mine not in later_ids, "the later account saw the first account's strategy"
