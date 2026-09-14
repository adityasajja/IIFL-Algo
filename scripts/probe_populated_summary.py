"""Exercise the populated dashboard path end-to-end.

The live account has 0 open positions and 0 closed trades, so every capture so
far has shown only empty states -- the sparkline, win-rate and drawdown render
paths had never executed. This monkeypatches the two data sources
(_authed_client -> positions/trades) with a realistic book, then calls the real
endpoint function, so the actual production code builds the payload. Only the
inputs are synthetic; every line that formats the response is the same line
that runs in production.

Usage:  ./.venv/Scripts/python.exe scripts/probe_populated_summary.py
"""
import sys
import json

sys.path.insert(0, "src")

from atr.api import main as M  # noqa: E402

DAYS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
        "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]

# 36 closed trades, newest-first, 2 wins per 3, ~4 per session.
trades = []
for i in range(36):
    day = DAYS[(i // 4) % len(DAYS)]
    pnl = 820.0 if i % 3 != 2 else -430.0
    trades.append({
        "realized_pnl": pnl,
        "ts": f"{day}T09:32:00+05:30",
        "symbol": f"SYM{i % 6}",
        "qty": 50,
    })

# NOTE ON PRICES: ltp must be near the real cached close for the symbol, or
# the cache-resolved Day P&L is nonsense. An invented ltp against a real close
# produced +52.99% "in one day" during testing -- which is exactly the failure
# mode to watch for if the broker's ltp and the cache ever diverge.
positions = [
    {"tradingSymbol": "RELIANCE", "netQuantity": 40, "ltp": 1263.2,
     "averagePrice": 1270.0, "realizedPnl": 2860.0,
     "last_flat_at": "2026-09-11T15:29:00+05:30"},
    {"tradingSymbol": "HDFCBANK", "netQuantity": 60, "ltp": 703.4,
     "averagePrice": 712.0, "realizedPnl": -1662.0},
    # A squared-off row: IIFL still returns it, with qty 0. It must not be
    # counted as a position -- IiflBroker.positions() skips these.
    {"tradingSymbol": "INFY", "netQuantity": 0, "ltp": 1037.7,
     "averagePrice": 1040.0},
    {"tradingSymbol": "TCS", "netQuantity": 25, "ltp": 2208.5,
     "averagePrice": 2190.0},
]


def fake_client():
    class C:
        def positions(self):
            return positions

        def trades(self):
            return trades

        def limits(self):
            return {"margin_available": 250000.0}

    return C()


M._authed_client = fake_client
M._SUMMARY_CACHE = {"data": None, "at": 0.0, "exchange": ""}

out = M.dashboard_summary(exchange="NSEEQ")
print(json.dumps(out, indent=2, default=str))
