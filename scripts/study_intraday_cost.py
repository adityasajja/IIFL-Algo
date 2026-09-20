"""Price the +2% plan honestly: cheap when it closes the same day, dear when it does not.

Delivery costs 0.33% a round trip (0.1% STT on each side plus slippage). A trade that is
bought and sold the same day is a day-trade: STT only on the sell at 0.025%, brokerage
capped at Rs 20 a side, so the round trip falls to roughly 0.13%. The plan below buys as
delivery and, if the stock touches +2% on Monday itself, sells it as a day-trade. Anything
that has not hit by the close is carried and pays the full delivery cost when it exits.

The same-day fill assumes the sell limit at +2% is executed when Monday's high reaches it
and the stop was not hit first. Slippage is varied because it now dominates the cost.

Run (from scripts/):  ../.venv/Scripts/python study_intraday_cost.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from study_intraweek_target import ROUND_TRIP as DELIVERY_RT
from study_intraweek_target import simulate, stock_universe, weeks_for

from atr.research.hunt import STOCK_COSTS, Costs

STOP = 0.05
NOTIONAL = 100_000.0  # a Rs 1 lakh ticket: what the Rs 20 brokerage cap means in percent


def intraday_costs(slippage: float) -> Costs:
    """Day-trade schedule: STT 0.025% on the sell only, stamp 0.003% on the buy."""
    return Costs(name="NSE day-trade share", stt_buy=0.0, stt_sell=0.00025, stamp_buy=0.00003, slippage=slippage)


def main() -> None:
    weeks = pd.concat([w for w in (weeks_for(s) for s in stock_universe(min_bars=1500)) if w is not None], ignore_index=True)
    weeks["mkt_prior_week"] = weeks.groupby("date")["ret5"].transform("median")
    res = simulate(weeks, STOP)
    mid = weeks["date"].sort_values().iloc[len(weeks) // 2]
    early = (weeks["date"] < mid).to_numpy()
    same_day_win = ((res["reason"] == "target") & (res["day"] == 0)).to_numpy()

    print(f"Delivery round trip {100 * DELIVERY_RT:.3f}%   |   day-trade round trip at 5bp slippage "
          f"{intraday_costs(0.0005).round_trip_pct(NOTIONAL):.3f}%, at 10bp {intraday_costs(0.001).round_trip_pct(NOTIONAL):.3f}%\n")

    gap = weeks["gap_open"]
    rules = {
        "every stock, every Monday": pd.Series(True, index=weeks.index),
        "gapped down >1% Monday": gap < -0.01,
        "gapped down >2% Monday": gap < -0.02,
        "gapped down >1% + market rose last wk": (gap < -0.01) & (weeks["mkt_prior_week"] > 0.01),
        "gapped down >1% + above 50-day avg": (gap < -0.01) & (weeks["dist_ema50"] > 0),
    }
    print(f"{'rule (buy Monday open, sell at +2%, stop -5%)':<46}{'trades':>8}{'same-day':>10}{'wins':>7}"
          f"{'delivery avg':>14}{'hybrid avg (5bp)':>18}{'(10bp)':>9}{'late half (5bp)':>17}")
    for name, cond in rules.items():
        m = cond.to_numpy()
        n = int(m.sum())
        if n < 500:
            continue
        gross = res["ret"].to_numpy()
        row = [f"{name:<46}{n:>8,}{100 * same_day_win[m].mean():>9.1f}%{100 * (res['reason'] == 'target').to_numpy()[m].mean():>6.0f}%"]
        row.append(f"{100 * (gross[m] - DELIVERY_RT).mean():>+13.2f}%")
        results = {}
        for slip in (0.0005, 0.001):
            cheap = intraday_costs(slip).round_trip_pct(NOTIONAL) / 100
            dear = (STOCK_COSTS.round_trip_pct() / 100) + (slip - 0.0005) * 2  # delivery schedule at the same slippage
            cost = np.where(same_day_win, cheap, dear)
            results[slip] = gross - cost
        row.append(f"{100 * results[0.0005][m].mean():>+17.2f}%")
        row.append(f"{100 * results[0.001][m].mean():>+8.2f}%")
        late = m & ~early
        row.append(f"{100 * results[0.0005][late].mean():>+16.2f}%")
        print("".join(row))

    print("\nSame-day share of the gap-down winners, by half:")
    m = (gap < -0.01).to_numpy()
    won = (res["reason"] == "target").to_numpy()
    for tag, half in (("early", early), ("late", ~early)):
        sel = m & half
        print(f"   {tag}: {100 * same_day_win[sel].sum() / max(won[sel].sum(), 1):.0f}% of the winners hit on Monday itself")


if __name__ == "__main__":
    main()
