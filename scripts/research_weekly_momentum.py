"""Can weekly momentum selection deliver 5% per week across a portfolio?

The claim under test, verbatim from the request:

    "we should be able to filter and pick stocks that can gain momentum in the
    coming week. it's okay if all the stocks that we select don't perform but
    cumulative I should be earning 5% each week across the portfolio."

That is a testable statement, so this script tests it rather than argues about
it. Three things make it a fair test:

1. **The grid is pre-registered below, before any result is seen.** Every
   combination in it counts as a trial, and the trial count is reported. If the
   grid is widened later to find a winner, that shows up in the denominator.
2. **Every configuration is scored against a matched random control.** The
   control selects the *same number of names on the same dates* with the
   ranking destroyed — same concentration, same turnover, same costs, same
   exposure. If momentum beats it, the ranking contributed; if it does not, the
   configuration is a volatility choice wearing a signal's name.
3. **The cost model is the realistic one** (`IndianDeliveryCosts`, ~0.283% of
   turnover per round trip, dominated by STT at 0.1% per leg). A weekly
   rebalance pays that every week.

The decisive output is not a cumulative return. It is the **weekly return
distribution**, and specifically the pair

    P(week >= +5%)   versus   P(week <= -5%)

Because a rule that reaches +5% often but -5% just as often has not found an
edge, it has bought variance. The request accepts losers; it does not accept
that the winners and losers are the same size.

Usage::

    .venv/Scripts/python.exe scripts/research_weekly_momentum.py
    .venv/Scripts/python.exe scripts/research_weekly_momentum.py --universes smallcap250
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
UNIVERSE_DIR = ROOT / "data" / "universe"

TRADING_WEEKS = 52
#: Round trip on NSE delivery, as a fraction of turnover. STT 0.1% per leg
#: dominates; see `atr.backtest.costs.IndianDeliveryCosts`.
ROUND_TRIP_COST = 0.00283

# ---------------------------------------------------------------------------
# PRE-REGISTERED GRID — fixed before the first run, not after.
#
# The horizons span the request's own language ("momentum in the coming week")
# rather than being tuned: 1 and 2 weeks are the literal short-horizon reading,
# 4/12/26 weeks are the textbook Jegadeesh-Titman windows, included so that a
# null result cannot be blamed on "you tested the wrong horizon".
#
# `skip_weeks` is included because short-horizon returns mean-revert; skipping
# the most recent week is the standard correction and omitting it is the
# standard mistake. Both sides are tested so neither can be blamed.
# ---------------------------------------------------------------------------
GRID = {
    "lookback_weeks": [1, 2, 4, 12, 26],
    "skip_weeks": [0, 1],
    "top_n": [5, 10, 20],
}
CONTROL_SEEDS = list(range(8))

UNIVERSES = {
    "nifty50": "n50.txt",
    "midcap150": "mid150.txt",
    "smallcap250": "smallcap250.txt",
}


def universe_symbols(name: str) -> list[str]:
    path = UNIVERSE_DIR / UNIVERSES[name]
    if not path.exists():
        return []
    # Single-line comma-separated, not one per line — `.split()` returns one
    # giant token and yields nothing, which reads as "no data".
    raw = path.read_text().replace("\n", ",")
    return sorted({s.strip().upper() for s in raw.split(",") if s.strip()})


def load_weekly(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series | None:
    """Friday-close series for one symbol, hygiene-filtered."""
    path = DAILY / f"{symbol}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path, columns=["ts", "close"])
    except Exception:  # noqa: BLE001
        return None
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0].sort_values("ts")
    if len(df) < 300:
        return None
    from atr.data.hygiene import drop_reverting_spikes

    df, _ = drop_reverting_spikes(df)
    df = df[df["close"] > 0].sort_values("ts")
    if len(df) < 300:
        return None
    s = pd.Series(df["close"].to_numpy(), index=pd.DatetimeIndex(df["ts"]))
    s = s[~s.index.duplicated(keep="last")]
    s = s.loc[(s.index >= start) & (s.index <= end)]
    if len(s) < 250:
        return None
    return s.resample("W-FRI").last().dropna()


def build_matrix(symbols: list[str], start: pd.Timestamp, end: pd.Timestamp):
    cols = {}
    for sym in symbols:
        s = load_weekly(sym, start, end)
        if s is not None:
            cols[sym] = s
    if not cols:
        return None, None
    W = pd.DataFrame(cols).sort_index()
    W = W.loc[:, W.notna().sum() >= 100]
    W = W.loc[W.notna().sum(axis=1) >= 10]
    return W, W.pct_change()


def weekly_metrics(returns: np.ndarray) -> dict:
    r = returns[np.isfinite(returns)]
    if len(r) < 30:
        return {}
    equity = np.cumprod(1 + r)
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1
    years = len(r) / TRADING_WEEKS
    total = float(equity[-1] - 1)
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else 0.0
    sd = float(r.std())
    return {
        "weeks": int(len(r)),
        "mean_weekly_pct": round(100 * float(r.mean()), 4),
        "median_weekly_pct": round(100 * float(np.median(r)), 4),
        "sd_weekly_pct": round(100 * sd, 4),
        "total_return_pct": round(100 * total, 2),
        "cagr_pct": round(100 * cagr, 2),
        "ann_vol_pct": round(100 * sd * np.sqrt(TRADING_WEEKS), 2),
        "sharpe": round(cagr / (sd * np.sqrt(TRADING_WEEKS)), 3) if sd > 0 else None,
        "max_drawdown_pct": round(100 * float(dd.min()), 2),
        "hit_ge_5pct": round(100 * float((r >= 0.05).mean()), 3),
        "hit_ge_2pct": round(100 * float((r >= 0.02).mean()), 3),
        "hit_ge_1pct": round(100 * float((r >= 0.01).mean()), 3),
        "hit_le_minus5pct": round(100 * float((r <= -0.05).mean()), 3),
        "hit_le_minus2pct": round(100 * float((r <= -0.02).mean()), 3),
        "best_week_pct": round(100 * float(r.max()), 2),
        "worst_week_pct": round(100 * float(r.min()), 2),
        "weeks_ge_5pct": int((r >= 0.05).sum()),
    }


def simulate(W: pd.DataFrame, R: pd.DataFrame, lookback: int, skip: int,
             top_n: int, seed: int | None, exec_lag: int = 0) -> np.ndarray | None:
    """Weekly returns of an equal-weight top-N momentum book.

    Signal at week ``t`` is the return from ``t-skip-lookback`` to ``t-skip``.
    ``exec_lag=0`` trades at that same Friday close; ``exec_lag=1`` defers the
    trade a week, which is the conservative reading of "you see the signal and
    then act".
    """
    prices = W.to_numpy(dtype=float)
    rets = R.to_numpy(dtype=float)
    n_w, n_s = prices.shape
    if n_w <= lookback + skip + exec_lag + 5:
        return None

    rng = np.random.default_rng(seed if seed is not None else 0)
    signal = np.full((n_w, n_s), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        signal = prices / np.roll(prices, skip + lookback, axis=0) - 1.0
    signal[: skip + lookback] = np.nan

    out = np.full(n_w, np.nan)
    prev: set[int] = set()
    for t in range(n_w - 1):
        entry_week = t + exec_lag
        if entry_week + 1 >= n_w:
            break
        row = signal[t]
        ok = np.isfinite(row) & np.isfinite(rets[entry_week + 1])
        candidates = np.flatnonzero(ok)
        if candidates.size < top_n:
            continue
        if seed is None:
            scores = row[candidates]
            order = np.argsort(-scores, kind="stable")
            picks = set(candidates[order[:top_n]].tolist())
        else:
            picks = set(rng.choice(candidates, size=top_n, replace=False).tolist())
        nxt = rets[entry_week + 1]
        gross = float(np.nanmean(nxt[list(picks)]))
        turnover = 1.0 if not prev else len(picks - prev) / float(top_n)
        out[entry_week + 1] = gross - turnover * ROUND_TRIP_COST
        prev = picks
    return out


def oracle(W: pd.DataFrame, R: pd.DataFrame, top_n: int) -> np.ndarray | None:
    """Hold the top-N names by *realised* next-week return. Unachievable ceiling."""
    rets = R.to_numpy(dtype=float)
    n_w, _ = rets.shape
    out = np.full(n_w, np.nan)
    for t in range(1, n_w):
        row = rets[t]
        ok = np.isfinite(row)
        if ok.sum() < top_n:
            continue
        out[t] = float(np.mean(np.sort(row[ok])[-top_n:]))
    return out


def leverage_analysis(mean_weekly_pct: float, max_dd_pct: float,
                      target_weekly_pct: float = 5.0,
                      borrow_rate_pct: float = 11.0) -> dict:
    """What leverage bridges the gap to 5%/week, and what it does to the floor?

    This is the honest answer to "so lever it". Leverage multiplies the return
    *and* the drawdown by the same factor, and the borrowed money costs
    something. Solving for the leverage ``L`` that nets the target:

        L * gross_annual - (L - 1) * borrow_rate = target_annual

    The number that matters is not the required ``L``. It is ``ruin_dd_pct``:
    the unlevered drawdown that wipes the account at that leverage. If the
    strategy has ever drawn down more than that, the plan is not risky — it is
    arithmetically impossible.
    """
    gross_annual = mean_weekly_pct / 100.0 * TRADING_WEEKS
    target_annual = target_weekly_pct / 100.0 * TRADING_WEEKS
    borrow = borrow_rate_pct / 100.0
    denom = gross_annual - borrow
    if denom <= 0:
        return {"leverage_required": None,
                "reason": "gross edge is below the cost of borrowing — no "
                          "leverage reaches the target"}
    L = (target_annual + borrow) / denom
    return {
        "gross_edge_annual_pct": round(100 * gross_annual, 1),
        "target_annual_pct": round(100 * target_annual, 1),
        "borrow_rate_pct": borrow_rate_pct,
        "leverage_required": round(L, 2),
        "financing_drag_annual_pct": round(100 * (L - 1) * borrow, 1),
        "unlevered_max_dd_pct": max_dd_pct,
        "levered_max_dd_pct": round(max_dd_pct * L, 1),
        "ruin_dd_pct": round(-100.0 / L, 2),
        "ruin": max_dd_pct * L <= -100.0,
        "note": "ruin_dd_pct is the unlevered drawdown that wipes the account at "
                "this leverage; compare it to unlevered_max_dd_pct",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universes", default="nifty50,midcap150,smallcap250")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2026-09-11")
    ap.add_argument("--out", default="data/self_learning/weekly_momentum.json")
    ap.add_argument("--exec-lag", type=int, default=0)
    args = ap.parse_args()

    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    combos = 1
    for v in GRID.values():
        combos *= len(v)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "claim_under_test": (
            "filter and pick stocks that gain momentum in the coming week; "
            "losers acceptable, cumulative 5% per week across the portfolio"
        ),
        "pre_registered_grid": GRID,
        "control": {"kind": "random selection, same N, same dates",
                    "seeds": CONTROL_SEEDS},
        "cost_model": {"round_trip_pct_of_turnover": round(100 * ROUND_TRIP_COST, 4),
                       "source": "IndianDeliveryCosts (STT 0.1% per leg)"},
        "window": [str(start.date()), str(end.date())],
        "combos_per_universe": combos,
        "exec_lag_weeks": args.exec_lag,
        "universes": {},
    }

    for name in [u.strip() for u in args.universes.split(",") if u.strip()]:
        syms = universe_symbols(name)
        if not syms:
            print(f"{name}: no universe list", file=sys.stderr)
            continue
        W, R = build_matrix(syms, start, end)
        if W is None:
            print(f"{name}: no cached history", file=sys.stderr)
            continue

        # Buy & hold over exactly the same window and names, equal weight.
        bh = R.mean(axis=1).to_numpy()
        bh_metrics = weekly_metrics(bh)

        print(f"\n══ {name}: {W.shape[1]} names, {W.shape[0]} weeks, "
              f"{W.index[0]:%Y-%m-%d} → {W.index[-1]:%Y-%m-%d}")
        print(f"   buy & hold equal weight: {bh_metrics['total_return_pct']:+.2f}%  "
              f"CAGR {bh_metrics['cagr_pct']:.2f}%  Sharpe {bh_metrics['sharpe']:.3f}  "
              f"mean {bh_metrics['mean_weekly_pct']:+.4f}%/wk  "
              f"P(>=5%)={bh_metrics['hit_ge_5pct']:.2f}%  "
              f"P(<=-5%)={bh_metrics['hit_le_minus5pct']:.2f}%")

        entry: dict = {
            "symbols": int(W.shape[1]),
            "weeks": int(W.shape[0]),
            "buy_and_hold": bh_metrics,
            "configs": [],
            "oracle": {},
        }

        for top_n in GRID["top_n"]:
            orc = oracle(W, R, top_n)
            if orc is not None:
                entry["oracle"][f"top{top_n}"] = weekly_metrics(orc)

        trials = 0
        for lookback in GRID["lookback_weeks"]:
            for skip in GRID["skip_weeks"]:
                for top_n in GRID["top_n"]:
                    trials += 1
                    r = simulate(W, R, lookback, skip, top_n, None, args.exec_lag)
                    if r is None:
                        continue
                    m = weekly_metrics(r)
                    if not m:
                        continue
                    ctrl = []
                    for seed in CONTROL_SEEDS:
                        cr = simulate(W, R, lookback, skip, top_n, seed, args.exec_lag)
                        if cr is None:
                            continue
                        cm = weekly_metrics(cr)
                        if cm:
                            ctrl.append(cm)
                    ctrl_mean = round(statistics.fmean(
                        c["mean_weekly_pct"] for c in ctrl), 4) if ctrl else None
                    ctrl_sd = round(statistics.stdev(
                        c["mean_weekly_pct"] for c in ctrl), 4) if len(ctrl) > 1 else None
                    z = None
                    if ctrl_mean is not None and ctrl_sd and ctrl_sd > 1e-9:
                        z = round((m["mean_weekly_pct"] - ctrl_mean) / ctrl_sd, 2)
                    cfg = {
                        "lookback_weeks": lookback,
                        "skip_weeks": skip,
                        "top_n": top_n,
                        "metrics": m,
                        "control": {
                            "seeds": len(ctrl),
                            "mean_weekly_pct": ctrl_mean,
                            "sd_weekly_pct": ctrl_sd,
                            "hit_ge_5pct": round(statistics.fmean(
                                c["hit_ge_5pct"] for c in ctrl), 3) if ctrl else None,
                            "hit_le_minus5pct": round(statistics.fmean(
                                c["hit_le_minus5pct"] for c in ctrl), 3) if ctrl else None,
                            "sharpe": round(statistics.fmean(
                                c["sharpe"] for c in ctrl if c["sharpe"] is not None), 3)
                            if ctrl else None,
                        },
                        "z_vs_control": z,
                    }
                    entry["configs"].append(cfg)
                    print(f"   L={lookback:2d}w skip={skip} N={top_n:2d}  "
                          f"mean {m['mean_weekly_pct']:+.3f}%/wk  "
                          f"P(≥5%)={m['hit_ge_5pct']:5.2f}%  "
                          f"P(≤-5%)={m['hit_le_minus5pct']:5.2f}%  "
                          f"tot {m['total_return_pct']:+9.2f}%  "
                          f"Sh {m['sharpe']:+.2f}  "
                          f"ctrl {ctrl_mean:+.3f}  z={z}")

        entry["trials"] = trials
        cfgs = entry["configs"]
        if cfgs:
            best = max(cfgs, key=lambda c: c["metrics"]["mean_weekly_pct"])
            entry["best_mean_weekly"] = {
                k: best[k] for k in ("lookback_weeks", "skip_weeks", "top_n",
                                     "z_vs_control")
            }
            entry["best_mean_weekly"]["mean_weekly_pct"] = best["metrics"]["mean_weekly_pct"]
            entry["max_hit_ge_5pct"] = max(c["metrics"]["hit_ge_5pct"] for c in cfgs)
            entry["max_mean_weekly_pct"] = max(c["metrics"]["mean_weekly_pct"] for c in cfgs)
            entry["n_configs_beating_control"] = sum(
                1 for c in cfgs
                if c["z_vs_control"] is not None and c["z_vs_control"] >= 1.895
            )
            entry["n_configs_with_mean_ge_5pct"] = sum(
                1 for c in cfgs if c["metrics"]["mean_weekly_pct"] >= 5.0
            )
            entry["leverage_to_reach_5pct_wk"] = leverage_analysis(
                best["metrics"]["mean_weekly_pct"],
                best["metrics"]["max_drawdown_pct"],
            )
        payload["universes"][name] = entry

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf8")
    print(f"\nwrote {out}")

    print("\n── summary ─────────────────────────────────────────────")
    for name, e in payload["universes"].items():
        if not e.get("configs"):
            continue
        print(f"{name}: {e['trials']} configs | best mean "
              f"{e['max_mean_weekly_pct']:+.3f}%/wk | max P(≥5%) "
              f"{e['max_hit_ge_5pct']:.2f}% | beating control at t=1.895: "
              f"{e['n_configs_beating_control']}/{len(e['configs'])} | "
              f"mean ≥5%/wk: {e['n_configs_with_mean_ge_5pct']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
