"""Research track D: does a regime / defensive overlay make the index steadier?

The question is narrow. Owning NIFTYBEES and doing nothing earns ~13% a year with
a -36% drawdown and a losing week 44% of the time. This script asks whether a
slow, cheap, long-only overlay -- a moving-average trend filter, a breadth
filter, or volatility targeting -- raises the Sharpe *and* cuts the drawdown,
after real costs, in both halves of the sample.

Everything that decides whether a result is real (costs, metrics, the lag, the
split) comes from ``atr.research.hunt``; this file owns only the signals.

Two data facts shape the design:

* **LIQUIDBEES is not a usable cash proxy.** Its close is pinned at 999.99 for
  the whole 2009-2026 history (it is a constant-NAV fund that pays its yield as
  fresh units), so a price backtest would credit cash with exactly 0% return.
  We therefore synthesise a ``CASH`` column compounding at a constant
  ``CASH_YIELD`` and say so in every result.
* **The exchange-traded fund universe is mildly survivorship-biased** -- these
  are the funds that still exist and still trade. For NIFTYBEES/GOLDBEES that
  bias is small, but it is not zero.

Run:  .venv/Scripts/python scripts/hunt_regime_overlay.py
Out:  data/research/regime_overlay.json
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
    load_panel,
    run_weights,
    split_halves,
    stock_universe,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research" / "regime_overlay.json"

RISK = "NIFTYBEES"
GOLD = "GOLDBEES"
CASH = "CASH"

#: Annual yield credited to the synthetic cash leg. Indian overnight/liquid-fund
#: money has paid roughly this over the sample; LIQUIDBEES cannot be used
#: because its quoted price never moves.
CASH_YIELD = 0.06

#: Windows we report separately, because a trend filter earns its keep in a
#: crash and pays for itself in a chop.
CRISES = {
    "2015_16_drawdown": ("2015-03-01", "2016-02-29"),
    "2018_19_chop": ("2018-08-01", "2019-12-31"),
    "2020_covid": ("2020-01-15", "2020-06-30"),
    "2022_drawdown": ("2022-01-01", "2022-07-31"),
}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------
def build_panel(cash_yield: float = CASH_YIELD) -> pd.DataFrame:
    """NIFTYBEES + GOLDBEES + a synthetic constant-drift cash column.

    ``cash_yield`` is an *assumption*, not a measured series: LIQUIDBEES cannot
    supply one. Results are reported at 6% and again at 0% so the sensitivity of
    every conclusion to this number is visible.
    """
    panel = load_panel([RISK, GOLD], etf=True, min_bars=2000).dropna()
    daily = (1 + cash_yield) ** (1 / TRADING_DAYS) - 1
    panel[CASH] = 100.0 * (1 + daily) ** np.arange(len(panel))
    return panel


def breadth_series(index: pd.DatetimeIndex, window: int) -> pd.Series:
    """Fraction of the long-history share universe above its own ``window``-day mean.

    Survivorship-biased by construction (these are today's names), so the level
    of the breadth reading is optimistic; only its time variation is used.
    """
    stocks = load_panel(stock_universe(min_bars=1500), min_bars=1500)
    above = stocks > stocks.rolling(window, min_periods=window).mean()
    valid = stocks.rolling(window, min_periods=window).mean().notna()
    frac = above.where(valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)
    return frac.reindex(index).ffill()


# ---------------------------------------------------------------------------
# Signals -> weights
# ---------------------------------------------------------------------------
def weights_from_exposure(
    panel: pd.DataFrame, exposure: pd.Series, safe: str, *, deadband: float = 0.0
) -> pd.DataFrame:
    """Hold ``exposure`` of NIFTYBEES and the rest in ``safe``.

    The harness treats each row of the weight frame as a *rebalance event* and
    lets the book drift in between, so rows are emitted only when the target
    actually moves by more than ``deadband``. For a binary filter that is the
    handful of crossing dates a year; for volatility targeting the deadband is
    what stops a continuously-drifting target from trading every session and
    burning the edge in costs.

    ``exposure`` is indexed by the session whose close produced it; the harness
    fills a target dated ``d`` on ``d+1``, so nothing trades on information it
    did not have.
    """
    exposure = exposure.reindex(panel.index).fillna(0.0).clip(0.0, 1.0)
    values = exposure.to_numpy()
    keep = [0]
    last = values[0]
    for i in range(1, len(values)):
        if abs(values[i] - last) > max(deadband, 1e-9):
            keep.append(i)
            last = values[i]
    dates = panel.index[keep]
    weights = pd.DataFrame(0.0, index=dates, columns=panel.columns)
    weights[RISK] = values[keep]
    if safe != "none":
        weights[safe] = 1.0 - values[keep]
    return weights


def trend_exposure(price: pd.Series, window: int, buffer_pct: float = 0.0) -> pd.Series:
    """1 when price is above its ``window``-day mean (by ``buffer_pct``), else 0.

    With a buffer the rule is hysteretic: it needs price above (1+b)*MA to switch
    on and below (1-b)*MA to switch off, which is the cheapest known whipsaw cure.
    """
    ma = price.rolling(window, min_periods=window).mean()
    if buffer_pct <= 0:
        return (price > ma).astype(float)
    up, down = price > ma * (1 + buffer_pct), price < ma * (1 - buffer_pct)
    state, out = 0.0, []
    for u, d in zip(up.to_numpy(), down.to_numpy(), strict=True):
        if u:
            state = 1.0
        elif d:
            state = 0.0
        out.append(state)
    return pd.Series(out, index=price.index)


def vol_target_exposure(price: pd.Series, window: int, target: float) -> pd.Series:
    """Scale exposure to hit ``target`` annual vol, capped at 1.0 (no leverage)."""
    realised = price.pct_change().rolling(window, min_periods=window).std() * np.sqrt(TRADING_DAYS)
    return (target / realised).clip(0.0, 1.0).fillna(0.0)


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------
def excess_sharpe(m: dict) -> float:
    """Sharpe measured *above cash*, which is the only fair yardstick here.

    The harness reports ``sharpe`` as CAGR / volatility with no risk-free
    subtraction. That is fine for two fully-invested rules, but an overlay that
    parks money in cash earns the cash yield at zero volatility, which inflates
    that ratio mechanically -- a 50/50 equity/cash book would score above a 100%
    equity book with no skill at all. Subtracting the cash yield removes the
    artefact and is the number the conclusions rest on.
    """
    vol = m.get("ann_vol_pct", 0.0)
    return round((m["cagr_pct"] - 100 * CASH_YIELD) / vol, 3) if vol > 0 else 0.0


def flips_per_year(exposure: pd.Series) -> float:
    """How often a binary filter changes its mind, per year."""
    on = (exposure > 0.5).astype(int)
    years = len(on) / TRADING_DAYS
    return round(float(on.diff().abs().sum()) / years, 2) if years else 0.0


def false_exit_cost(price: pd.Series, exposure: pd.Series) -> dict[str, float]:
    """What the index did while the filter was out of it.

    This is the whipsaw bill stated directly: if the index compounded strongly
    on the days the filter sat in the safe asset, the filter was not avoiding
    losses, it was missing gains. ``index_cagr_while_out_pct`` is the index's
    annualised return over just those sessions.
    """
    ret = price.pct_change().shift(-1).reindex(exposure.index)  # the day the weight applies
    out_mask = exposure.reindex(price.index).fillna(0.0) < 0.5
    out = ret[out_mask].dropna()
    inn = ret[~out_mask].dropna()
    if out.empty:
        return {"days_out_pct": 0.0}

    def ann(x: pd.Series) -> float:
        return round(100 * (float((1 + x).prod()) ** (TRADING_DAYS / len(x)) - 1), 2)

    return {
        "days_out_pct": round(100 * float(out_mask.mean()), 1),
        "index_cagr_while_out_pct": ann(out),
        "index_cagr_while_in_pct": ann(inn) if len(inn) > 20 else None,
        "up_days_while_out_pct": round(100 * float((out > 0).mean()), 1),
    }


def window_stats(res: Result, start: str, end: str) -> dict[str, float]:
    r = res.returns.loc[start:end].dropna()
    if r.empty:
        return {}
    curve = (1 + r).cumprod()
    return {
        "return_pct": round(100 * float(curve.iloc[-1] - 1), 2),
        "max_drawdown_pct": round(100 * float((curve / curve.cummax() - 1).min()), 2),
    }


def slice_weights(weights: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Restrict a sparse rebalance frame to ``index``, carrying the opening book.

    The frame only carries rows on rebalance dates, so a naive slice would start
    the sub-period flat until the next signal change. The last target on or
    before the window start is re-dated to the start so the sub-period opens
    holding what the rule actually held.
    """
    prior = weights.loc[: index[0]]
    inside = weights.loc[index[0] : index[-1]]
    if prior.empty:
        return inside
    opening = prior.iloc[[-1]].copy()
    opening.index = pd.DatetimeIndex([index[0]])
    return pd.concat([opening, inside[inside.index > index[0]]])


def half_metrics(panel: pd.DataFrame, weights: pd.DataFrame) -> dict[str, dict]:
    """Same weights, measured separately on each half of the sample.

    The signal is computed once on the full history (it is backward-looking, so
    this leaks nothing); only the *measurement* window is split, which keeps the
    moving-average warm-up out of the second half's results.
    """
    early, late = split_halves(panel)
    out = {}
    for name, part in (("first_half", early), ("second_half", late)):
        res = run_weights(part, slice_weights(weights, part.index), ETF_COSTS)
        bench = buy_and_hold(part, RISK, ETF_COSTS)
        m, b = res.metrics(), bench.metrics()
        out[name] = {
            "span": f"{part.index[0].date()} to {part.index[-1].date()}",
            "cagr_pct": m["cagr_pct"],
            "sharpe": m["sharpe"],
            "excess_sharpe": excess_sharpe(m),
            "max_drawdown_pct": m["max_drawdown_pct"],
            "benchmark_cagr_pct": b["cagr_pct"],
            "benchmark_sharpe": b["sharpe"],
            "benchmark_excess_sharpe": excess_sharpe(b),
            "excess_sharpe_gap": round(excess_sharpe(m) - excess_sharpe(b), 3),
        }
    return out


def evaluate(
    name: str, panel: pd.DataFrame, exposure: pd.Series, safe: str, *, deadband: float = 0.0
) -> tuple[dict, Result, pd.DataFrame]:
    weights = weights_from_exposure(panel, exposure, safe, deadband=deadband)
    res = run_weights(panel, weights, ETF_COSTS)
    m = res.metrics()
    row = {
        "name": name,
        "safe_asset": safe,
        "cagr_pct": m["cagr_pct"],
        "sharpe": m["sharpe"],
        "excess_sharpe": excess_sharpe(m),
        "max_drawdown_pct": m["max_drawdown_pct"],
        "ann_vol_pct": m["ann_vol_pct"],
        "weekly_win_rate_pct": m["weekly_win_rate_pct"],
        "turnover_per_yr": m["turnover_per_yr"],
        "cost_drag_pct_yr": m["cost_drag_pct_yr"],
        "flips_per_yr": flips_per_year(exposure),
        "rebalances_per_yr": round(len(weights) / max(m["years"], 1e-9), 1),
        "deadband": deadband,
        "avg_exposure_pct": round(100 * float(exposure.mean()), 1),
        "years": m["years"],
        "crises": {k: window_stats(res, a, b) for k, (a, b) in CRISES.items()},
        "false_exit": false_exit_cost(panel[RISK], exposure),
    }
    return row, res, weights


def static_blend(panel: pd.DataFrame, equity_w: float, other: str, freq: str = "QE") -> Result:
    """A fixed equity/``other`` mix, rebalanced on ``freq``.

    This is the control the overlays actually have to beat. An overlay that sits
    in gold 30% of the time and beats the index has not necessarily timed
    anything -- gold compounded faster than the index over this sample at
    roughly zero correlation, so *any* standing allocation to it would have
    helped. Only the gap between the overlay and this blend is timing skill.
    """
    # Actual trading sessions that end each period -- a calendar quarter-end is
    # usually not a session, so resampling the index directly loses the dates.
    marks = pd.Series(panel.index, index=panel.index).resample(freq).last().dropna()
    dates = pd.DatetimeIndex([panel.index[0], *marks.to_list()]).unique()
    weights = pd.DataFrame(0.0, index=dates, columns=panel.columns)
    weights[RISK] = equity_w
    weights[other] = 1.0 - equity_w
    return run_weights(panel, weights, ETF_COSTS), weights


def control_blends(panel: pd.DataFrame) -> dict:
    """Static mixes at the same average equity exposure the overlays ran."""
    out = {}
    for other in (GOLD, CASH):
        for equity_w in (0.5, 0.6, 0.7, 0.8):
            res, weights = static_blend(panel, equity_w, other)
            m = res.metrics()
            name = f"static_{int(equity_w * 100)}nifty_{int(round((1 - equity_w) * 100))}{other.lower()}"
            out[name] = {
                "cagr_pct": m["cagr_pct"],
                "sharpe": m["sharpe"],
                "excess_sharpe": excess_sharpe(m),
                "max_drawdown_pct": m["max_drawdown_pct"],
                "weekly_win_rate_pct": m["weekly_win_rate_pct"],
                "turnover_per_yr": m["turnover_per_yr"],
                "cost_drag_pct_yr": m["cost_drag_pct_yr"],
                "crises": {k: window_stats(res, a, b) for k, (a, b) in CRISES.items()},
                "eras": {k: window_stats(res, a, b) for k, (a, b) in GOLD_ERAS.items()},
                "halves": half_metrics(panel, weights),
            }
    return out


def cash_sensitivity(rows: list[dict], keep: dict) -> dict:
    """Every cash-legged variant re-measured with cash earning 0% instead of 6%.

    The 6% figure is an assumption; this shows how much of each result rests on
    it. Gold-legged and fully-invested variants are unaffected by construction.
    """
    zero = build_panel(cash_yield=0.0)
    out = {}
    for row in rows:
        if row["safe_asset"] != CASH:
            continue
        _, _, exposure = keep[row["name"]]
        res = run_weights(
            zero, weights_from_exposure(zero, exposure, CASH, deadband=row["deadband"]), ETF_COSTS
        )
        m = res.metrics()
        out[row["name"]] = {
            "cash_6pct": {"cagr_pct": row["cagr_pct"], "sharpe": row["sharpe"]},
            "cash_0pct": {"cagr_pct": m["cagr_pct"], "sharpe": m["sharpe"]},
            "cagr_cost_of_zero_cash_pct": round(m["cagr_pct"] - row["cagr_pct"], 2),
        }
    return out


#: Gold's own history splits into two strong runs around a long flat stretch.
GOLD_ERAS = {
    "2009_2012_gold_bull": ("2009-01-01", "2012-12-31"),
    "2013_2018_gold_flat": ("2013-01-01", "2018-12-31"),
    "2019_2026_gold_bull": ("2019-01-01", "2026-12-31"),
}


def gold_diagnostics(panel: pd.DataFrame, keep: dict, variants: list[str]) -> dict:
    """Is the gold leg a persistent property or a few lucky eras?

    Reports gold's and the index's own compounding inside each era, their
    correlation, and how the gold-legged overlay did against the cash-legged one
    in each era. If the overlay only works in the two gold bull runs, it is a
    bet on gold, not a regime filter.
    """
    ret = panel.pct_change().dropna()
    eras = {}
    for name, (a, b) in GOLD_ERAS.items():
        r = ret.loc[a:b]
        if r.empty:
            continue
        years = len(r) / TRADING_DAYS
        eras[name] = {
            "years": round(years, 2),
            "gold_cagr_pct": round(100 * (float((1 + r[GOLD]).prod()) ** (1 / years) - 1), 2),
            "nifty_cagr_pct": round(100 * (float((1 + r[RISK]).prod()) ** (1 / years) - 1), 2),
            "corr_gold_nifty": round(float(r[GOLD].corr(r[RISK])), 3),
        }
        for variant in variants:
            res = keep[variant][0]
            rr = res.returns.loc[a:b].dropna()
            curve = (1 + rr).cumprod()
            eras[name][variant] = {
                "cagr_pct": round(100 * (float(curve.iloc[-1]) ** (TRADING_DAYS / len(rr)) - 1), 2),
                "max_drawdown_pct": round(100 * float((curve / curve.cummax() - 1).min()), 2),
            }
    return {
        "full_sample_corr_gold_nifty": round(float(ret[GOLD].corr(ret[RISK])), 3),
        "eras": eras,
    }


# ---------------------------------------------------------------------------
def main() -> None:
    panel = build_panel()
    bench = buy_and_hold(panel, RISK, ETF_COSTS)
    bm = bench.metrics()
    price = panel[RISK]

    rows: list[dict] = []
    keep: dict[str, tuple[Result, pd.DataFrame, pd.Series]] = {}

    def add(name: str, exposure: pd.Series, safe: str, deadband: float = 0.0) -> None:
        row, res, w = evaluate(name, panel, exposure, safe, deadband=deadband)
        rows.append(row)
        keep[name] = (res, w, exposure)

    #: Volatility targets move a little every day; they are only rebalanced when
    #: the target has drifted this far, so the harness charges a realistic bill.
    vol_deadband = 0.05

    # 1. Trend filter on the index, three windows, three risk-off destinations.
    for window in (100, 150, 200):
        exp = trend_exposure(price, window)
        for safe in (CASH, GOLD, "none"):
            add(f"trend_ma{window}_{'zerocash' if safe == 'none' else safe.lower()}", exp, safe)
        # 2% hysteresis band -- the standard whipsaw cure.
        expb = trend_exposure(price, window, buffer_pct=0.02)
        for safe in (CASH, GOLD):
            add(f"trend_ma{window}_band2_{safe.lower()}", expb, safe)

    # 2. Breadth of the share universe as the switch instead of index price.
    breadth_start = None
    for window in (50, 200):
        br = breadth_series(panel.index, window)
        if breadth_start is None:
            breadth_start = str(br.dropna().index[0].date())
        for threshold in (0.4, 0.5, 0.6):
            exp = (br > threshold).astype(float).fillna(0.0)
            # Before breadth history begins, stay invested rather than in cash.
            exp[br.isna()] = 1.0
            for safe in (CASH, GOLD):
                add(f"breadth{window}_gt{int(threshold * 100)}_{safe.lower()}", exp, safe)

    # 3. Volatility targeting, no leverage.
    for window in (20, 60):
        for target in (0.10, 0.12, 0.15):
            exp = vol_target_exposure(price, window, target)
            for safe in (CASH, GOLD):
                add(f"voltgt{int(target * 100)}_{window}d_{safe.lower()}", exp, safe, vol_deadband)

    # 4. Trend filter combined with volatility targeting.
    for window in (150, 200):
        trend = trend_exposure(price, window)
        for vw in (20, 60):
            exp = trend * vol_target_exposure(price, vw, 0.12)
            for safe in (CASH, GOLD):
                add(f"trend{window}_x_voltgt12_{vw}d_{safe.lower()}", exp, safe, vol_deadband)

    n_trials = len(rows)

    # Every variant is measured on both halves, because the headline number of a
    # regime filter is usually one crash wearing a strategy's clothes.
    halves = {r["name"]: half_metrics(panel, keep[r["name"]][1]) for r in rows}
    for r in rows:
        h = halves[r["name"]]
        r["first_half_excess_sharpe_gap"] = h["first_half"]["excess_sharpe_gap"]
        r["second_half_excess_sharpe_gap"] = h["second_half"]["excess_sharpe_gap"]
        r["beats_in_both_halves"] = bool(
            h["first_half"]["excess_sharpe_gap"] > 0 and h["second_half"]["excess_sharpe_gap"] > 0
        )

    # Best by *excess-of-cash* Sharpe among variants that also cut the drawdown.
    eligible = [r for r in rows if r["max_drawdown_pct"] > bm["max_drawdown_pct"]]
    best = max(eligible or rows, key=lambda r: r["excess_sharpe"])
    # The one we would actually run: it must also beat the index in BOTH halves.
    robust = [r for r in eligible if r["beats_in_both_halves"]]
    best_robust = max(robust, key=lambda r: r["excess_sharpe"]) if robust else None
    best_res, best_w, best_exp = keep[best["name"]]
    n_obs = len(best_res.returns.dropna())

    report = {
        "generated_for": "research track D - regime / defensive overlay",
        "universe_note": (
            "Exchange-traded funds that still trade today; mild survivorship bias. "
            "LIQUIDBEES close is constant at 999.99 across 2009-2026 (constant-NAV "
            f"fund), so cash is synthetic at a flat {CASH_YIELD:.1%} a year."
        ),
        "costs": {
            "wrapper": ETF_COSTS.name,
            "round_trip_pct": round(ETF_COSTS.round_trip_pct(), 4),
        },
        "benchmark_buy_and_hold_NIFTYBEES": dict(
            bm,
            excess_sharpe=excess_sharpe(bm),
            crises={k: window_stats(bench, a, b) for k, (a, b) in CRISES.items()},
            eras={k: window_stats(bench, a, b) for k, (a, b) in GOLD_ERAS.items()},
        ),
        "sample_period_caveat": (
            "History starts 2009-01-02, immediately after the global financial "
            "crisis. Trend filters earn most of their historical reputation in "
            "2008, and this sample excludes it, so the results below are biased "
            "AGAINST the trend filter. The 2009-2026 window contains exactly one "
            "fast crash (2020) and several choppy corrections, which is the "
            "regime a moving-average filter handles worst."
        ),
        "sharpe_note": (
            "The harness 'sharpe' is CAGR/vol with no risk-free subtraction, so any "
            "rule that parks money in cash scores higher for free. 'excess_sharpe' "
            f"= (CAGR - {CASH_YIELD:.0%}) / vol is the yardstick used to pick the best."
        ),
        "breadth_note": (
            f"Breadth is only computable from {breadth_start}; before that the "
            "breadth variants are held fully invested, so their first-half record "
            "is largely unfiltered buy-and-hold."
        ),
        "n_trials": n_trials,
        "variants": sorted(rows, key=lambda r: -r["excess_sharpe"]),
        "best": {
            "name": best["name"],
            "spec": best,
            "vs_benchmark": compare(best["name"], best_res, bench),
            "deflated_sharpe": round(deflated_sharpe(best["sharpe"], n_trials, n_obs), 3),
            "deflated_excess_sharpe": round(deflated_sharpe(best["excess_sharpe"], n_trials, n_obs), 3),
            "halves": halves[best["name"]],
        },
        "best_robust_both_halves": (
            None
            if best_robust is None
            else {
                "name": best_robust["name"],
                "spec": best_robust,
                "vs_benchmark": compare(best_robust["name"], keep[best_robust["name"]][0], bench),
                "deflated_excess_sharpe": round(
                    deflated_sharpe(best_robust["excess_sharpe"], n_trials, n_obs), 3
                ),
                "halves": halves[best_robust["name"]],
            }
        ),
        "n_variants_beating_index_in_both_halves": len(robust),
        "halves": halves,
        "control_static_blends": control_blends(panel),
        "cash_yield_sensitivity": cash_sensitivity(rows, keep),
        "gold_leg_diagnostics": gold_diagnostics(
            panel,
            keep,
            sorted({"trend_ma200_goldbees", "trend_ma200_cash", best["name"]}
                   | ({best_robust["name"]} if best_robust else set())),
        ),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    hdr = (
        f"{'variant':38s} {'CAGR':>7s} {'Shrp':>6s} {'xShrp':>6s} {'MaxDD':>7s} "
        f"{'WkWin':>6s} {'flips':>6s} {'cost':>6s}"
    )
    print(hdr)
    print("-" * len(hdr))
    print(
        f"{'BUY & HOLD NIFTYBEES':38s} {bm['cagr_pct']:7.2f} {bm['sharpe']:6.2f} "
        f"{excess_sharpe(bm):6.2f} {bm['max_drawdown_pct']:7.2f} "
        f"{bm['weekly_win_rate_pct']:6.1f} {0.0:6.2f} {bm['cost_drag_pct_yr']:6.2f}"
    )
    for r in sorted(rows, key=lambda r: -r["excess_sharpe"]):
        print(
            f"{r['name']:38s} {r['cagr_pct']:7.2f} {r['sharpe']:6.2f} {r['excess_sharpe']:6.2f} "
            f"{r['max_drawdown_pct']:7.2f} {r['weekly_win_rate_pct']:6.1f} "
            f"{r['flips_per_yr']:6.2f} {r['cost_drag_pct_yr']:6.2f}"
        )
    print()
    for name, c in report["control_static_blends"].items():
        print(
            f"{name:38s} {c['cagr_pct']:7.2f} {c['sharpe']:6.2f} {c['excess_sharpe']:6.2f} "
            f"{c['max_drawdown_pct']:7.2f} {c['weekly_win_rate_pct']:6.1f} {0.0:6.2f} "
            f"{c['cost_drag_pct_yr']:6.2f}"
        )
    print(f"\ntrials={n_trials}  beat index in BOTH halves: {len(robust)}")
    print(f"best(full sample)={best['name']}  best(robust)={best_robust['name'] if best_robust else None}")
    print(f"deflated sharpe={report['best']['deflated_sharpe']} excess={report['best']['deflated_excess_sharpe']}")
    print(json.dumps(report["best"]["halves"], indent=2))
    print(json.dumps(best["crises"], indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
