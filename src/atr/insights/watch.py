"""What is worth knowing about each holding. Descriptions and risk control, never forecasts."""

from __future__ import annotations

from typing import Any

import pandas as pd

from atr.insights.evidence import LAGGING_NOTE, PROTECT_NOTE, QUIET_NOTE
from atr.insights.stall import stall_frame

PROTECT_AT_GAIN_PCT = 15.0  # start talking about protecting a gain once it is this big
TRAIL_BELOW_HIGH_PCT = 8.0  # ...with a stop this far under the 20-day high
LAGGING_RS_PCT = -15.0  # this far behind the market over 20 days


def watch_holding(
    holding: dict[str, Any],
    df: pd.DataFrame | None,
    *,
    rs_20d: float | None = None,
) -> dict[str, Any]:
    """One holding's row: price, gain, and any flags with their honest notes."""
    symbol = holding["symbol"]
    avg = float(holding.get("avg_price") or 0)
    row: dict[str, Any] = {
        "symbol": symbol.replace("-EQ", ""),
        "qty": holding.get("qty"),
        "avg_price": avg or None,
        "last": None,
        "pnl_pct": None,
        "as_of": None,
        "flags": [],
    }
    if df is None or len(df) < 30:
        return row

    df = df.sort_values("ts").reset_index(drop=True)
    last = float(df["close"].iloc[-1])
    row["last"] = round(last, 2)
    row["as_of"] = pd.Timestamp(df["ts"].iloc[-1]).strftime("%Y-%m-%d")
    pnl = ((last / avg) - 1) * 100 if avg > 0 else None
    row["pnl_pct"] = round(pnl, 1) if pnl is not None else None

    high20 = float(df["high"].tail(20).max())

    # A big gain: name a level that keeps most of it, and say when price has slipped under it.
    if pnl is not None and pnl >= PROTECT_AT_GAIN_PCT:
        level = round(high20 * (1 - TRAIL_BELOW_HIGH_PCT / 100), 2)
        kept = ((level / avg) - 1) * 100
        if last < level:
            row["flags"].append(
                {
                    "kind": "slipped",
                    "title": "Slipped below its protective level",
                    "detail": f"Up {pnl:.0f}% overall, but now under {level:,.2f} ({TRAIL_BELOW_HIGH_PCT:.0f}% below its 20-day high).",
                    "level": level,
                    "note": PROTECT_NOTE,
                }
            )
        else:
            row["flags"].append(
                {
                    "kind": "protect",
                    "title": "Big gain to protect",
                    "detail": f"Up {pnl:.0f}%. A stop at {level:,.2f} would keep about {kept:.0f}% of it.",
                    "level": level,
                    "note": PROTECT_NOTE,
                }
            )

    f = stall_frame(df).iloc[-1]
    quiet = abs(f["ret5"]) <= 2.0 and f["range_ratio"] <= 0.7 and f["off_high"] >= -3.5
    if bool(quiet):
        row["flags"].append(
            {
                "kind": "quiet",
                "title": "Gone quiet near its high",
                "detail": f"Barely moved for 5 days ({abs(f['ret5']):.1f}%), within {abs(f['off_high']):.1f}% of its 20-day high.",
                "level": None,
                "note": QUIET_NOTE,
            }
        )

    if rs_20d is not None and rs_20d <= LAGGING_RS_PCT and (pnl is None or pnl < 0):
        row["flags"].append(
            {
                "kind": "lagging",
                "title": "Lagging the market",
                "detail": f"{abs(rs_20d):.0f}% behind the Nifty over 20 days.",
                "level": None,
                "note": LAGGING_NOTE,
            }
        )
    return row
