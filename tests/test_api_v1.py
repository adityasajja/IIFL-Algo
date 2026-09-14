"""API-level tests for the ``/api/v1`` surface.

These exercise the wiring rather than the logic: that the permission dependency is
actually attached, that the dev bypass is as narrow as it claims, that a rate
limit returns 429 rather than 500, and that the audit trail records what happened.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atr.api.main import app
from atr.auth.rate_limit import reset_rate_limiter
from atr.config.settings import get_settings

LOOPBACK = ("127.0.0.1", 51234)
REMOTE = ("203.0.113.9", 51234)


def _client(peer=LOOPBACK) -> TestClient:
    return TestClient(app, client=peer)


# ─── authentication is required, from everywhere ──────────────────────────────
def test_a_fresh_instance_reports_that_it_needs_setup(client):
    response = client.get("/api/v1/auth/bootstrap")
    assert response.status_code == 200
    body = response.json()
    assert body["needs_setup"] is True
    assert body["allow_signup"] is False  # a trading platform takes no strangers


def test_the_versioned_api_requires_authentication_even_from_loopback(client, master):
    """There is no loopback bypass.

    An earlier design exempted local requests while no account existed, so the
    operator's own tooling kept working on a fresh checkout. It was removed: the
    synthetic principal could not own a row, so its first write died on a foreign
    key, and a bypass that grants write authority to an unauthenticated request is
    the wrong shape for a trading platform. Creating the first account is one
    request, and the response authenticates the browser from then on.
    """
    response = client.get("/api/v1/instruments/status")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthenticated"


def test_a_remote_address_gets_the_same_401(fresh_env, master):
    response = _client(REMOTE).get("/api/v1/instruments/status")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthenticated"


def test_legacy_routes_stay_open_so_the_existing_dashboard_is_unaffected(client):
    """Phase 1 gates the new surface; it does not lock the operator out."""
    assert client.get("/health").status_code == 200
    assert client.get("/strategies").status_code == 200


def test_auth_required_false_yields_a_read_only_anonymous_principal(
    fresh_env, master, monkeypatch
):
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    get_settings.cache_clear()

    anonymous = _client()
    assert anonymous.get("/api/v1/instruments/status").status_code == 200
    denied = anonymous.post("/api/v1/watchlists", json={"name": "Nope"})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"


def test_bootstrap_can_only_happen_once(auth_client):
    response = auth_client.post(
        "/api/v1/auth/bootstrap",
        json={"email": "second@example.com", "username": "second", "password": "Str0ngPassw0rd"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_bootstrapped"


def test_bootstrap_rejects_a_weak_password(client):
    response = client.post(
        "/api/v1/auth/bootstrap",
        json={"email": "o@example.com", "username": "owner", "password": "weak"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "weak_password"


def test_signup_is_refused_by_default(auth_client):
    response = auth_client.post(
        "/api/v1/auth/register",
        json={"email": "newbie@example.com", "username": "newbie", "password": "Str0ngPassw0rd"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "signup_disabled"


# ─── login, session, profile ──────────────────────────────────────────────────
def test_login_sets_a_cookie_and_returns_a_token(auth_client):
    fresh = _client()
    response = fresh.post(
        "/api/v1/auth/login", json={"identifier": "owner", "password": "Str0ngPassw0rd"}
    )
    assert response.status_code == 200
    assert response.json()["token"]
    assert "atr_session" in response.cookies

    me = fresh.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["role"] == "owner"
    assert "order:place" in me.json()["permissions"]


def test_a_bad_password_is_401_with_a_stable_code(auth_client):
    response = _client().post(
        "/api/v1/auth/login", json={"identifier": "owner", "password": "wrong"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid"


def test_the_session_cookie_is_http_only_and_lax(auth_client):
    response = _client().post(
        "/api/v1/auth/login", json={"identifier": "owner", "password": "Str0ngPassw0rd"}
    )
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.replace("samesite", "SameSite")


def test_logout_revokes_the_session(auth_client):
    token = auth_client.headers["Authorization"].split(" ", 1)[1]
    assert auth_client.post("/api/v1/auth/logout").status_code == 200
    assert _client().get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_changing_the_password_requires_the_current_one(auth_client):
    bad = auth_client.post(
        "/api/v1/auth/me/password",
        json={"current_password": "nope", "new_password": "An0therG00dPass"},
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "bad_password"


def test_me_requires_authentication(fresh_env, master):
    response = _client(REMOTE).get("/api/v1/auth/me")
    assert response.status_code == 401


# ─── permissions actually attached ────────────────────────────────────────────
@pytest.fixture()
def viewer_client(auth_client):
    """A viewer-role account, logged in.

    Created directly through the repository because both account-creation routes
    are deliberately closed: ``/register`` is off by default and
    ``PATCH /users/{id}`` can only change an existing user. A viewer can only
    come from an admin action, which is the point.
    """
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
    fresh = _client()
    login = fresh.post(
        "/api/v1/auth/login", json={"identifier": "viewer", "password": "Str0ngPassw0rd"}
    )
    assert login.status_code == 200
    fresh.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return fresh


def test_a_viewer_can_read_but_not_write(viewer_client):
    assert viewer_client.get("/api/v1/watchlists").status_code == 200
    denied = viewer_client.post("/api/v1/watchlists", json={"name": "Nope"})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"
    assert denied.json()["detail"]["required"] == "watchlist:write"


def test_a_viewer_cannot_read_the_audit_trail(viewer_client):
    response = viewer_client.get("/api/v1/audit/events")
    assert response.status_code == 403
    assert response.json()["detail"]["required"] == "audit:read"


def test_an_owner_can_read_the_audit_trail(auth_client):
    assert auth_client.get("/api/v1/audit/events").status_code == 200
    assert auth_client.get("/api/v1/audit/actions").status_code == 200


# ─── watchlists over HTTP ─────────────────────────────────────────────────────
def test_watchlist_lifecycle_over_http(auth_client):
    created = auth_client.post("/api/v1/watchlists", json={"name": "Core"})
    assert created.status_code == 201
    wid = created.json()["watchlist_id"]

    assert auth_client.get("/api/v1/watchlists").json()["watchlists"][0]["name"] == "Core"

    added = auth_client.post(
        f"/api/v1/watchlists/{wid}/items", json={"symbols": ["reliance", "INFY", "NOPE"]}
    )
    assert added.status_code == 200
    assert added.json()["added"] == ["RELIANCE", "INFY"]
    assert added.json()["unknown"] == ["NOPE"]

    reordered = auth_client.put(f"/api/v1/watchlists/{wid}/items/order", json={"symbols": ["INFY"]})
    assert reordered.json()["items"] == ["INFY", "RELIANCE"]

    columns = auth_client.put(
        f"/api/v1/watchlists/{wid}/columns", json={"columns": ["ltp", "rsi14", "made_up"]}
    )
    assert columns.json()["columns"] == ["ltp", "rsi14"]
    assert columns.json()["rejected_columns"] == ["made_up"]

    quotes = auth_client.get(f"/api/v1/watchlists/{wid}/quotes")
    assert quotes.status_code == 200
    assert quotes.json()["count"] == 2
    assert quotes.json()["rows"][0]["rsi14"] is not None

    assert auth_client.delete(f"/api/v1/watchlists/{wid}").json()["deleted"] is True
    assert auth_client.get(f"/api/v1/watchlists/{wid}").status_code == 404


def test_available_columns_explains_what_is_missing(auth_client):
    body = auth_client.get("/api/v1/watchlists/columns/available").json()
    assert "ltp" in body["available"]
    assert "iv" in body["unavailable"]
    spec = next(c for c in body["columns"] if c["key"] == "iv")
    assert spec["available"] is False and spec["requires"]


def test_duplicate_watchlist_name_is_409(auth_client):
    auth_client.post("/api/v1/watchlists", json={"name": "Dup"})
    response = auth_client.post("/api/v1/watchlists", json={"name": "Dup"})
    assert response.status_code == 409


def test_an_unknown_watchlist_id_is_404_not_403(auth_client):
    response = auth_client.get("/api/v1/watchlists/does-not-exist")
    assert response.status_code == 404


# ─── instruments over HTTP ────────────────────────────────────────────────────
def test_instrument_search_and_lookup(auth_client):
    body = auth_client.get("/api/v1/instruments/search", params={"q": "reliance"}).json()
    assert body["results"][0]["symbol"] == "RELIANCE"

    detail = auth_client.get("/api/v1/instruments/RELIANCE-EQ")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Reliance Industries Ltd."

    assert auth_client.get("/api/v1/instruments/NOPE").status_code == 404
    assert auth_client.get("/api/v1/instruments/status").json()["symbols"] >= 4
    exchanges = auth_client.get("/api/v1/instruments/exchanges").json()["exchanges"]
    # One exchange only: the synthetic cache has a single iifl_daily/NSEEQ tree,
    # and the granularity directory must not appear as a second exchange.
    assert [e["exchange"] for e in exchanges] == ["NSEEQ"]
    assert exchanges[0]["symbols"] >= 4


# ─── api keys ─────────────────────────────────────────────────────────────────
def test_an_api_key_authenticates_and_is_scoped(auth_client):
    created = auth_client.post(
        "/api/v1/auth/me/api-keys",
        json={"label": "read only", "scopes": ["market:read", "instrument:read"]},
    )
    assert created.status_code == 201
    raw = created.json()["key"]

    keyed = _client(REMOTE)
    keyed.headers.update({"X-API-Key": raw})
    assert keyed.get("/api/v1/instruments/status").status_code == 200
    # The same key cannot write, because it was never granted watchlist:write.
    denied = keyed.post("/api/v1/watchlists", json={"name": "Nope"})
    assert denied.status_code == 403

    listing = auth_client.get("/api/v1/auth/me/api-keys").json()["keys"]
    assert listing[0]["prefix"] in raw
    assert "key" not in listing[0]  # the secret is never returned again

    key_id = created.json()["key_id"]
    assert auth_client.delete(f"/api/v1/auth/me/api-keys/{key_id}").status_code == 200
    assert keyed.get("/api/v1/instruments/status").status_code == 401


# ─── audit ────────────────────────────────────────────────────────────────────
def test_login_and_denials_are_recorded_in_the_audit_trail(auth_client):
    _client().post("/api/v1/auth/login", json={"identifier": "owner", "password": "wrong"})
    _client().post(
        "/api/v1/auth/login", json={"identifier": "owner", "password": "Str0ngPassw0rd"}
    )

    events = auth_client.get("/api/v1/audit/events", params={"action": "auth.login"}).json()
    assert events["total"] >= 2
    results = {e["result"] for e in events["events"]}
    assert "failure" in results and "success" in results
    # The actor is recorded, not just an id.
    assert any(e["actor"] == "owner@example.com" for e in events["events"])


def test_watchlist_writes_are_audited(auth_client):
    auth_client.post("/api/v1/watchlists", json={"name": "Audited"})
    events = auth_client.get("/api/v1/audit/events", params={"action": "watchlist.create"}).json()
    assert events["total"] == 1
    assert events["events"][0]["target_type"] == "watchlist"


def test_the_legacy_jsonl_audit_trail_still_exists(auth_client):
    """The restart-proof file sink is not replaced by the queryable table."""
    from atr.audit.log import read_file

    auth_client.post("/api/v1/watchlists", json={"name": "FileSink"})
    entries = read_file(limit=50)
    assert any(e.get("action") == "watchlist.create" for e in entries)


# ─── transport-level hardening ────────────────────────────────────────────────
def test_security_headers_and_request_id(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers
    assert response.headers["X-Request-ID"]


def test_a_client_supplied_request_id_is_echoed(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
    assert response.headers["X-Request-ID"] == "trace-abc-123"


def test_an_oversized_request_id_is_bounded(client):
    response = client.get("/health", headers={"X-Request-ID": "x" * 200})
    assert len(response.headers["X-Request-ID"]) == 32


def test_csrf_origin_check_applies_to_cookie_auth_only(fresh_env, master):
    session_client = _client()
    bootstrapped = session_client.post(
        "/api/v1/auth/bootstrap",
        json={"email": "o@example.com", "username": "owner", "password": "Str0ngPassw0rd"},
    )
    assert bootstrapped.status_code == 201
    # Cookie only — no Authorization header, so ambient authority is in play.
    rejected = session_client.post(
        "/api/v1/watchlists", json={"name": "Cross"}, headers={"Origin": "http://evil.example"}
    )
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "csrf_origin_rejected"

    allowed = session_client.post(
        "/api/v1/watchlists", json={"name": "Same"}, headers={"Origin": "http://localhost:8000"}
    )
    assert allowed.status_code == 201

    # A bearer token is not attached automatically by a browser, so no check.
    token = session_client.post(
        "/api/v1/auth/login", json={"identifier": "owner", "password": "Str0ngPassw0rd"}
    ).json()["token"]
    via_token = _client().post(
        "/api/v1/watchlists",
        json={"name": "Token"},
        headers={"Origin": "http://evil.example", "Authorization": f"Bearer {token}"},
    )
    assert via_token.status_code == 201


def test_a_same_origin_request_is_allowed_on_any_port(fresh_env, master):
    """The case the hardcoded port list got wrong.

    Browsers send ``Origin`` on same-origin POSTs too, so `atr serve --port 8123`
    produced ``Origin: http://127.0.0.1:8123`` — not in the allowlist — and every
    write came back 403 as a "cross-origin" request. The rule has to be
    "is this the origin you connected to?", not "is this one of four ports I
    happened to write down".

    ``Host`` is set explicitly because httpx would otherwise send ``testserver``,
    which is not a port anyone would be fooled by.
    """
    session_client = _client()
    assert (
        session_client.post(
            "/api/v1/auth/bootstrap",
            json={"email": "o@example.com", "username": "owner", "password": "Str0ngPassw0rd"},
        ).status_code
        == 201
    )

    for host, origin in (
        ("127.0.0.1:8123", "http://127.0.0.1:8123"),
        ("localhost:9000", "http://localhost:9000"),
        ("atr.internal", "https://atr.internal"),
    ):
        res = session_client.post(
            "/api/v1/watchlists",
            json={"name": f"Same origin {host}"},
            headers={"Origin": origin, "Host": host},
        )
        assert res.status_code == 201, f"{origin} against Host {host}: {res.text}"

    # A different port on the same host is still a different origin, and is
    # refused — the fix must not degrade into "any localhost is fine".
    other_port = session_client.post(
        "/api/v1/watchlists",
        json={"name": "Wrong port"},
        headers={"Origin": "http://127.0.0.1:9999", "Host": "127.0.0.1:8123"},
    )
    assert other_port.status_code == 403
    assert other_port.json()["code"] == "csrf_origin_rejected"

    # `Origin: null` comes from a sandboxed frame or a file:// page.
    nulled = session_client.post(
        "/api/v1/watchlists",
        json={"name": "Null"},
        headers={"Origin": "null", "Host": "127.0.0.1:8123"},
    )
    assert nulled.status_code == 403


def test_rate_limiting_returns_429_with_retry_after(fresh_env, master, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "3")
    get_settings.cache_clear()
    reset_rate_limiter()

    limited = _client()
    codes = [limited.get("/health").status_code for _ in range(6)]
    assert 429 in codes
    assert codes.index(429) <= 3  # it kicks in promptly, not eventually

    blocked = limited.get("/health")
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert blocked.json()["code"] == "rate_limited"


def test_health_still_works_and_is_not_authenticated(client):
    """The environment banner polls this; gating it would break the header."""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "execution_mode" in body and "kill_switch" in body


def test_legacy_routes_are_untouched(auth_client):
    """The SPA depends on these; Phase 1 must not have broken them."""
    for path in ("/strategies", "/history/status", "/risk/status"):
        assert auth_client.get(path).status_code == 200, path
