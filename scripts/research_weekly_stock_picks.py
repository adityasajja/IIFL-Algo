"""Which stocks actually gain 5% in the coming week?

The question is about *individual picks*, not index categories: given a filter,
what fraction of the stocks it flags go on to gain >= 5% over the next week?
That is a hit rate, and it has a base rate to beat.

Measured over the whole liquid NSE universe (not one index), 2015-2026:

    base rate = P(a randomly chosen stock gains >= 5% next week)

Every filter below is pre-registered and scored against that base rate. A filter
is only useful if it lifts the hit rate *and* the flagged names' average return,
because a filter that raises the hit rate while lowering the average is just
selecting for volatility.

The last section applies the surviving filter to the most recent completed week
and writes an actual pick list for the coming week.

Usage::

    .venv/Scripts/python.exe scripts/research_weekly_stock_picks.py
    .venv/Scripts/python.exe scripts/research_weekly_stock_picks.py --rebuild
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
CACHE = ROOT / "data" / "self_learning" / "stock_week_panel.parquet"
OUT = ROOT / "data" / "self_learning" / "weekly_stock_picks.json"

TARGET = 0.05
MIN_DAILY_TURNOVER = 1.0e7      # Rs 1 crore median daily turnover: tradeable
MIN_DAILY_BARS = 1500


def load_daily(symbol: str, start: pd.Timestamp, end: pd.Timestamp):
    path = DAILY / f"{symbol}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path, columns=["ts", "open", "high", "low", "close", "volume"])
    except Exception:  # noqa: BLE001
        return None
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0].sort_values("ts")
    if len(df) < MIN_DAILY_BARS:
        return None
    from atr.data.hygiene import drop_reverting_spikes

    df, _ = drop_reverting_spikes(df)
    df = df[df["close"] > 0].sort_values("ts")
    if len(df) < MIN_DAILY_BARS:
        return None
    df = df.loc[(df["ts"] >= start) & (df["ts"] <= end)]
    if len(df) < MIN_DAILY_BARS:
        return None
    turnover = float((df["close"] * df["volume"]).median())
    if not np.isfinite(turnover) or turnover < MIN_DAILY_TURNOVER:
        return None
    return df


def build_panel(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """One row per (symbol, week): features known at the close, plus the outcome."""
    files = sorted(DAILY.glob("*.parquet"))
    # Prefer the bare ticker over the "-EQ" spelling; skip the duplicate.
    files = [f for f in files if not f.stem.endswith("-EQ")]
    rows = []
    for i, path in enumerate(files, 1):
        if i % 500 == 0:
            print(f"  … {i}/{len(files)}", file=sys.stderr)
        df = load_daily(path.stem, start, end)
        if df is None:
            continue
        w = df.set_index(pd.DatetimeIndex(df["ts"])).resample("W-FRI")
        wk = pd.DataFrame({
            "close": w["close"].last(),
            "open": w["open"].first(),
            "high": w["high"].max(),
            "low": w["low"].min(),
            "volume": w["volume"].sum(),
        }).dropna()
        if len(wk) < 60:
            continue
        wk["symbol"] = path.stem
        wk["week"] = wk.index
        wk["ret_1w"] = wk["close"].pct_change(1)
        wk["ret_2w"] = wk["close"].pct_change(2)
        wk["ret_4w"] = wk["close"].pct_change(4)
        wk["ret_12w"] = wk["close"].pct_change(12)
        wk["ret_26w"] = wk["close"].pct_change(26)
        wk["vol_surge"] = wk["volume"] / wk["volume"].shift(1).rolling(12).mean()
        wk["range_pct"] = (wk["high"] - wk["low"]) / wk["close"]
        wk["range_expansion"] = wk["range_pct"] / wk["range_pct"].shift(1).rolling(12).mean()
        wk["high_52w"] = wk["close"].rolling(52).max()
        wk["near_52w_high"] = wk["close"] / wk["high_52w"]
        wk["high_4w"] = wk["close"].rolling(4).max()
        wk["breakout_4w"] = (wk["close"] >= wk["high_4w"]).astype(float)
        wk["gap_up"] = wk["open"] / wk["close"].shift(1) - 1.0
        wk["next_ret"] = wk["close"].shift(-1) / wk["close"] - 1.0
        wk["next_ret_2w"] = wk["close"].shift(-2) / wk["close"] - 1.0
        rows.append(wk.reset_index(drop=True))
    if not rows:
        return pd.DataFrame()
    panel = pd.concat(rows, ignore_index=True)
    # NOTE: do *not* drop rows with a missing `next_ret`. The final week has no
    # known outcome by construction — that is the week you are picking for.
    # Dropping it silently pushes the pick list one week into the past.
    panel = panel.dropna(subset=["ret_26w", "vol_surge", "near_52w_high"])
    # A weekly move beyond +-300% is a corporate action or a bad bar, not a
    # return. Left in, one such week moves a filter's mean from +0.4% to +1.8%
    # and makes a pure artefact look like the best rule on the board. Flagged
    # rather than dropped so the count survives the parquet cache.
    panel["extreme"] = panel["next_ret"].abs() > 3.0
    return panel


# ---------------------------------------------------------------------------
# PRE-REGISTERED FILTERS. Each is a boolean mask over the panel; the score
# column is what you would rank by if you were picking a handful of names.
# Written down before scoring so the multiple-testing count is honest.
# ---------------------------------------------------------------------------
FILTERS: dict[str, dict] = {
    "top_decile_ret_1w": {"col": "ret_1w", "quantile": 0.90},
    "top_decile_ret_2w": {"col": "ret_2w", "quantile": 0.90},
    "top_decile_ret_4w": {"col": "ret_4w", "quantile": 0.90},
    "top_decile_ret_12w": {"col": "ret_12w", "quantile": 0.90},
    "top_decile_ret_26w": {"col": "ret_26w", "quantile": 0.90},
    "top_decile_vol_surge": {"col": "vol_surge", "quantile": 0.90},
    "top_decile_range_expansion": {"col": "range_expansion", "quantile": 0.90},
    "top_decile_gap_up": {"col": "gap_up", "quantile": 0.90},
    "near_52w_high": {"col": "near_52w_high", "quantile": 0.90},
    "breakout_4w": {"col": "breakout_4w", "quantile": 0.999},   # already boolean
    "momentum_and_volume": {"combo": ["ret_26w", "vol_surge"]},
    "momentum_and_52w_high": {"combo": ["ret_26w", "near_52w_high"]},
    "momentum_volume_52w_high": {"combo": ["ret_26w", "vol_surge", "near_52w_high"]},
    "short_term_breakout": {"combo": ["ret_1w", "vol_surge", "breakout_4w"]},
}


def mask_for(panel: pd.DataFrame, spec: dict) -> pd.Series:
    if "col" in spec:
        col, q = spec["col"], spec["quantile"]
        if col == "breakout_4w":
            return panel[col] >= 1.0
        return panel[col] >= panel[col].quantile(q)
    masks = []
    for col in spec["combo"]:
        if col == "breakout_4w":
            masks.append(panel[col] >= 1.0)
        else:
            masks.append(panel[col] >= panel[col].quantile(0.90))
    m = masks[0]
    for other in masks[1:]:
        m = m & other
    return m


def score_col(spec: dict) -> str:
    return spec.get("col") or spec["combo"][0]


def evaluate(panel: pd.DataFrame, spec: dict, base_rate: float,
             base_mean: float) -> dict:
    panel = panel[np.isfinite(panel["next_ret"]) & ~panel["extreme"]]
    mask = mask_for(panel, spec)
    sub = panel[mask]
    if len(sub) < 200:
        return {}
    hit = float((sub["next_ret"] >= TARGET).mean())
    mean = float(sub["next_ret"].mean())
    return {
        "fired": int(len(sub)),
        "pct_of_universe": round(100 * len(sub) / len(panel), 3),
        "hit_rate_ge_5pct": round(100 * hit, 3),
        "lift_vs_base": round(hit / base_rate, 3) if base_rate else None,
        "mean_next_week_pct": round(100 * mean, 3),
        "mean_lift_vs_base_pct": round(100 * (mean - base_mean), 3),
        "median_next_week_pct": round(100 * float(sub["next_ret"].median()), 3),
        "pct_le_minus5pct": round(100 * float((sub["next_ret"] <= -TARGET).mean()), 3),
        "best_pct": round(100 * float(sub["next_ret"].max()), 1),
    }


def top_n_weekly(panel: pd.DataFrame, spec: dict, top_n: int) -> dict:
    """Each week, take the top-N by score, equal weight, hold one week."""
    col = score_col(spec)
    rets, hits = [], []
    for _, grp in panel.groupby("week"):
        grp = grp.dropna(subset=[col, "next_ret"])
        grp = grp[~grp["extreme"]]
        if len(grp) < top_n:
            continue
        picks = grp.nlargest(top_n, col)
        rets.append(float(picks["next_ret"].mean()))
        hits.append(float((picks["next_ret"] >= TARGET).mean()))
    if len(rets) < 30:
        return {}
    r = np.array(rets)
    equity = np.cumprod(1 + r)
    peak = np.maximum.accumulate(equity)
    years = len(r) / 52
    total = float(equity[-1] - 1)
    return {
        "weeks": len(r),
        "mean_weekly_pct": round(100 * float(r.mean()), 4),
        "median_weekly_pct": round(100 * float(np.median(r)), 4),
        "total_return_pct": round(100 * total, 2),
        "cagr_pct": round(100 * ((1 + total) ** (1 / years) - 1), 2),
        "sharpe": round(float(r.mean()) / float(r.std()) * np.sqrt(52), 3)
        if r.std() > 0 else None,
        "max_drawdown_pct": round(100 * float((equity / peak - 1).min()), 2),
        "weeks_with_at_least_one_5pct_winner_pct": round(100 * float(np.mean([h > 0 for h in hits])), 2),
        "mean_share_of_picks_ge_5pct": round(100 * float(np.mean(hits)), 3),
        "pct_weeks_portfolio_ge_5pct": round(100 * float((r >= TARGET).mean()), 3),
        "best_week_pct": round(100 * float(r.max()), 2),
        "worst_week_pct": round(100 * float(r.min()), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2026-09-11")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--top-n", type=int, default=10)
    args = ap.parse_args()

    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    if CACHE.exists() and not args.rebuild:
        panel = pd.read_parquet(CACHE)
        print(f"panel from cache: {len(panel):,} stock-weeks")
    else:
        print("building panel (this reads every daily parquet)…", file=sys.stderr)
        panel = build_panel(start, end)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        panel.to_parquet(CACHE, index=False)
        print(f"panel built: {len(panel):,} stock-weeks → {CACHE}")

    if panel.empty:
        print("empty panel", file=sys.stderr)
        return 1

    scored = panel[np.isfinite(panel["next_ret"]) & ~panel["extreme"]]
    base_rate = float((scored["next_ret"] >= TARGET).mean())
    base_mean = float(scored["next_ret"].mean())
    print(f"\nuniverse: {panel['symbol'].nunique()} symbols, {panel['week'].nunique()} weeks, "
          f"{len(scored):,} stock-weeks with a known outcome")
    print(f"BASE RATE: {100 * base_rate:.2f}% of stock-weeks gain >= 5% the next week")
    print(f"           mean next-week return {100 * base_mean:+.3f}%")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "question": "which stocks gain >= 5% in the coming week?",
        "universe": {
            "symbols": int(panel["symbol"].nunique()),
            "weeks": int(panel["week"].nunique()),
            "stock_weeks": int(len(panel)),
            "min_daily_turnover_rs": MIN_DAILY_TURNOVER,
            "window": [str(panel["week"].min().date()), str(panel["week"].max().date())],
        },
        "extreme_weeks_excluded": int(panel["extreme"].sum()),
        "base_rate_ge_5pct": round(100 * base_rate, 3),
        "base_mean_next_week_pct": round(100 * base_mean, 3),
        "filters": {},
        "top_n_portfolios": {},
    }

    # Suspicion check: a filter whose mean is far above its hit rate is usually
    # a data artefact rather than an edge. Report the per-year spread so a
    # single lucky year cannot masquerade as a result.
    def per_year(panel: pd.DataFrame, spec: dict) -> dict:
        sub = panel[np.isfinite(panel["next_ret"]) & ~panel["extreme"]]
        sub = sub[mask_for(sub, spec)]
        if sub.empty:
            return {}
        by = sub.groupby(sub["week"].dt.year)["next_ret"].mean()
        return {str(int(k)): round(100 * float(v), 1) for k, v in by.items()}

    payload["per_year_mean_pct"] = {
        name: per_year(panel, spec) for name, spec in FILTERS.items()
    }

    print("\n── filters, ranked by hit rate ─────────────────────────")
    for name, spec in FILTERS.items():
        res = evaluate(panel, spec, base_rate, base_mean)
        if not res:
            continue
        payload["filters"][name] = res
        print(f"  {name:28s} fires {res['pct_of_universe']:5.2f}%  "
              f"P(≥5%)={res['hit_rate_ge_5pct']:5.2f}%  "
              f"lift {res['lift_vs_base']:.2f}x  "
              f"mean {res['mean_next_week_pct']:+.3f}%  "
              f"P(≤-5%)={res['pct_le_minus5pct']:5.2f}%")

    print(f"\n── top-{args.top_n} portfolios by score ────────────────────")
    for name, spec in FILTERS.items():
        res = top_n_weekly(panel, spec, args.top_n)
        if not res:
            continue
        payload["top_n_portfolios"][name] = res
        print(f"  {name:28s} mean {res['mean_weekly_pct']:+.3f}%/wk  "
              f"P(port ≥5%)={res['pct_weeks_portfolio_ge_5pct']:5.2f}%  "
              f"picks hitting 5% {res['mean_share_of_picks_ge_5pct']:5.2f}%  "
              f"weeks with ≥1 winner {res['weeks_with_at_least_one_5pct_winner_pct']:5.1f}%  "
              f"maxDD {res['max_drawdown_pct']:6.1f}%")

    # ------------------------------------------------------------------
    # Apply to the most recent completed week → picks for the coming week.
    # ------------------------------------------------------------------
    last_week = panel["week"].max()
    latest = panel[panel["week"] == last_week].copy()
    print(f"\n── picks for the week after {last_week:%Y-%m-%d} ─────────────")
    payload["latest_week"] = str(last_week.date())
    payload["picks"] = {}

    ranked = []
    for name, spec in FILTERS.items():
        col = score_col(spec)
        sub = latest.dropna(subset=[col])
        if len(sub) < args.top_n:
            continue
        # Apply *this filter's own* mask before ranking. Ranking the whole
        # universe and then labelling the result with a filter's statistics
        # quotes the stats of a rule that was never applied — the first version
        # of this did exactly that.
        sub = sub[mask_for(sub, spec)]
        if len(sub) < args.top_n:
            continue
        picks = sub.nlargest(args.top_n, col)
        hist = payload["filters"].get(name, {})
        payload["picks"][name] = {
            "expected_hit_rate_ge_5pct": hist.get("hit_rate_ge_5pct"),
            "expected_mean_next_week_pct": hist.get("mean_next_week_pct"),
            "names": [
                {"symbol": r["symbol"], "close": round(float(r["close"]), 2),
                 "ret_26w_pct": round(100 * float(r["ret_26w"]), 1)
                 if np.isfinite(r["ret_26w"]) else None}
                for _, r in picks.iterrows()
            ],
        }
        ranked.append((name, hist.get("hit_rate_ge_5pct", 0)))

    ranked.sort(key=lambda x: -(x[1] or 0))
    for name, hit in ranked[:3]:
        entry = payload["picks"][name]
        names = ", ".join(p["symbol"] for p in entry["names"])
        print(f"  {name}  (P(≥5%)={hit}%, mean {entry['expected_mean_next_week_pct']}%/wk)")
        print(f"    {names}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
