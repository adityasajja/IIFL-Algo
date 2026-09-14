"""Is a 5-10% weekly return achievable on NSE equity?

This script does not search for a strategy. It measures the target itself:
what a 5%/week and 10%/week hurdle demands of a return distribution, and how
often the real NSE equity market has produced anything close to it.

Run:  ./.venv/Scripts/python.exe scripts/research_weekly_target.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
OUT = ROOT / "data" / "self_learning" / "weekly_target_feasibility.json"

TRADING_WEEKS = 52
TARGETS = (0.05, 0.10)


def load(symbol: str) -> pd.Series | None:
    path = DAILY / f"{symbol}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=["ts", "close"])
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0].sort_values("ts")
    if len(df) < 300:
        return None
    # The IIFL daily cache carries corrupt bars on 2021-09-15/16 (14 bars at
    # 3-5x the surrounding level). Left in, they fabricate +1000% weeks and
    # inflate every mean. Reuse the repo's reversion-keyed filter.
    from atr.data.hygiene import drop_reverting_spikes

    df, _dropped = drop_reverting_spikes(df)
    df = df[df["close"] > 0].sort_values("ts")
    if len(df) < 300:
        return None
    s = pd.Series(df["close"].to_numpy(), index=pd.DatetimeIndex(df["ts"]))
    return s[~s.index.duplicated(keep="last")]


def weekly(close: pd.Series) -> pd.Series:
    """Friday-to-Friday returns from a daily close series."""
    w = close.resample("W-FRI").last().dropna()
    return w.pct_change().dropna()


def describe_weekly(r: pd.Series) -> dict:
    yrs = len(r) / TRADING_WEEKS
    ann_vol = r.std() * np.sqrt(TRADING_WEEKS)
    return {
        "weeks": int(len(r)),
        "years": round(yrs, 2),
        "mean_weekly_pct": round(100 * r.mean(), 4),
        "median_weekly_pct": round(100 * r.median(), 4),
        "sd_weekly_pct": round(100 * r.std(), 4),
        "ann_vol_pct": round(100 * ann_vol, 2),
        "best_week_pct": round(100 * r.max(), 2),
        "worst_week_pct": round(100 * r.min(), 2),
        "hit_ge_2pct": round(100 * (r >= 0.02).mean(), 3),
        "hit_ge_5pct": round(100 * (r >= 0.05).mean(), 3),
        "hit_ge_10pct": round(100 * (r >= 0.10).mean(), 3),
        "weeks_ge_5pct_count": int((r >= 0.05).sum()),
        "weeks_ge_10pct_count": int((r >= 0.10).sum()),
    }


def hurdle_table(close: pd.Series, name: str) -> dict:
    """What each weekly hurdle demands, and how the instrument actually did."""
    w = close.resample("W-FRI").last().dropna()
    total_years = (w.index[-1] - w.index[0]).days / 365.25
    total_ret = w.iloc[-1] / w.iloc[0] - 1
    cagr = (1 + total_ret) ** (1 / total_years) - 1
    r = w.pct_change().dropna()
    ann_vol = r.std() * np.sqrt(TRADING_WEEKS)
    cagr_ratio = (1 + cagr) ** (1 / TRADING_WEEKS) - 1

    row = {
        "instrument": name,
        "years": round(total_years, 2),
        "total_return_pct": round(100 * total_ret, 2),
        "cagr_pct": round(100 * cagr, 2),
        "weekly_equivalent_pct": round(100 * cagr_ratio, 4),
        "ann_vol_pct": round(100 * ann_vol, 2),
        "realised_sharpe": round(cagr / ann_vol, 3) if ann_vol else None,
        "required_sharpe": {},
        "rupees_5L": round(500000 * (1 + total_ret)),
    }
    for t in TARGETS:
        ann = (1 + t) ** TRADING_WEEKS - 1
        row["required_sharpe"][f"{int(t*100)}pct_wk"] = {
            "annual_return_pct": round(100 * ann, 1),
            "multiple_of_5L_in_1yr": round((1 + t) ** TRADING_WEEKS, 1),
            "sharpe_at_realised_vol": round(ann / ann_vol, 2) if ann_vol else None,
            "sharpe_at_30pct_vol": round(ann / 0.30, 2),
            "x_realised_cagr": round(ann / cagr, 1) if cagr > 0 else None,
        }
    return row


def bootstrap_paths(r: np.ndarray, n_paths: int, seed: int = 7) -> dict:
    """IID bootstrap of 52-week paths from the realised weekly distribution."""
    rng = np.random.default_rng(seed)
    draws = rng.choice(r, size=(n_paths, TRADING_WEEKS), replace=True)
    terminal = np.prod(1 + draws, axis=1)
    out = {
        "n_paths": n_paths,
        "median_terminal_multiple": round(float(np.median(terminal)), 3),
        "pct_paths_doubling": round(100 * float((terminal >= 2).mean()), 2),
        "best_path_multiple": round(float(terminal.max()), 2),
        "pct_paths_ge_5pct_wk": round(100 * float((terminal >= 1.05**52).mean()), 4),
        "pct_paths_ge_10pct_wk": round(100 * float((terminal >= 1.10**52).mean()), 5),
    }
    # Continuous drag: IID resampling lets a lucky path stack; report the
    # compounded return of the *mean* log return, which is what a repeatable
    # edge would have to beat.
    out["geometric_annual_pct_from_bootstrap"] = round(
        100 * (np.exp(TRADING_WEEKS * np.log1p(draws).mean()) - 1), 2
    )
    return out


def best_rolling_window(close: pd.Series, weeks: int = TRADING_WEEKS) -> dict:
    w = close.resample("W-FRI").last().dropna()
    roll = w / w.shift(weeks) - 1
    roll = roll.dropna()
    if roll.empty:
        return {}
    i = int(np.nanargmax(roll.to_numpy()))
    return {
        "best_52w_return_pct": round(100 * float(roll.iloc[i]), 2),
        "best_52w_end": str(roll.index[i].date()),
        "pct_52w_windows_ge_5pct_wk": round(100 * float((roll >= 1.05**weeks - 1).mean()), 4),
        "n_52w_windows": int(len(roll)),
    }


def stock_week_universe(symbols: list[str], start, end) -> dict:
    """How often did an individual NSE stock deliver a >=5% week?"""
    rows = []
    for sym in symbols:
        s = load(sym)
        if s is None:
            continue
        s = s.loc[(s.index >= start) & (s.index <= end)]
        if len(s) < 300:
            continue
        r = weekly(s)
        if len(r) < 100:
            continue
        rows.append(r)
    if not rows:
        return {}
    allr = pd.concat(rows, ignore_index=True)
    return {
        "n_symbols": len(rows),
        "n_stock_weeks": int(len(allr)),
        "pct_stock_weeks_ge_5pct": round(100 * float((allr >= 0.05).mean()), 3),
        "pct_stock_weeks_ge_10pct": round(100 * float((allr >= 0.10).mean()), 3),
        "pct_stock_weeks_ge_20pct": round(100 * float((allr >= 0.20).mean()), 3),
        "median_stock_week_pct": round(100 * float(allr.median()), 4),
        # If you held 10 names at equal weight, how often is the *portfolio*
        # up 5% in a week? Approximate by sampling 10 stock-weeks per week.
        "note": "stock-weeks pooled across names and dates; not a portfolio return",
    }


def portfolio_simulation(start, end, sizes=(1, 5, 10, 20, 50), n_draws: int = 2000,
                         seed: int = 11) -> dict:
    """Weekly return of an equal-weight portfolio of N randomly chosen names.

    This is the decisive test. 14.6% of individual stock-weeks clear +5%, so the
    raw material for a 5% week plainly exists. What matters is whether a
    *portfolio* you could have assembled in advance clears it.
    """
    uni = ROOT / "data" / "universe"
    syms: list[str] = []
    for f in ("n50.txt", "mid150.txt", "smallcap250.txt"):
        p = uni / f
        if p.exists():
            syms += [x.strip().upper() for x in p.read_text().replace("\n", ",").split(",")]
    syms = sorted({s for s in syms if s})

    cols = {}
    for sym in syms:
        s = load(sym)
        if s is None:
            continue
        s = s.loc[(s.index >= start) & (s.index <= end)]
        if len(s) < 300:
            continue
        cols[sym] = weekly(s)
    if not cols:
        return {}
    W = pd.DataFrame(cols).sort_index()
    W = W.loc[:, W.notna().sum() >= 100]
    # A weekly move beyond +-300% is a corporate-action artefact, not a return.
    # Count and report them rather than silently clipping.
    n_extreme = int((W.abs() > 3.0).sum().sum())
    W = W.mask(W.abs() > 3.0)
    # Drop all-NaN rows; keep the rest (nanmean tolerates missing names).
    W = W.loc[W.notna().sum(axis=1) >= 10]
    weeks = W.index
    M = W.to_numpy(dtype=float)
    n_w, n_s = M.shape

    rng = np.random.default_rng(seed)
    out: dict = {"n_symbols": int(n_s), "n_weeks": int(n_w),
                 "extreme_weeks_dropped": n_extreme,
                 "window": [str(weeks[0].date()), str(weeks[-1].date())]}
    for N in sizes:
        if N > n_s:
            continue
        draws = np.empty((n_draws, n_w), dtype=float)
        for d in range(n_draws):
            pick = rng.choice(n_s, size=N, replace=False)
            sub = M[:, pick]
            with np.errstate(invalid="ignore"):
                draws[d] = np.nanmean(sub, axis=1)
        flat = draws[np.isfinite(draws)]
        out[f"N={N}"] = {
            "mean_weekly_pct": round(100 * float(flat.mean()), 4),
            "median_weekly_pct": round(100 * float(np.median(flat)), 4),
            "sd_weekly_pct": round(100 * float(flat.std()), 4),
            "pct_weeks_ge_5pct": round(100 * float((flat >= 0.05).mean()), 3),
            "pct_weeks_ge_2pct": round(100 * float((flat >= 0.02).mean()), 3),
            "pct_weeks_le_minus5pct": round(100 * float((flat <= -0.05).mean()), 3),
            "best_week_pct": round(100 * float(flat.max()), 2),
        }

    # Ceiling: hold the single best-performing name every week (perfect
    # foresight). Not achievable — it bounds the idea, it does not describe it.
    best = np.nanmax(M, axis=1)
    out["oracle_single_best_name"] = {
        "mean_weekly_pct": round(100 * float(np.nanmean(best)), 2),
        "pct_weeks_ge_5pct": round(100 * float((best >= 0.05).mean()), 2),
        "note": "perfect foresight of the week's top name; unachievable upper bound",
    }
    # Median name — the "pick an average stock" counterfactual.
    med = np.nanmedian(M, axis=1)
    out["median_name"] = {
        "mean_weekly_pct": round(100 * float(np.nanmean(med)), 3),
        "pct_weeks_ge_5pct": round(100 * float((med >= 0.05).mean()), 2),
    }
    return out


def required_edge(port: dict, n_key: str = "N=10") -> dict:
    """Express the hurdle as the Sharpe a strategy would have to sustain.

    Uses the random portfolio's own volatility, so the comparison is against
    something that actually exists rather than against a made-up vol number.
    """
    base = port.get(n_key)
    if not base:
        return {}
    mu = base["mean_weekly_pct"] / 100
    sd = base["sd_weekly_pct"] / 100
    out: dict = {"baseline": n_key,
                 "baseline_mean_weekly_pct": base["mean_weekly_pct"],
                 "baseline_sd_weekly_pct": base["sd_weekly_pct"]}
    for t in TARGETS:
        shift = t - mu
        weekly_sharpe = shift / sd
        out[f"{int(t*100)}pct_wk"] = {
            "extra_weekly_return_needed_pct_points": round(100 * shift, 2),
            "in_weekly_sd_units": round(weekly_sharpe, 3),
            "annualised_sharpe_required": round(weekly_sharpe * np.sqrt(TRADING_WEEKS), 2),
            "t_stat_over_52w": round(weekly_sharpe * np.sqrt(TRADING_WEEKS), 2),
        }
    return out


def main() -> None:
    report: dict = {"generated": pd.Timestamp.now().isoformat(timespec="seconds")}

    # --- 1. The target itself -------------------------------------------------
    report["hurdle_math"] = {
        f"{int(t*100)}pct_per_week": {
            "annual_return_pct": round(100 * ((1 + t) ** TRADING_WEEKS - 1), 1),
            "multiple_in_1yr": round((1 + t) ** TRADING_WEEKS, 1),
            "multiple_in_3yr": round((1 + t) ** (3 * TRADING_WEEKS), 0),
            "rs_5L_in_1yr": round(500000 * (1 + t) ** TRADING_WEEKS),
        }
        for t in TARGETS
    }

    # --- 2. Real instruments --------------------------------------------------
    instruments = {}
    for sym in ("NIFTYBEES", "JUNIORBEES", "BANKBEES"):
        s = load(sym)
        if s is None:
            continue
        instruments[sym] = hurdle_table(s, sym)
        instruments[sym]["weekly_distribution"] = describe_weekly(weekly(s))
        instruments[sym]["best_rolling"] = best_rolling_window(s)
    report["instruments"] = instruments

    # --- 3. Bootstrap ---------------------------------------------------------
    nb = load("NIFTYBEES")
    if nb is not None:
        report["bootstrap_niftybees_52w"] = bootstrap_paths(weekly(nb).to_numpy(), 200_000)

    # --- 4. The universe as individual names ---------------------------------
    uni = ROOT / "data" / "universe"
    syms: list[str] = []
    for f in ("n50.txt", "mid150.txt", "smallcap250.txt"):
        p = uni / f
        if p.exists():
            # These are single-line comma-separated, not one-per-line.
            syms += [x.strip().upper() for x in p.read_text().replace("\n", ",").split(",")]
    syms = sorted({s for s in syms if s})
    if syms:
        start = pd.Timestamp("2015-01-01")
        end = pd.Timestamp("2026-09-11")
        report["individual_stocks"] = stock_week_universe(syms, start, end)
        report["random_portfolios"] = portfolio_simulation(start, end)
        report["required_edge"] = required_edge(report["random_portfolios"])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
