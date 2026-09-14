"""Drive the exact request sequence the React app makes, against a live server.

``tests/test_api_contract.py`` pins the JSON shapes through Starlette's in-process
TestClient. This does the same over a real socket, which additionally covers the
things a TestClient cannot: the ASGI server, the middleware stack in its real
order, the built SPA being served, and the ``Origin`` header a browser actually
sends on a same-origin POST — the header that exposed a CSRF rule which only
worked on four hardcoded ports.

Start a server on a store nobody cares about, then point this at it:

    PYTHONPATH=src APP_DB_URL="sqlite:///.smoke/run.db" ATR_SECRET_KEY=smoke-key \\
      ENV=dev ALLOW_SIGNUP=false .venv/Scripts/python.exe -m uvicorn \\
      atr.api.main:app --host 127.0.0.1 --port 8899

    PYTHONPATH=src .venv/Scripts/python.exe scripts/smoke_api.py http://127.0.0.1:8899

It expects an empty store (it asserts the first-run state), so use a fresh
database name per run rather than deleting one.

Exit code is 0 only if every check passed.
"""

from __future__ import annotations

import json
import re
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8899"
ORIGIN = BASE  # exactly what a browser puts in `Origin` for a same-origin POST

failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=60.0, follow_redirects=True)

    # ── the server is up, and serving the SPA ────────────────────────────────
    for _ in range(60):
        try:
            if client.get("/health").status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(1)
    else:
        print("server never became reachable")
        return 2

    print("\n[Served SPA]")
    index = client.get("/")
    check("GET / returns 200", index.status_code == 200)
    check("GET / is the built dashboard", '<div id="root">' in index.text)

    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.text)
    check("the page references built assets", len(assets) >= 2, str(assets))
    check(
        "every referenced asset is actually served",
        all(client.get(a).status_code == 200 for a in assets),
        str([a for a in assets if client.get(a).status_code != 200]),
    )

    print("\n[First-run state]")
    boot = client.get("/api/v1/auth/bootstrap").json()
    check("needs_setup is true on an empty store", boot["needs_setup"] is True)
    check("auth is required", boot["auth_required"] is True)

    print("\n[Anonymous access is refused]")
    anon = client.get("/api/v1/auth/me")
    check("GET /auth/me is 401 with no session", anon.status_code == 401, str(anon.status_code))

    print("\n[Bootstrap the owner]")
    created = client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "aditya@example.com",
            "username": "aditya",
            "password": "Str0ngPassw0rd",
            "display_name": "Aditya",
        },
        headers={"Origin": ORIGIN},
    )
    check("POST /auth/bootstrap is 201", created.status_code == 201, created.text[:200])
    check("a session cookie was set", "atr_session" in client.cookies)

    me = client.get("/api/v1/auth/me").json()
    check("the first account is the owner", me["role"] == "owner", me.get("role", "?"))
    check("owner may write watchlists", "watchlist:write" in me["permissions"])
    check("owner may change the execution mode", "execution_mode:change" in me["permissions"])
    check("auth_method is session", me["auth_method"] == "session")
    check("mfa_satisfied is true without MFA", me["mfa_satisfied"] is True)

    print("\n[Password login, as a returning user]")
    login = client.post(
        "/api/v1/auth/login",
        json={"identifier": "aditya", "password": "Str0ngPassw0rd"},
        headers={"Origin": ORIGIN},
    )
    check("POST /auth/login is 200", login.status_code == 200, login.text[:200])
    check("it returns a token for scripts", bool(login.json().get("token")))
    sessions = client.get("/api/v1/auth/me/sessions").json()["sessions"]
    check("the login created a second session", len(sessions) >= 2, str(len(sessions)))
    check("exactly one session is marked current", sum(s["current"] for s in sessions) == 1)
    wrong = client.post(
        "/api/v1/auth/login",
        json={"identifier": "aditya", "password": "definitely-wrong"},
        headers={"Origin": ORIGIN},
    )
    check("a wrong password is 401", wrong.status_code == 401, str(wrong.status_code))
    check("with a stable code the UI can branch on", "code" in wrong.json().get("detail", {}))

    print("\n[Cross-origin writes are still refused]")
    evil = client.post(
        "/api/v1/watchlists", json={"name": "Evil"}, headers={"Origin": "http://evil.example"}
    )
    check("a foreign Origin is 403", evil.status_code == 403, str(evil.status_code))
    check("with the documented code", evil.json().get("code") == "csrf_origin_rejected")

    print("\n[Column registry]")
    cols = client.get("/api/v1/watchlists/columns/available").json()
    check("columns are listed", len(cols["columns"]) > 0)
    check("groups are derived", len(cols["groups"]) > 0)
    check("unavailable columns are declared", len(cols["unavailable"]) > 0)
    check(
        "each unavailable column names what it needs",
        all(
            c["requires"]
            for c in cols["columns"]
            if c["key"] in cols["unavailable"]
        ),
    )

    print("\n[Watchlists — the panel's whole lifecycle]")
    empty = client.get("/api/v1/watchlists").json()
    check("a new account has no lists", empty["watchlists"] == [])

    made = client.post(
        "/api/v1/watchlists",
        json={"name": "Core", "exchange": "NSEEQ"},
        headers={"Origin": ORIGIN},
    )
    check("POST /watchlists is 201", made.status_code == 201, made.text[:200])
    wl = made.json()
    wid = wl["watchlist_id"]
    check("the first list becomes the default", wl["is_default"] is True)
    check("it arrives with a default column set", len(wl["columns"]) > 0)

    dup = client.post(
        "/api/v1/watchlists", json={"name": "Core"}, headers={"Origin": ORIGIN}
    )
    check("a duplicate name is 409", dup.status_code == 409, str(dup.status_code))
    check("with a duplicate_name code", dup.json()["detail"]["code"] == "duplicate_name")

    print("\n[Instrument search — the add-symbol box]")
    found = client.get("/api/v1/instruments/search?q=reliance").json()
    check("search returns results", found["count"] >= 1)
    check(
        "any spelling resolves to the canonical symbol",
        "RELIANCE" in {r["symbol"] for r in found["results"]},
    )
    check("results carry a bar count", isinstance(found["results"][0]["bars"], int))

    print("\n[Adding symbols]")
    added = client.post(
        f"/api/v1/watchlists/{wid}/items",
        json={"symbols": ["RELIANCE", "TCS", "INFY", "NOSUCHSYMBOL"]},
        headers={"Origin": ORIGIN},
    ).json()
    check("known symbols are added", set(added["added"]) >= {"RELIANCE", "TCS", "INFY"})
    check("a typo is reported, not dropped", "NOSUCHSYMBOL" in added["unknown"])

    again = client.post(
        f"/api/v1/watchlists/{wid}/items",
        json={"symbols": ["RELIANCE"]},
        headers={"Origin": ORIGIN},
    ).json()
    check("a re-add is reported as skipped", again["skipped"] == ["RELIANCE"])

    print("\n[Quotes — live overlay with a cache fallback]")
    q = client.get(f"/api/v1/watchlists/{wid}/quotes?live=true").json()
    check("quotes come back", q["count"] == 3, str(q["count"]))
    check("the source says where prices came from", q["source"] in ("broker+cache", "local_cache"))
    row = q["rows"][0]
    check("a row names its symbol", row["symbol"] in ("RELIANCE", "TCS", "INFY"))
    check("a row carries the staleness flag", "stale" in row)
    check(
        "every configured column is a key on the row",
        all(c["key"] in row for c in q["columns"]),
    )
    check("prices resolved from the real cache", row.get("ltp") is not None, json.dumps(row)[:200])

    print("\n[Reorder]")
    order = ["INFY", "RELIANCE", "TCS"]
    reordered = client.put(
        f"/api/v1/watchlists/{wid}/items/order",
        json={"symbols": order},
        headers={"Origin": ORIGIN},
    ).json()
    check("the new order is returned", reordered["items"] == order, str(reordered["items"]))

    print("\n[Columns]")
    setc = client.put(
        f"/api/v1/watchlists/{wid}/columns",
        json={"columns": ["ltp", "change_pct", "volume_ratio", "nonsense"]},
        headers={"Origin": ORIGIN},
    ).json()
    check("known columns are kept", setc["columns"] == ["ltp", "change_pct", "volume_ratio"])
    check("unknown columns are reported", setc["rejected_columns"] == ["nonsense"])

    after = client.get(f"/api/v1/watchlists/{wid}/quotes?live=false").json()
    check("the new columns are computed", "volume_ratio" in after["rows"][0])
    check("live=false reports the cache as the source", after["source"] == "local_cache")

    print("\n[Removal and ownership]")
    removed = client.delete(f"/api/v1/watchlists/{wid}/items/TCS", headers={"Origin": ORIGIN})
    check("a symbol can be removed", removed.status_code == 200, str(removed.status_code))
    check(
        "the list shrank",
        client.get(f"/api/v1/watchlists/{wid}").json()["items"] == ["INFY", "RELIANCE"],
    )
    missing = client.get("/api/v1/watchlists/nope")
    check("an unknown list is 404, not 403", missing.status_code == 404)

    print("\n[Audit trail]")
    trail = client.get("/api/v1/audit/events?limit=50").json()
    check("the queryable trail is paged", {"events", "total", "limit", "offset"} <= set(trail))
    actions = {e["action"] for e in trail["events"]}
    check("the audit recorded the bootstrap", "auth.bootstrap" in actions, str(sorted(actions)))
    check("and the password login", "auth.login" in actions, str(sorted(actions)))
    # A failed attempt keeps the same action name and differs only by `result` —
    # which is exactly why the UI has to render `result`, not just the action.
    check(
        "and the failed attempt, distinguishable by result",
        any(
            e["action"] == "auth.login" and e["result"] != "success"
            for e in trail["events"]
        ),
        str([(e["action"], e["result"]) for e in trail["events"]]),
    )
    check("and the watchlist writes", any(a.startswith("watchlist.") for a in actions))
    if trail["events"]:
        check(
            "events carry the actor denormalised",
            all("actor" in e for e in trail["events"]),
        )
        check("events carry a result", all(e["result"] for e in trail["events"]))
    filtered = client.get("/api/v1/audit/events?action=watchlist.create").json()
    check(
        "the trail can be filtered by action",
        all(e["action"] == "watchlist.create" for e in filtered["events"]),
    )
    known = client.get("/api/v1/audit/actions").json()
    check("distinct actions are listed", "watchlist.create" in known["actions"])

    # The JSONL sink is a separate, restart-proof file. It is read by the legacy
    # route, which must keep working.
    legacy = client.get("/audit?limit=50").json()
    check("the legacy JSONL sink still exists", "audit.jsonl" in legacy["path"])
    check("and still returns entries", len(legacy["entries"]) > 0)

    print("\n[Sign out]")
    out = client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
    check("logout succeeds", out.status_code == 200, str(out.status_code))
    check("the session is gone", client.get("/api/v1/auth/me").status_code == 401)

    print("\n[The legacy dashboard routes are untouched]")
    check("GET /health needs no auth", client.get("/health").status_code == 200)
    check("GET /strategies needs no auth", client.get("/strategies").status_code == 200)
    check("GET /risk/status needs no auth", client.get("/risk/status").status_code == 200)

    client.close()
    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
