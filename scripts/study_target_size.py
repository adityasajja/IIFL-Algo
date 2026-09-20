"""How big a profit target is worth aiming for?

A bigger target earns more per win but is reached less often, and a stop that is too
tight is hit by ordinary noise. This measures the whole grid on the one rule that had
an edge: buy a stock that gaps down over 1% at Monday's open, sell on a touch of the
target, stop below, else Friday's close. Same-day exits are priced as day-trades and
carried ones as delivery, both after 5bp of slippage a side.

Run (from scripts/):  ../.venv/Scripts/python study_target_size.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from study_intraday_cost import NOTIONAL, intraday_costs
from study_intraweek_target import simulate, stock_universe, weeks_for

from atr.research.hunt import STOCK_COSTS

TARGETS = (0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.06)
STOPS = (0.03, 0.05, 0.08, None)


def net_returns(res: pd.DataFrame) -> np.ndarray:
    same_day = ((res["reason"] == "target") & (res["day"] == 0)).to_numpy()
    cost = np.where(same_day, intraday_costs(0.0005).round_trip_pct(NOTIONAL) / 100, STOCK_COSTS.round_trip_pct() / 100)
    return res["ret"].to_numpy() - cost


def main() -> None:
    weeks = pd.concat([w for w in (weeks_for(s) for s in stock_universe(min_bars=1500)) if w is not None], ignore_index=True)
    weeks["mkt"] = weeks.groupby("date")["ret5"].transform("median")
    mid = weeks["date"].sort_values().iloc[len(weeks) // 2]
    early = (weeks["date"] < mid).to_numpy()
    rules = {
        "gap-down >1%, market rose >1% last week": ((weeks["gap_open"] < -0.01) & (weeks["mkt"] > 0.01)).to_numpy(),
        "gap-down >1%, any market": (weeks["gap_open"] < -0.01).to_numpy(),
    }
    for name, m in rules.items():
        print(f"\n{name}   ({int(m.sum()):,} trades)")
        print(f"{'target':>7}{'stop':>7}{'wins (touch target)':>21}{'avg net/trade':>15}{'early half':>12}{'late half':>11}{'avg win':>9}{'avg loss':>10}")
        for t in TARGETS:
            for stop in STOPS:
                res = simulate(weeks, stop, t)
                net = net_returns(res)
                won = (res["reason"] == "target").to_numpy()
                sel = m
                label = "none" if stop is None else f"{int(stop * 100)}%"
                w = net[sel & won]
                l = net[sel & ~won]
                print(f"{int(t * 1000) / 10:>6}%{label:>7}{100 * won[sel].mean():>20.1f}%{100 * net[sel].mean():>+14.2f}%"
                      f"{100 * net[sel & early].mean():>+11.2f}%{100 * net[sel & ~early].mean():>+10.2f}%"
                      f"{100 * w.mean():>+8.2f}%{100 * l.mean() if len(l) else 0:>+9.2f}%")


if __name__ == "__main__":
    main()
