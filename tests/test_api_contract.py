"""The wire contract between ``web/src/api.ts`` and this API.

Every interface in the TypeScript client is a promise about a JSON shape. Nothing
enforces that promise at build time — `tsc` only knows what I told it, and a
server-side rename would surface as a column of em dashes rather than a failure.

These tests are that enforcement. Each one names the TypeScript interface it
guards and asserts that every field the client *reads* is actually present.

The direction matters: the assertion is "the client's fields are a **subset** of
the response", not equality. Adding a field server-side is free and must not
break the build. Renaming one is not, and must.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("fresh_env")


# --------------------------------------------------------------------- helpers
def _missing(payload: dict, required: set[str]) -> set[str]:
    return required - set(payload)


def _assert_has(payload: dict, required: set[str], what: str) -> None:
    missing = _missing(payload, required)
    assert not missing, f"{what} is missing {sorted(missing)}; got {sorted(payload)}"


# ------------------------------------------------------------ auth: api.ts §auth
def test_bootstrap_status_matches_BootstrapStatus(auth_client) -> None:
    body = auth_client.get("/api/v1/auth/bootstrap").json()
    _assert_has(
        body,
        {"needs_setup", "allow_signup", "env", "auth_required"},
        "BootstrapStatus",
    )
    assert body["needs_setup"] is False


def test_me_matches_Principal(auth_client) -> None:
    body = auth_client.get("/api/v1/auth/me").json()
    _assert_has(
        body,
        {
            "user_id",
            "username",
            "email",
            "display_name",
            "role",
            "permissions",
            "auth_method",
            "mfa_satisfied",
            # The sidebar greys buttons out from this, so its absence would make
            # the whole UI read-only with no explanation.
            "role_permissions",
        },
        "Principal",
    )
    assert isinstance(body["permissions"], list)
    assert "watchlist:write" in body["permissions"]
    assert body["auth_method"] == "session"


def test_login_matches_LoginSuccess(auth_client) -> None:
    body = auth_client.post(
        "/api/v1/auth/login",
        json={"identifier": "owner", "password": "Str0ngPassw0rd"},
    ).json()
    _assert_has(body, {"token", "expires_at", "user"}, "LoginSuccess")
    _assert_has(
        body["user"],
        {
            "user_id",
            "email",
            "username",
            "display_name",
            "role",
            "is_active",
            "mfa_enabled",
            "created_at",
            "updated_at",
            "last_login_at",
        },
        "AppUser",
    )


def test_mfa_challenge_is_202_not_an_error(auth_client) -> None:
    """The gate branches on `mfa_required` in the body, so it must not be a 4xx.

    A 401 here would make the client show "wrong password" to a user whose
    password was correct.
    """
    import time

    from atr.auth.totp import code_at

    enroll = auth_client.post("/api/v1/auth/me/mfa/enroll").json()
    secret = enroll["secret"]
    auth_client.post("/api/v1/auth/me/mfa/activate", json={"code": code_at(secret)})

    # The activation code is spent — `activate_mfa` records its counter so it can
    # never be replayed as a login. The next window's code is the one a user
    # would actually type a few seconds later.
    next_window = code_at(secret, time.time() + 30)

    # A fresh client with no cookie: password only, second factor owed.
    from fastapi.testclient import TestClient

    from atr.api.main import app

    fresh = TestClient(app, client=("127.0.0.1", 51234))
    res = fresh.post(
        "/api/v1/auth/login",
        json={"identifier": "owner", "password": "Str0ngPassw0rd"},
    )
    assert res.status_code == 202, res.text
    _assert_has(res.json(), {"mfa_required"}, "MfaChallenge")
    assert res.json()["mfa_required"] is True

    # And the code completes it, so the branch the gate renders is reachable.
    ok = fresh.post(
        "/api/v1/auth/login",
        json={
            "identifier": "owner",
            "password": "Str0ngPassw0rd",
            "totp_code": next_window,
        },
    )
    assert ok.status_code == 200, ok.text
    _assert_has(ok.json(), {"token", "expires_at", "user"}, "LoginSuccess after MFA")


def test_session_row_matches_SessionRow(auth_client) -> None:
    body = auth_client.get("/api/v1/auth/me/sessions").json()
    _assert_has(body, {"sessions"}, "sessions envelope")
    assert body["sessions"], "the login should have created a session"
    _assert_has(
        body["sessions"][0],
        {
            "session_id",
            "created_at",
            "last_seen_at",
            "expires_at",
            "ip",
            "user_agent",
            "current",
        },
        "SessionRow",
    )


def test_api_key_matches_ApiKeyRow(auth_client) -> None:
    created = auth_client.post(
        "/api/v1/auth/me/api-keys",
        json={"label": "smoke", "scopes": ["watchlist:read"]},
    ).json()
    _assert_has(
        created,
        {"key_id", "label", "prefix", "scopes", "created_at", "expires_at", "key"},
        "CreatedApiKey",
    )
    # The secret is shown once; the list must never carry it again.
    listed = auth_client.get("/api/v1/auth/me/api-keys").json()["keys"]
    _assert_has(
        listed[0],
        {
            "key_id",
            "label",
            "prefix",
            "scopes",
            "created_at",
            "expires_at",
            "last_used_at",
            "revoked",
        },
        "ApiKeyRow",
    )
    assert "key" not in listed[0]


# ---------------------------------------------------- watchlists: api.ts §watchlists
def test_available_columns_matches_AvailableColumns(auth_client) -> None:
    body = auth_client.get("/api/v1/watchlists/columns/available").json()
    _assert_has(body, {"columns", "groups", "available", "unavailable"}, "AvailableColumns")
    _assert_has(
        body["columns"][0],
        {"key", "label", "group", "kind", "available", "requires", "description"},
        "ColumnSpec",
    )
    # The picker renders unavailable columns greyed with their reason. If the
    # server ever stopped flagging them, the UI would offer a column that can
    # only ever be an em dash.
    assert body["unavailable"], "expected at least one not-yet-collectable column"
    by_key = {c["key"]: c for c in body["columns"]}
    for key in body["unavailable"]:
        assert by_key[key]["requires"], f"{key} is unavailable but names no requirement"


def test_watchlist_summary_matches_WatchlistSummary(auth_client) -> None:
    auth_client.post("/api/v1/watchlists", json={"name": "Core"})
    body = auth_client.get("/api/v1/watchlists").json()
    _assert_has(body, {"watchlists"}, "watchlists envelope")
    _assert_has(
        body["watchlists"][0],
        {
            "watchlist_id",
            "user_id",
            "name",
            "exchange",
            "is_default",
            "sort_order",
            "created_at",
            "updated_at",
            "item_count",
            "columns",
        },
        "WatchlistSummary",
    )


def test_watchlist_detail_matches_WatchlistDetail(auth_client) -> None:
    created = auth_client.post("/api/v1/watchlists", json={"name": "Core"}).json()
    _assert_has(created, {"items", "columns"}, "WatchlistDetail")
    assert created["items"] == []
    assert created["columns"], "a new list should arrive with a default column set"


def test_add_items_matches_AddItemsResult(auth_client) -> None:
    created = auth_client.post("/api/v1/watchlists", json={"name": "Core"}).json()
    body = auth_client.post(
        f"/api/v1/watchlists/{created['watchlist_id']}/items",
        json={"symbols": ["RELIANCE", "NOTAREALSYMBOL"]},
    ).json()
    _assert_has(body, {"added", "skipped", "unknown"}, "AddItemsResult")
    assert "RELIANCE" in body["added"]
    # A typo is reported, never silently dropped — otherwise it looks like a
    # successful add and the user wonders why the row never appears.
    assert "NOTAREALSYMBOL" in body["unknown"]


def test_quotes_matches_WatchlistQuotes(auth_client) -> None:
    created = auth_client.post("/api/v1/watchlists", json={"name": "Core"}).json()
    wid = created["watchlist_id"]
    auth_client.post(f"/api/v1/watchlists/{wid}/items", json={"symbols": ["RELIANCE", "TCS"]})

    body = auth_client.get(f"/api/v1/watchlists/{wid}/quotes?live=false").json()
    _assert_has(
        body,
        {
            "watchlist_id",
            "name",
            "exchange",
            "as_of",
            "source",
            "live_symbols",
            "columns",
            "rows",
            "count",
        },
        "WatchlistQuotes",
    )
    assert body["count"] == 2
    assert body["source"] == "local_cache"

    row = body["rows"][0]
    _assert_has(row, {"symbol", "name", "stale"}, "WatchlistQuoteRow")
    # Every configured column must be a key on the row, even when its value is
    # None — the table reads `row[columnKey]` positionally.
    for spec in body["columns"]:
        assert spec["key"] in row, f"row is missing configured column {spec['key']}"
        _assert_has(spec, {"key", "label", "kind"}, "quotes ColumnSpec")


def test_a_symbol_with_no_history_reports_an_error_not_a_zero(auth_client, market_cache) -> None:
    """The row shape for "we have no data" — the case the UI must not fake.

    Deleting the parquet after the master was indexed is exactly what a stale
    index looks like in production, and it is the one path where a lazy `?? 0`
    in the frontend would invent a price.
    """
    (market_cache / "iifl_daily" / "NSEEQ" / "TCS-EQ.parquet").unlink()

    created = auth_client.post("/api/v1/watchlists", json={"name": "Core"}).json()
    wid = created["watchlist_id"]
    auth_client.post(f"/api/v1/watchlists/{wid}/items", json={"symbols": ["TCS"]})

    body = auth_client.get(f"/api/v1/watchlists/{wid}/quotes?live=false").json()
    row = body["rows"][0]
    assert row["symbol"] == "TCS"
    assert "error" in row, "a symbol with no local history must say so"
    # `stale` is deliberately absent here: nothing was priced, so nothing can be
    # described as a stale price. The TS interface marks both optional for this.
    assert row.get("stale") is None


def test_reorder_returns_the_new_order(auth_client) -> None:
    created = auth_client.post("/api/v1/watchlists", json={"name": "Core"}).json()
    wid = created["watchlist_id"]
    auth_client.post(
        f"/api/v1/watchlists/{wid}/items", json={"symbols": ["RELIANCE", "TCS", "INFY"]}
    )
    body = auth_client.put(
        f"/api/v1/watchlists/{wid}/items/order",
        json={"symbols": ["INFY", "RELIANCE", "TCS"]},
    ).json()
    assert body["items"] == ["INFY", "RELIANCE", "TCS"]


def test_set_columns_reports_rejected_keys(auth_client) -> None:
    created = auth_client.post("/api/v1/watchlists", json={"name": "Core"}).json()
    wid = created["watchlist_id"]
    body = auth_client.put(
        f"/api/v1/watchlists/{wid}/columns",
        json={"columns": ["ltp", "change_pct", "no_such_column"]},
    ).json()
    _assert_has(body, {"columns", "rejected_columns"}, "columns response")
    assert body["columns"] == ["ltp", "change_pct"]
    assert body["rejected_columns"] == ["no_such_column"]


def test_not_found_uses_the_documented_code(auth_client) -> None:
    """The client branches on `code`, so the envelope shape is part of the contract."""
    res = auth_client.get("/api/v1/watchlists/does-not-exist")
    assert res.status_code == 404
    detail = res.json()["detail"]
    _assert_has(detail, {"detail", "code"}, "error envelope")
    assert detail["code"] == "not_found"


# ------------------------------------------- instruments: api.ts §instruments
def test_instrument_search_matches_InstrumentRecord(auth_client) -> None:
    body = auth_client.get("/api/v1/instruments/search?q=rel").json()
    _assert_has(body, {"query", "count", "results"}, "instrument search envelope")
    assert body["count"] >= 1
    record = body["results"][0]
    _assert_has(
        record,
        {
            "symbol",
            "exchange",
            "asset_class",
            "series",
            "name",
            "isin",
            "industry",
            "lot_size",
            "tick_size",
            "bars",
            "first_date",
            "last_date",
            "cache_file",
            "timeframes",
            "indices",
        },
        "InstrumentRecord",
    )
    # The suggestion list shows bar count, so it must be a number, not None.
    assert isinstance(record["bars"], int)
    assert isinstance(record["timeframes"], list)


def test_instrument_search_canonicalises_any_spelling(auth_client) -> None:
    """The add-symbol box sends whatever was typed; the server normalises it."""
    for query in ("RELIANCE", "reliance", "RELIANCE-EQ"):
        body = auth_client.get(f"/api/v1/instruments/search?q={query}").json()
        symbols = {r["symbol"] for r in body["results"]}
        assert "RELIANCE" in symbols, f"{query} did not resolve to the canonical symbol"


def test_exchanges_matches_the_sidebar_shape(auth_client) -> None:
    body = auth_client.get("/api/v1/instruments/exchanges").json()
    _assert_has(body, {"exchanges"}, "exchanges envelope")
    _assert_has(body["exchanges"][0], {"exchange", "symbols"}, "exchange row")
