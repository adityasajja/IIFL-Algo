"""Research track A: does rotating between Indian exchange-traded funds beat buy-and-hold?

WHY THIS TRACK EXISTS
---------------------
Exchange-traded funds are the one wrapper in the Indian cash market where a
high-turnover rule is not dead on arrival: securities transaction tax is 0.001%
on the sell only, against 0.1% both sides for a share. A full weekly rotation
costs roughly 6.7%/yr of the account in funds and ~12%/yr in shares. That gap is
the whole reason to look here — if a rotation edge exists anywhere in this market
after costs, this is the wrapper where it shows up.

The question is narrow and falsifiable: over 2009-2026, does *any* of the standard
rotation families — absolute trend, cross-sectional momentum, dual momentum,
inverse-volatility weighting — beat NIFTYBEES buy-and-hold on both CAGR and
Sharpe, in BOTH halves of the window, after realistic costs?

WHAT IS DELIBERATELY NOT DONE
-----------------------------
No parameter is chosen by looking at the second half. Every variant in the grid
is run once over the full window, every one is reported, and the winner is then
shown separately in each half. A rule that only earns in one half is recorded as
a failure, not as a rule.

CASH LEG
--------
LIQUIDBEES prices are constant at 999.99 for every session in the file: it is a
constant-NAV, dividend-paying fund, so its price series carries none of its
return. Using it as the risk-free leg would understate cash by ~6%/yr and flatter
every trend rule that sits in cash. It is therefore excluded, and cash is a
synthetic series compounding at CASH_RATE (6%/yr, roughly the Indian overnight
rate across this window). Moves in and out of cash are charged full ETF costs,
which is conservative — a liquid fund switch is cheaper than a market order.

SURVIVORSHIP
------------
The universe is funds that exist today. Delisted or merged funds are absent, so
levels here are mildly optimistic. All comparisons are like-for-like inside the
same universe, which is what the bias permits.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from atr.research.hunt import (
    ETF_COSTS,
    TRADING_DAYS,
    Result,
    buy_and_hold,
    compare,
    deflated_sharpe,
    etf_universe,
    load_panel,
    run_weights,
    split_halves,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research" / "etf_rotation.json"

CASH = "CASH"
#: Annual cash yield. This is an ASSUMPTION, not a measured series — LIQUIDBEES,
#: the only cash instrument in the data, is pinned at 999.99 for all 17 years
#: because it pays its return in extra units. 6% is roughly the Indian
#: overnight/liquid-fund rate across the window. The whole grid is re-run at 0%
#: as a sensitivity, because every rotate-to-cash rule is levered to this number.
CASH_RATE = 0.06
CASH_RATE_SENSITIVITY = 0.0

#: Six funds with 2009+ history, plus Hang Seng from 2010. The longest window.
SLEEVE_CORE = ["NIFTYBEES", "JUNIORBEES", "BANKBEES", "GOLDBEES", "PSUBNKBEES"]
#: Adds the three 2011+ funds: mid-cap, Nasdaq 100 and Hang Seng. Shorter, wider.
SLEEVE_WIDE = SLEEVE_CORE + ["HNGSNGBEES", "MOM100", "MON100"]

REBALANCE = {"W": "W-FRI", "M": "ME", "Q": "QE"}


# ---------------------------------------------------------------------------
# Panel plumbing
# ---------------------------------------------------------------------------
def build_panel(symbols: list[str], cash_rate: float = CASH_RATE) -> pd.DataFrame:
    """Close prices for ``symbols`` plus a synthetic cash column, over their common window."""
    raw = load_panel(etf_universe(), etf=True)
    panel = raw[symbols].dropna()
    days = (panel.index - panel.index[0]).days.to_numpy()
    panel = panel.copy()
    panel[CASH] = (1 + cash_rate) ** (days / 365.25)
    return panel


def rebalance_dates(index: pd.DatetimeIndex, freq: str) -> pd.DatetimeIndex:
    """Last trading session of each period — the day a signal is computed.

    The panel's first session is always included. Without it a quarterly rule
    would sit in cash for its first quarter while a weekly rule would not, and
    the cadence comparison would measure that head start rather than the cadence.
    """
    marks = pd.Series(index, index=index).resample(REBALANCE[freq]).last().dropna()
    dates = pd.DatetimeIndex(marks.to_numpy()).union(pd.DatetimeIndex([index[0]]))
    return dates.sort_values()


def slice_result(result: Result, start: pd.Timestamp, end: pd.Timestamp) -> Result:
    """The same run restricted to a date span, so halves keep the full warm-up.

    Re-running a rule on half the price history would throw away the lookback
    window at the start of each half and change the rule being measured. Instead
    the rule runs once over everything and its realised returns are cut.
    """
    mask = (result.returns.index >= start) & (result.returns.index <= end)
    net = result.returns[mask]
    return Result(
        equity=(1 + net).cumprod(),
        returns=net,
        turnover=result.turnover[mask],
        costs=result.costs,
        gross_returns=result.gross_returns[mask],
    )


# ---------------------------------------------------------------------------
# Signal families
# ---------------------------------------------------------------------------
def w_trend(panel: pd.DataFrame, assets: list[str], window: int, freq: str) -> pd.DataFrame:
    """Absolute momentum: equal weight in every fund above its own N-day average, rest cash."""
    prices = panel[assets]
    above = prices > prices.rolling(window).mean()
    dates = rebalance_dates(panel.index, freq)
    signal = above.reindex(dates).fillna(False)
    n = len(assets)
    weights = pd.DataFrame(0.0, index=dates, columns=panel.columns)
    weights[assets] = signal.astype(float) / n
    weights[CASH] = 1.0 - weights[assets].sum(axis=1)
    return weights


def w_cross(
    panel: pd.DataFrame,
    assets: list[str],
    lookback: int,
    k: int,
    freq: str,
    *,
    absolute: bool = False,
) -> pd.DataFrame:
    """Hold the top ``k`` funds by trailing return; with ``absolute``, only those also beating cash."""
    prices = panel[assets]
    trail = prices / prices.shift(lookback) - 1
    cash_trail = panel[CASH] / panel[CASH].shift(lookback) - 1
    dates = rebalance_dates(panel.index, freq)
    trail = trail.reindex(dates)
    cash_trail = cash_trail.reindex(dates)

    weights = pd.DataFrame(0.0, index=dates, columns=panel.columns)
    ranks = trail.rank(axis=1, ascending=False, method="first")
    picked = (ranks <= k) & trail.notna()
    if absolute:
        picked &= trail.gt(cash_trail, axis=0)
    weights[assets] = picked.astype(float) / k
    weights[CASH] = 1.0 - weights[assets].sum(axis=1)
    return weights


def w_invvol(panel: pd.DataFrame, assets: list[str], window: int, freq: str) -> pd.DataFrame:
    """Inverse-volatility weights across the whole sleeve, always fully invested."""
    vol = panel[assets].pct_change().rolling(window).std()
    dates = rebalance_dates(panel.index, freq)
    inv = (1.0 / vol.reindex(dates)).replace([np.inf, -np.inf], np.nan)
    norm = inv.div(inv.sum(axis=1), axis=0).fillna(0.0)
    weights = pd.DataFrame(0.0, index=dates, columns=panel.columns)
    weights[assets] = norm
    weights[CASH] = 1.0 - weights[assets].sum(axis=1)
    return weights


def w_hold(panel: pd.DataFrame, assets: list[str], freq: str) -> pd.DataFrame:
    """Equal-weight the sleeve and rebalance on schedule. The passive control."""
    dates = rebalance_dates(panel.index, freq)
    weights = pd.DataFrame(0.0, index=dates, columns=panel.columns)
    weights[assets] = 1.0 / len(assets)
    return weights


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------
def variants() -> list[dict]:
    """Every rule that will be run. Declared up front so the trial count is honest."""
    grid: list[dict] = []
    for sleeve_name, assets in (("core5", SLEEVE_CORE), ("wide8", SLEEVE_WIDE)):
        for freq in ("W", "M", "Q"):
            # 1. absolute trend
            for window in (50, 100, 200):
                grid.append(
                    {
                        "family": "trend",
                        "name": f"trend{window}_{sleeve_name}_{freq}",
                        "sleeve": sleeve_name,
                        "assets": assets,
                        "freq": freq,
                        "fn": lambda p, a, w=window, f=freq: w_trend(p, a, w, f),
                    }
                )
            # 2/3. cross-sectional and dual momentum
            for lookback in (21, 63, 126, 252):
                for k in (1, 2, 3):
                    for absolute in (False, True):
                        tag = "dual" if absolute else "xsec"
                        grid.append(
                            {
                                "family": tag,
                                "name": f"{tag}{lookback}k{k}_{sleeve_name}_{freq}",
                                "sleeve": sleeve_name,
                                "assets": assets,
                                "freq": freq,
                                "fn": lambda p, a, lb=lookback, kk=k, f=freq, ab=absolute: w_cross(
                                    p, a, lb, kk, f, absolute=ab
                                ),
                            }
                        )
            # 4. inverse volatility
            for window in (63, 126):
                grid.append(
                    {
                        "family": "invvol",
                        "name": f"invvol{window}_{sleeve_name}_{freq}",
                        "sleeve": sleeve_name,
                        "assets": assets,
                        "freq": freq,
                        "fn": lambda p, a, w=window, f=freq: w_invvol(p, a, w, f),
                    }
                )
            # 5. passive control: equal weight, rebalanced only
            grid.append(
                {
                    "family": "equal",
                    "name": f"equal_{sleeve_name}_{freq}",
                    "sleeve": sleeve_name,
                    "assets": assets,
                    "freq": freq,
                    "fn": lambda p, a, f=freq: w_hold(p, a, f),
                }
            )
    return grid


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def attribution(panel: pd.DataFrame, weights: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Where a rule's gross return actually came from, asset by asset.

    One asset in a lucky regime (Nasdaq 100 since 2011) can carry a whole
    rotation. This decomposes gross return into per-asset contributions so that
    can be seen rather than assumed away.
    """
    daily = panel.pct_change().fillna(0.0)
    held = weights.reindex(panel.index).ffill().fillna(0.0).reindex(columns=panel.columns, fill_value=0.0)
    traded = held.shift(1).fillna(0.0)
    contrib = (traded * daily).sum()
    total = float(contrib.sum())
    return {
        "avg_weight_pct": {c: round(100 * float(traded[c].mean()), 1) for c in panel.columns},
        "share_of_gross_return_pct": {
            c: (round(100 * float(contrib[c]) / total, 1) if total else 0.0) for c in panel.columns
        },
    }


def run_grid(cash_rate: float) -> tuple[list[dict], dict, dict, dict, dict]:
    """Run every variant once at a given cash yield.

    Returns the per-variant rows plus the panels, benchmarks and split dates, so
    the caller can re-run the whole grid at a different cash assumption and
    compare like with like.
    """
    panels = {
        "core5": build_panel(SLEEVE_CORE, cash_rate),
        "wide8": build_panel(SLEEVE_WIDE, cash_rate),
    }

    # Two benchmarks, both on each panel's own window so comparisons are date-for-date.
    # NIFTYBEES is the mandated one. The equal-weight sleeve is the one that
    # matters: it isolates *rotation* from plain diversification. A rotation rule
    # that beats the index but not a static equal weight of the same funds has
    # discovered gold and mid-caps, not timing.
    benches = {name: buy_and_hold(p, "NIFTYBEES", ETF_COSTS) for name, p in panels.items()}
    equals = {
        name: run_weights(p, w_hold(p, SLEEVE_CORE if name == "core5" else SLEEVE_WIDE, "Q"), ETF_COSTS)
        for name, p in panels.items()
    }
    splits = {}
    for name, panel in panels.items():
        early, late = split_halves(panel)
        splits[name] = (early.index[0], early.index[-1], late.index[0], late.index[-1])

    rows = []
    grid = variants()
    for spec in grid:
        panel = panels[spec["sleeve"]]
        weights = spec["fn"](panel, spec["assets"])
        result = run_weights(panel, weights, ETF_COSTS)
        e0, e1, l0, l1 = splits[spec["sleeve"]]
        bench = benches[spec["sleeve"]]

        full = result.metrics()
        early = slice_result(result, e0, e1).metrics()
        late = slice_result(result, l0, l1).metrics()
        b_early = slice_result(bench, e0, e1).metrics()
        b_late = slice_result(bench, l0, l1).metrics()
        eq = equals[spec["sleeve"]]
        q_early = slice_result(eq, e0, e1).metrics()
        q_late = slice_result(eq, l0, l1).metrics()

        def wins(s_e: dict, s_l: dict, r_e: dict = early, r_l: dict = late) -> bool:
            return (
                r_e["cagr_pct"] > s_e["cagr_pct"]
                and r_e["sharpe"] > s_e["sharpe"]
                and r_l["cagr_pct"] > s_l["cagr_pct"]
                and r_l["sharpe"] > s_l["sharpe"]
            )

        rows.append(
            {
                "name": spec["name"],
                "family": spec["family"],
                "sleeve": spec["sleeve"],
                "freq": spec["freq"],
                "full": full,
                "early": early,
                "late": late,
                "bench_early": b_early,
                "bench_late": b_late,
                "equal_early": q_early,
                "equal_late": q_late,
                "beats_both_halves": bool(wins(b_early, b_late)),
                "beats_equal_both_halves": bool(wins(q_early, q_late)),
                "compare": compare(spec["name"], result, bench),
                "compare_vs_equal": compare(spec["name"] + " vs equal-weight sleeve", result, eq),
                "attribution": attribution(panel, weights),
            }
        )
    return rows, panels, benches, equals, splits


def main() -> None:
    rows, panels, benches, equals, splits = run_grid(CASH_RATE)
    for name, panel in panels.items():
        print(f"{name}: {panel.index[0].date()} -> {panel.index[-1].date()}  {len(panel)} sessions")

    n_trials = len(rows)
    survivors = [r for r in rows if r["beats_both_halves"]]
    hard = [r for r in survivors if r["beats_equal_both_halves"] and r["family"] != "equal"]
    ranked = sorted(rows, key=lambda r: r["full"]["sharpe"], reverse=True)
    best = ranked[0]
    n_obs = int(best["full"]["years"] * TRADING_DAYS)

    # Cost/frequency frontier: what changing only the rebalance cadence costs.
    frontier = [
        {
            "name": r["name"],
            "freq": r["freq"],
            "turnover_per_yr": r["full"]["turnover_per_yr"],
            "cost_drag_pct_yr": r["full"]["cost_drag_pct_yr"],
            "gross_cagr_pct": r["full"]["gross_cagr_pct"],
            "cagr_pct": r["full"]["cagr_pct"],
            "sharpe": r["full"]["sharpe"],
        }
        for r in rows
    ]

    # Sensitivity: every rotate-to-cash rule is levered to the cash assumption,
    # so the whole grid is re-run with cash earning nothing.
    zero_rows, _, _, _, _ = run_grid(CASH_RATE_SENSITIVITY)
    zero_by_name = {r["name"]: r for r in zero_rows}
    sensitivity = [
        {
            "name": r["name"],
            "cagr_at_6pct_cash": r["full"]["cagr_pct"],
            "cagr_at_0pct_cash": zero_by_name[r["name"]]["full"]["cagr_pct"],
            "sharpe_at_6pct_cash": r["full"]["sharpe"],
            "sharpe_at_0pct_cash": zero_by_name[r["name"]]["full"]["sharpe"],
            "beats_equal_both_halves_at_0pct": zero_by_name[r["name"]]["beats_equal_both_halves"],
        }
        for r in ranked[:20]
    ]
    hard_zero = [
        r["name"] for r in zero_rows if r["beats_equal_both_halves"] and r["family"] != "equal"
    ]

    # Robustness: a real edge is a plateau, not a spike. For anything that cleared
    # both hurdles in both halves, list the Sharpe of every variant differing in
    # exactly one parameter. If the neighbours collapse, the survivor is luck.
    by_name = {r["name"]: r for r in rows}

    def neighbours(name: str) -> dict[str, float]:
        body, sleeve, freq = name.rsplit("_", 2)
        out = {}
        for other in by_name:
            o_body, o_sleeve, o_freq = other.rsplit("_", 2)
            differs = (o_body != body) + (o_sleeve != sleeve) + (o_freq != freq)
            if differs == 1:
                out[other] = by_name[other]["full"]["sharpe"]
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    neighbourhood = {r["name"]: neighbours(r["name"]) for r in hard}

    payload = {
        "meta": {
            "track": "A: exchange-traded fund rotation",
            "n_variants": n_trials,
            "cash_leg": (
                f"ASSUMPTION: synthetic {CASH_RATE:.1%}/yr constant drift, not a measured series. "
                "LIQUIDBEES is pinned at 999.99 for all 4369 sessions (constant-NAV, pays in units) "
                "so its price carries zero of its return and it cannot be used."
            ),
            "cash_sensitivity_rate": CASH_RATE_SENSITIVITY,
            "costs": ETF_COSTS.name,
            "round_trip_pct": round(ETF_COSTS.round_trip_pct(), 4),
            "survivorship": "funds listed today only; mildly optimistic levels",
            "windows": {k: [str(v[0].date()), str(v[3].date())] for k, v in splits.items()},
            "split_dates": {k: [str(d.date()) for d in v] for k, v in splits.items()},
        },
        "best_by_full_sharpe": {
            "name": best["name"],
            "sharpe": best["full"]["sharpe"],
            "deflated_sharpe": round(deflated_sharpe(best["full"]["sharpe"], n_trials, n_obs), 3),
            "detail": best,
        },
        "n_beating_nifty_both_halves": len(survivors),
        "survivors_vs_nifty": [r["name"] for r in survivors],
        "n_beating_equal_weight_sleeve_full_sample": sum(
            1 for r in rows if r["compare_vs_equal"]["beats_benchmark"]
        ),
        "n_beating_equal_weight_sleeve_both_halves": len(hard),
        "survivors_vs_equal_weight_sleeve": [r["name"] for r in hard],
        "survivor_neighbourhood_sharpe": neighbourhood,
        "frequency_frontier": frontier,
        "cash_rate_sensitivity_top20": sensitivity,
        "survivors_vs_equal_weight_sleeve_at_0pct_cash": hard_zero,
        "all_variants": ranked,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\n{n_trials} variants; {len(survivors)} beat NIFTYBEES in BOTH halves")
    print(f"benchmark full: {benches['core5'].metrics()}")
    head = "{:<28} {:>7} {:>7} {:>8} {:>7} {:>7} {:>6} {:>6}"
    print(head.format("variant", "cagr", "sharpe", "maxdd", "turn", "drag", "shE", "shL"))
    for r in ranked[:20]:
        print(
            head.format(
                r["name"],
                r["full"]["cagr_pct"],
                r["full"]["sharpe"],
                r["full"]["max_drawdown_pct"],
                r["full"]["turnover_per_yr"],
                r["full"]["cost_drag_pct_yr"],
                r["early"]["sharpe"],
                r["late"]["sharpe"],
            )
        )
    print("\nbeat NIFTYBEES both halves:", [s["name"] for s in survivors] or "NONE")
    print("\nALSO beat a static equal-weight sleeve both halves:", [s["name"] for s in hard] or "NONE")

    for name, nbrs in neighbourhood.items():
        own = by_name[name]["full"]["sharpe"]
        print(f"\nneighbourhood of {name} (own sharpe {own}):")
        for other, sharpe in nbrs.items():
            print(f"    {other:<24} {sharpe}")

    print("\ncash-rate sensitivity (top 20 by Sharpe):")
    for s in sensitivity:
        print(
            f"  {s['name']:<24} cagr {s['cagr_at_6pct_cash']:>6} -> {s['cagr_at_0pct_cash']:>6}"
            f"   sharpe {s['sharpe_at_6pct_cash']:>6} -> {s['sharpe_at_0pct_cash']:>6}"
        )
    print("beat equal-weight both halves with cash at 0%:", hard_zero or "NONE")

    print("\nattribution of the top 5 (share of gross return):")
    for r in ranked[:5]:
        share = {k: v for k, v in r["attribution"]["share_of_gross_return_pct"].items() if abs(v) >= 1}
        print(f"  {r['name']:<24} {share}")

    print("\nfrequency frontier (equal-weight sleeve, cadence only):")
    for r in rows:
        if r["family"] == "equal":
            print(
                f"  {r['name']:<20} turn/yr {r['full']['turnover_per_yr']:>5} "
                f"drag {r['full']['cost_drag_pct_yr']:>5}%  cagr {r['full']['cagr_pct']:>6}%"
            )
    print("\nfrequency frontier (trend50, cadence only):")
    for r in rows:
        if r["name"].startswith("trend50_"):
            print(
                f"  {r['name']:<20} turn/yr {r['full']['turnover_per_yr']:>5} "
                f"drag {r['full']['cost_drag_pct_yr']:>5}%  cagr {r['full']['cagr_pct']:>6}%"
                f"  sharpe {r['full']['sharpe']}"
            )


if __name__ == "__main__":
    main()
