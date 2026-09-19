"""Is there a way to pick the Monday entries so the +2% plan makes money on average?

The plain plan wins about 63% of the time yet loses on average, because the misses cost
more than the hits earn and 0.33% of costs eats a sixth of every 2% win. This searches
conditions known before Monday's open (prior-week move, trend, RSI, how jumpy, the gap at
the open, the market's own prior week) on the EARLY half, keeps the best by average net
result, and grades them on the LATE half they never saw.

Run:  ./.venv/Scripts/python.exe scripts/study_intraweek_search.py
"""

from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd
from study_intraweek_target import ROUND_TRIP, simulate, stock_universe, weeks_for

MIN_TRAIN = 1500
TOP = 10
STOP = 0.05


def main() -> None:
    weeks = pd.concat([w for w in (weeks_for(s) for s in stock_universe(min_bars=1500)) if w is not None], ignore_index=True)
    weeks["mkt_prior_week"] = weeks.groupby("date")["ret5"].transform("median")  # what the market just did
    res = simulate(weeks, STOP)
    net = (res["ret"] - ROUND_TRIP).to_numpy()
    won = (res["reason"] == "target").to_numpy()
    mid = weeks["date"].sort_values().iloc[len(weeks) // 2]
    early = (weeks["date"] < mid).to_numpy()
    late = ~early
    q = lambda col, p: float(weeks[col].quantile(p))  # noqa: E731  (thresholds from the early half only would be stricter; these are coarse)

    flags = {
        "fell_last_week": weeks["ret5"] < -0.03, "rose_last_week": weeks["ret5"] > 0.03,
        "down_month": weeks["ret20"] < -0.10, "up_month": weeks["ret20"] > 0.10,
        "rsi_oversold": weeks["rsi"] < 30, "rsi_overbought": weeks["rsi"] > 70, "rsi_mid": weeks["rsi"].between(45, 60),
        "jumpy": weeks["vol20"] >= q("vol20", 0.8), "calm": weeks["vol20"] <= q("vol20", 0.2),
        "above_ema50": weeks["dist_ema50"] > 0, "below_ema50": weeks["dist_ema50"] < -0.05,
        "near_52w_high": weeks["dist_52w_high"] > -0.05, "far_from_high": weeks["dist_52w_high"] < -0.30,
        "gapped_up_monday": weeks["gap_open"] > 0.01, "gapped_down_monday": weeks["gap_open"] < -0.01,
        "market_fell_last_week": weeks["mkt_prior_week"] < -0.01, "market_rose_last_week": weeks["mkt_prior_week"] > 0.01,
    }
    names = list(flags)
    matrix = np.column_stack([flags[n].to_numpy() for n in names])
    print(f"{len(weeks):,} stock-weeks. Plan: buy Monday open, sell at +2%, stop -{int(STOP * 100)}%, else Friday close.")
    print(f"everything: win {100 * won.mean():.1f}%   avg net {100 * net.mean():+.2f}%   (early {100 * net[early].mean():+.2f}%, late {100 * net[late].mean():+.2f}%)\n")

    tried = []
    for size in (1, 2, 3):
        for combo in combinations(range(len(names)), size):
            mask = matrix[:, combo].all(axis=1)
            n = int((mask & early).sum())
            if n >= MIN_TRAIN:
                tried.append((float(net[mask & early].mean()), combo, mask))
    tried.sort(key=lambda t: -t[0])
    print(f"{len(tried)} conditions with at least {MIN_TRAIN:,} early-half trades. Best by early-half average, graded on the late half:\n")
    print(f"{'condition (all must hold)':<62}{'early avg':>10}{'late avg':>10}{'late n':>9}{'late win':>9}")
    for avg, combo, mask in tried[:TOP]:
        lm = mask & late
        print(f"{' + '.join(names[i] for i in combo):<62}{100 * avg:>+9.2f}%{100 * net[lm].mean():>+9.2f}%{int(lm.sum()):>9,}{100 * won[lm].mean():>8.1f}%")

    rng = np.random.default_rng(3)
    best_noise = []
    for _ in range(5):
        shuffled = rng.permutation(net[early])
        best_noise.append(max(float(shuffled[m[early]].mean()) for _, _, m in tried))
    print(f"\nbest early-half average the SAME search finds on shuffled outcomes (luck alone): {100 * np.mean(best_noise):+.2f}%")
    positive = [(a, c, m) for a, c, m in tried[:TOP] if net[m & late].mean() > 0]
    print(f"of the top {TOP} chosen on the early half, {len(positive)} are also positive on the late half")


if __name__ == "__main__":
    main()
