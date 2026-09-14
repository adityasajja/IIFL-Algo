"""Dump the raw position payloads so the prior-close question can be answered
from evidence rather than assumption.

Run with a live session (`.cache/iifl_session.json` must be current):
    ./.venv/Scripts/python.exe scripts/inspect_position_payload.py
"""
import contextlib
import json
import sys

sys.path.insert(0, "src")

from atr.api import main as M  # noqa: E402


def main() -> int:
    client = M._authed_client()
    try:
        payload = client.positions()
    finally:
        with contextlib.suppress(Exception):
            client.close()

    rows = M._broker_rows(payload)
    print(f"raw rows: {len(rows)}")
    if rows:
        # Show every key on the first row -- the whole point is to find out
        # whether a prior-close field exists under a name we did not guess.
        print("keys on row 0:", sorted(rows[0].keys()))
        print(json.dumps(rows[0], indent=2, default=str)[:1600])

    print("\n--- any key containing prev/close/open/yest ---")
    for row in rows[:3]:
        hits = {k: v for k, v in row.items()
                if any(t in k.lower() for t in ("prev", "close", "yest", "open"))}
        print(f"  {row.get('tradingSymbol') or row.get('symbol')}: {hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
