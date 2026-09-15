"""Manual end-to-end verification of the screener against the REAL NSE cache.

This is not a unit test. The acceptance criterion is "manually verify a real NSE
scan", and a synthetic fixture cannot discharge that: it would prove the code
runs, not that it works on the operator's actual 3,000-file parquet cache with
its real gaps, real symbol spellings, and real price levels.

Run:  ./.venv/Scripts/python.exe scripts/verify_screener_scan.py
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("ENV", "dev")
os.environ.setdefault("AUTH_REQUIRED", "true")
os.environ.setdefault("ATR_SECRET_KEY", "verify-key")
os.environ["APP_DB_URL"] = "sqlite:///" + tempfile.mkdtemp().replace("\\", "/") + "/verify.db"

from fastapi.testclient import TestClient  # noqa: E402

from atr.api.main import app  # noqa: E402


def main() -> int:
    client = TestClient(app, client=("127.0.0.1", 51234))
    boot = client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "verify@example.com",
            "username": "verify",
            "password": "Str0ngPassw0rd",
            "display_name": "Verify",
        },
    )
    print(f"bootstrap: {boot.status_code}")
    client.headers.update({"Authorization": f"Bearer {boot.json()['token']}"})

    print("\n=== universes (scannable, i.e. backed by local bars) ===")
    for entry in client.get("/api/v1/screener/universes").json()["universes"]:
        print(f"  {entry['name']:<12}{entry['size']:>6}  {entry['label']}")

    print("\n=== indicator catalog ===")
    cat = client.get("/api/v1/screener/indicators").json()
    print(f"  available:   {len(cat['available'])}")
    print(f"  unavailable: {len(cat['unavailable'])} -> {', '.join(cat['unavailable'])}")

    print("\n=== nested screen over ALL NSE ===")
    print("  (close > EMA50 AND RSI14 < 70 AND rel_volume > 1.2x)")
    print("  OR (gap > 1% AND close > previous day high)")
    tree = {
        "match": "any",
        "conditions": [
            {
                "match": "all",
                "conditions": [
                    {"indicator": "close", "op": ">", "rhs_indicator": "ema", "period": 50},
                    {"indicator": "rsi", "op": "<", "value": 70, "period": 14},
                    {"indicator": "rel_volume", "op": ">", "value": 1.2, "period": 20},
                ],
            },
            {
                "match": "all",
                "conditions": [
                    {"indicator": "gap_pct", "op": ">", "value": 1.0},
                    {"indicator": "close", "op": ">", "rhs_indicator": "prev_high"},
                ],
            },
        ],
    }

    response = client.post(
        "/api/v1/screener/run",
        json={
            "universe": "all",
            "exchange": "NSEEQ",
            "conditions": tree,
            "sort": "rel_volume",
            "limit": 8,
        },
    )
    print(f"  status: {response.status_code}")
    if response.status_code != 200:
        print("  body:", response.text[:600])
        return 1

    data = response.json()
    print(
        f"  scanned={data['scanned']}  matched={data['matched']}  "
        f"as_of={data['as_of']}  elapsed={data['elapsed_s']}s"
    )
    print(f"  summary: {data['conditions']['summary']}")
    if data["warnings"]:
        print(f"  warnings: {data['warnings']}")
    if data["errors"]:
        print(f"  errors: {data['errors'][:3]}")

    print("\n=== top rows, with the reason each matched ===")
    for row in data["rows"][:4]:
        print(
            f"\n  {row['symbol']:<12} LTP={row['ltp']:<10} chg={row['change_pct']}%  "
            f"relvol={row['rel_volume']}x  RSI={round(row['rsi14'], 1)}  "
            f"EMA20={round(row['ema20'], 1)}  EMA50={round(row['ema50'], 1)}  "
            f"ATR={round(row['atr_pct'], 2)}%"
        )
        for item in row["why"]:
            print(f"      OK  {item['reason']}")

    print("\n=== save / reload / re-run ===")
    saved = client.post(
        "/api/v1/screener/saved",
        json={
            "name": "verify-ema-rsl-vol",
            "definition": {"universe": "all", "exchange": "NSEEQ", "conditions": tree},
        },
    )
    print(f"  save: {saved.status_code}")
    scan_id = saved.json()["scan_id"]
    rerun = client.get(f"/api/v1/screener/saved/{scan_id}/results")
    print(f"  re-run by id: {rerun.status_code}  matched={rerun.json()['matched']}")
    print(f"  delete: {client.delete(f'/api/v1/screener/saved/{scan_id}').status_code}")

    print("\n=== error handling ===")
    bad = client.post(
        "/api/v1/screener/run",
        json={"universe": "all", "conditions": {"indicator": "rsl", "op": ">", "value": 1}},
    )
    print(f"  typo'd indicator: {bad.status_code} {bad.json()['detail']['code']}")
    badop = client.post(
        "/api/v1/screener/run",
        json={"universe": "all", "conditions": {"indicator": "rsi", "op": "~=", "value": 1}},
    )
    print(f"  typo'd operator:  {badop.status_code} {badop.json()['detail']['code']}")
    unavail = client.post(
        "/api/v1/screener/run",
        json={"universe": "all", "conditions": {"indicator": "market_cap", "op": ">", "value": 1}},
    )
    print(f"  unavailable:      {unavail.status_code} {unavail.json()['detail']['code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
