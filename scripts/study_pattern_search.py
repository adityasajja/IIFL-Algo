"""Can any combination of chart patterns reach a 60% chance of a +2% week?

Search every single pattern, pair and triple (plus a "volatile stock" condition) on
the EARLY half of the history only, keep whichever combinations scored highest,
then grade those same rules on the LATE half, which the search never saw.

The result to watch is the gap between the two. A rule that scores 55% in the half
it was chosen from and 33% in the half it was not is a description of the past, not a
signal. With hundreds of combinations tried, some will always look excellent by
chance; the late half is what tells the two apart.

Run:  ./.venv/Scripts/python.exe scripts/study_pattern_search.py
"""

from __future__ import annotations

import json
from itertools import combinations

import numpy as np
import pandas as pd

from atr.research.hunt import STOCKS
from study_chart_patterns import HIT, ROUND_TRIP, build

OUT = STOCKS.parents[2] / "research" / "pattern_search.json"
MIN_TRAIN = 300  # a rule seen fewer times than this in the early half is not ranked
TOP = 12


def main() -> None:
    stacked, columns, mid = build()
    # "Jumpy stock" is itself a condition worth combining: it is the strongest single
    # predictor of a big week, so the search should be free to use it.
    columns = dict(columns)
    columns["volatile_top_quintile"] = pd.Series(stacked["vol_bucket"].to_numpy() == stacked["vol_bucket"].max(), index=stacked.index)

    early = (stacked["date"] < mid).to_numpy()
    late = ~early
    hit = stacked["hit"].to_numpy()
    fwd = stacked["fwd"].to_numpy()
    names = list(columns)
    matrix = np.column_stack([columns[n].to_numpy() for n in names])

    base_early, base_late = float(hit[early].mean()), float(hit[late].mean())
    print(f"stock-weeks {len(stacked):,}   base rate: early {100 * base_early:.1f}%  late {100 * base_late:.1f}%")

    tried = []
    for size in (1, 2, 3):
        for combo in combinations(range(len(names)), size):
            mask = matrix[:, combo].all(axis=1)
            train = mask & early
            n = int(train.sum())
            if n < MIN_TRAIN:
                continue
            tried.append((float(hit[train].mean()), n, combo, mask))

    tried.sort(key=lambda t: -t[0])
    print(f"{len(tried):,} combinations had at least {MIN_TRAIN} early-half occurrences\n")
    print(f"{'rule (all must be true)':<70}{'early P':>8}{'n':>7}{'| late P':>9}{'n':>7}{'net%/wk':>9}")

    rows = []
    for p_train, n_train, combo, mask in tried[:TOP]:
        test = mask & late
        n_test = int(test.sum())
        p_test = float(hit[test].mean()) if n_test else float("nan")
        net = float(fwd[test].mean() - ROUND_TRIP) * 100 if n_test else float("nan")
        label = " AND ".join(names[i] for i in combo)
        rows.append(
            {"rule": label, "early_p_hit": round(100 * p_train, 2), "early_n": n_train,
             "late_p_hit": round(100 * p_test, 2), "late_n": n_test, "late_net_pct_per_week": round(net, 3)}
        )
        print(f"{label[:69]:<70}{100 * p_train:>8.1f}{n_train:>7}{'| ' + format(100 * p_test, '.1f'):>9}{n_test:>7}{net:>9.3f}")

    # How much of the early-half score is luck? Shuffle the outcomes and rerun the
    # identical search: the best combination it finds is what pure noise achieves.
    rng = np.random.default_rng(7)
    noise_best = []
    for _ in range(5):
        shuffled = rng.permutation(hit[early])
        best = 0.0
        for _, n_train, combo, mask in tried:
            sel = mask[early]
            best = max(best, float(shuffled[sel].mean()))
        noise_best.append(best)
    print(f"\nbest early-half score the SAME search finds on shuffled (meaningless) outcomes: "
          f"{100 * np.mean(noise_best):.1f}%   <- what luck alone achieves")

    top_late = [r["late_p_hit"] for r in rows if r["late_n"] >= 100]
    best_late = max((r for r in rows if r["late_n"] >= 100), key=lambda r: r["late_p_hit"], default=None)
    over60 = [r for r in rows if r["late_n"] >= 100 and r["late_p_hit"] >= 60]
    print(f"\nrules chosen on the early half that reach 60% on the late half: {len(over60)} of {len(rows)}")
    if best_late:
        print(f"best late-half result among the {len(rows)} chosen rules: {best_late['late_p_hit']}%  ({best_late['rule']})")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "target": f"P(next week >= {int(HIT * 100)}%) of 60% or more",
        "base_rate_early_pct": round(100 * base_early, 2),
        "base_rate_late_pct": round(100 * base_late, 2),
        "combinations_searched": len(tried),
        "best_early_score_on_shuffled_outcomes_pct": round(100 * float(np.mean(noise_best)), 2),
        "chosen_on_early_half_graded_on_late_half": rows,
        "reaching_60_on_late_half": len(over60),
        "median_late_p_hit_of_chosen": round(float(np.median(top_late)), 2) if top_late else None,
        "survivorship_note": "universe is today's long-history names; failed companies are absent",
    }, indent=1), encoding="utf8")


if __name__ == "__main__":
    main()
