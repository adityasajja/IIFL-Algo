"""Track B: where does cross-sectional stock selection beat its own cost?

The question is not "does momentum exist in NSE cash equity" — it is "at what
rebalance frequency does whatever edge exists survive a 0.33% delivery round
trip". Securities transaction tax of 0.1% a side dominates, so a full weekly
rotation spends roughly 17% of the account a year, monthly ~4%, quarterly ~1.3%.
The headline deliverable is therefore the *net-return-versus-frequency frontier*,
not any single rule.

What is measured
----------------
* Signals: trailing return over 1/3/6/12 months, the classic 12-1 (12 month
  return skipping the most recent month), 6-1, inverse trailing volatility, and
  distance from the 52-week high. All are computed from closes strictly prior to
  the rebalance date, and ``run_weights`` lags the book one more session.
* Rebalance: weekly, fortnightly, monthly, quarterly.
* Basket: top K in {5, 10, 20, 30}, equal weight, with inverse-volatility
  sizing and a 2K hysteresis buffer as variants on the better half of the grid.

Rules of evidence
-----------------
* The universe is **survivorship-biased**: the long-history files are today's
  members, so names that failed are absent. No absolute level printed here is
  achievable. The only quotable number is the gap against the *same-universe*
  equal-weight buy-and-hold, which carries the identical bias.
* The 200-name liquid universe is chosen on an **early sub-window** (the first
  250 sessions, calendar 2015) and trading starts after it, so the liquidity
  screen never sees the future.
* Every variant is counted; the best one is passed through ``deflated_sharpe``.
* Every result is reported in both halves via ``split_halves``. A rule that
  works in one half only is recorded as a failure.

Usage::

    .venv/Scripts/python.exe scripts/hunt_stock_momentum.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from atr.research.hunt import (
    ETF_COSTS,
    STOCK_COSTS,
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
DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
OUT = ROOT / "data" / "research" / "stock_momentum.json"

MIN_BARS = 1500
LIQUIDITY_BARS = 250      # early sub-window used to pick the liquid names
UNIVERSE_SIZE = 200
WARMUP_BARS = 252         # a year of signal history before the first trade

KS = (5, 10, 20, 30)
FREQS = {"weekly": 5, "fortnightly": 10, "monthly": 21, "quarterly": 63}


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
def early_liquidity(symbols: list[str], bars: int = LIQUIDITY_BARS) -> pd.Series:
    """Median daily traded value over each name's FIRST ``bars`` sessions.

    Using the earliest window is what keeps the screen honest: the backtest only
    starts once this window is over, so nothing about the future leaks into who
    is in the universe.
    """
    out: dict[str, float] = {}
    for symbol in symbols:
        path = DAILY / f"{symbol}.parquet"
        try:
            frame = pd.read_parquet(path, columns=["ts", "close", "volume"])
        except Exception:  # noqa: BLE001 - a corrupt file is simply not liquid
            continue
        frame = frame.dropna(subset=["close", "volume"]).sort_values("ts").head(bars)
        if len(frame) < bars:
            continue
        out[symbol] = float((frame["close"] * frame["volume"]).median())
    return pd.Series(out).sort_values(ascending=False)


def build_panel() -> tuple[pd.DataFrame, pd.Timestamp, list[str]]:
    """Closes for the liquid long-history names, trimmed of partial trailing bars."""
    symbols = stock_universe(min_bars=MIN_BARS)
    liquidity = early_liquidity(symbols)
    chosen = list(liquidity.head(UNIVERSE_SIZE).index)

    panel = load_panel(chosen, min_bars=MIN_BARS)
    # The broker cache's last day or two is often a partial snapshot; a bar where
    # most names are missing would fabricate a rebalance out of nothing.
    coverage = panel.notna().sum(axis=1)
    panel = panel[coverage >= 0.8 * coverage.median()]
    start = panel.index[min(LIQUIDITY_BARS + WARMUP_BARS, len(panel) - 1)]
    return panel, start, chosen


# ---------------------------------------------------------------------------
# Signals — every one uses only data up to and including the rebalance date,
# and run_weights then holds them from the NEXT session.
# ---------------------------------------------------------------------------
def build_signals(panel: pd.DataFrame) -> dict[str, pd.DataFrame]:
    daily = panel.pct_change()
    signals: dict[str, pd.DataFrame] = {}
    for months, lag in ((1, 21), (3, 63), (6, 126), (12, 252)):
        signals[f"mom{months}m"] = panel / panel.shift(lag) - 1.0
    for months, lag in ((6, 126), (12, 252)):
        # Skip the most recent month: the classic short-term-reversal guard.
        signals[f"mom{months}m_skip1"] = panel.shift(21) / panel.shift(lag) - 1.0
    vol = daily.rolling(126, min_periods=100).std()
    signals["lowvol"] = -vol                      # high rank == low volatility
    signals["near52whigh"] = panel / panel.rolling(252, min_periods=200).max() - 1.0
    return signals


def inverse_vol(panel: pd.DataFrame) -> pd.DataFrame:
    return 1.0 / panel.pct_change().rolling(63, min_periods=40).std()


# ---------------------------------------------------------------------------
# Weight construction
# ---------------------------------------------------------------------------
def rebalance_dates(index: pd.DatetimeIndex, step: int, start: pd.Timestamp) -> pd.DatetimeIndex:
    usable = index[index >= start]
    return usable[::step]


def make_weights(
    panel: pd.DataFrame,
    signal: pd.DataFrame,
    *,
    k: int,
    step: int,
    start: pd.Timestamp,
    sizing: str = "equal",
    hysteresis: bool = False,
    invvol: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Top-K basket on each rebalance date.

    ``hysteresis`` keeps an incumbent until it falls out of the top 2K, which
    trades signal freshness for turnover — the whole point of the brake.
    """
    dates = rebalance_dates(pd.DatetimeIndex(panel.index), step, start)
    rows: dict[pd.Timestamp, dict[str, float]] = {}
    held: list[str] = []

    for date in dates:
        scores = signal.loc[date].dropna()
        scores = scores[panel.loc[date].notna().reindex(scores.index, fill_value=False)]
        if len(scores) < 2 * k:
            continue
        ranked = scores.sort_values(ascending=False)
        top_k = list(ranked.index[:k])

        if hysteresis and held:
            buffer = set(ranked.index[: 2 * k])
            keep = [s for s in held if s in buffer]
            slots = k - len(keep)
            fresh = [s for s in top_k if s not in keep][:slots]
            picks = keep + fresh
            if len(picks) < k:  # top-K exhausted; top up from the buffer
                extra = [s for s in ranked.index[: 2 * k] if s not in picks]
                picks += extra[: k - len(picks)]
        else:
            picks = top_k

        if sizing == "invvol" and invvol is not None:
            raw = invvol.loc[date, picks].replace([np.inf, -np.inf], np.nan).dropna()
            if len(raw) < k // 2:
                raw = pd.Series(1.0, index=picks)
            weights = raw / raw.sum()
        else:
            weights = pd.Series(1.0 / len(picks), index=picks)

        rows[date] = weights.to_dict()
        held = list(picks)

    frame = pd.DataFrame.from_dict(rows, orient="index").fillna(0.0)
    return frame.reindex(columns=panel.columns, fill_value=0.0)


def equal_weight_hold(panel: pd.DataFrame, start: pd.Timestamp) -> pd.DataFrame:
    """Same-universe equal-weight buy-and-hold: the only honest comparator."""
    first = panel.index[panel.index >= start][0]
    live = panel.loc[first].dropna().index
    row = pd.Series(0.0, index=panel.columns)
    row[live] = 1.0 / len(live)
    return pd.DataFrame([row.to_dict()], index=[first])


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------
def slice_result(result: Result, index: pd.DatetimeIndex) -> Result:
    """The same run measured over a sub-period, so halves share one backtest."""
    keep = result.returns.index.isin(index)
    net = result.returns[keep]
    return Result(
        equity=(1 + net).cumprod(),
        returns=net,
        turnover=result.turnover[keep],
        costs=result.costs,
        gross_returns=result.gross_returns[keep],
    )


def evaluate(panel: pd.DataFrame, weights: pd.DataFrame, costs=STOCK_COSTS) -> dict:
    full = run_weights(panel, weights, costs)
    early, late = split_halves(panel)
    return {
        "full": full.metrics(),
        "half1": slice_result(full, early.index).metrics(),
        "half2": slice_result(full, late.index).metrics(),
        "_result": full,
    }


def strip(record: dict) -> dict:
    return {k: v for k, v in record.items() if not k.startswith("_")}


def main() -> None:
    panel, start, chosen = build_panel()
    traded = panel[panel.index >= start]
    signals = build_signals(panel)
    invvol = inverse_vol(panel)

    print(f"universe: {len(chosen)} liquid names chosen on the first {LIQUIDITY_BARS} sessions")
    print(f"traded window: {traded.index[0].date()} -> {traded.index[-1].date()} ({len(traded)} bars)")
    print(f"stock round trip: {STOCK_COSTS.round_trip_pct():.3f}% of turnover\n")

    # --- benchmarks ---------------------------------------------------------
    bench = evaluate(panel, equal_weight_hold(panel, start))
    print("same-universe equal-weight buy-and-hold (SURVIVORSHIP-BIASED level):")
    print("  full ", bench["full"])

    nifty = load_panel(["NIFTYBEES"], etf=True, min_bars=1000)
    nifty = nifty[(nifty.index >= traded.index[0]) & (nifty.index <= traded.index[-1])]
    nifty_res = buy_and_hold(nifty, "NIFTYBEES", ETF_COSTS)
    nifty_metrics = nifty_res.metrics()
    print("  NIFTYBEES over the same window:", nifty_metrics, "\n")

    # --- the grid -----------------------------------------------------------
    records: list[dict] = []
    trials = 0
    for name, signal in signals.items():
        for freq, step in FREQS.items():
            for k in KS:
                w = make_weights(panel, signal, k=k, step=step, start=start)
                rec = evaluate(panel, w)
                rec.update(signal=name, freq=freq, k=k, sizing="equal", hysteresis=False)
                records.append(rec)
                trials += 1
        print(f"  swept {name}: {trials} variants so far")

    # --- variants on the plain grid: turnover brake and volatility sizing ----
    base = sorted(records, key=lambda r: r["full"]["sharpe"], reverse=True)
    seeds = []
    seen = set()
    for rec in base:
        key = (rec["signal"], rec["freq"])
        if key in seen:
            continue
        seen.add(key)
        seeds.append(rec)
        if len(seeds) >= 12:
            break

    for rec in seeds:
        signal = signals[rec["signal"]]
        step = FREQS[rec["freq"]]
        for sizing, hyst in (("equal", True), ("invvol", False), ("invvol", True)):
            w = make_weights(
                panel, signal, k=rec["k"], step=step, start=start,
                sizing=sizing, hysteresis=hyst, invvol=invvol,
            )
            out = evaluate(panel, w)
            out.update(signal=rec["signal"], freq=rec["freq"], k=rec["k"],
                       sizing=sizing, hysteresis=hyst)
            records.append(out)
            trials += 1
    print(f"  variants done: {trials} total\n")

    # --- frontier: best net CAGR and Sharpe at each frequency ----------------
    frontier = {}
    for freq in FREQS:
        at_freq = [r for r in records if r["freq"] == freq]
        best_c = max(at_freq, key=lambda r: r["full"]["cagr_pct"])
        best_s = max(at_freq, key=lambda r: r["full"]["sharpe"])
        frontier[freq] = {
            "median_net_cagr_pct": round(float(np.median([r["full"]["cagr_pct"] for r in at_freq])), 2),
            "median_cost_drag_pct_yr": round(float(np.median([r["full"]["cost_drag_pct_yr"] for r in at_freq])), 2),
            "median_turnover_per_yr": round(float(np.median([r["full"]["turnover_per_yr"] for r in at_freq])), 1),
            "best_cagr": {"rule": _label(best_c), **_slim(best_c)},
            "best_sharpe": {"rule": _label(best_s), **_slim(best_s)},
        }

    # --- the winner: must beat the same-universe benchmark in BOTH halves ----
    def robust(rec: dict) -> bool:
        return (
            rec["half1"]["sharpe"] > bench["half1"]["sharpe"]
            and rec["half2"]["sharpe"] > bench["half2"]["sharpe"]
            and rec["half1"]["cagr_pct"] > bench["half1"]["cagr_pct"]
            and rec["half2"]["cagr_pct"] > bench["half2"]["cagr_pct"]
        )

    survivors = [r for r in records if robust(r)]
    ranked = sorted(records, key=lambda r: min(r["half1"]["sharpe"], r["half2"]["sharpe"]), reverse=True)
    best = (sorted(survivors, key=lambda r: min(r["half1"]["sharpe"], r["half2"]["sharpe"]), reverse=True)
            or ranked)[0]

    n_obs = len(best["_result"].returns.dropna())
    dsr = deflated_sharpe(best["full"]["sharpe"], trials, n_obs)
    comparison = compare(_label(best), best["_result"], bench["_result"])

    payload = {
        "meta": {
            "track": "B - cross-sectional stock selection on NSE cash equity",
            "survivorship_warning": (
                "Universe is today's long-history NSE names; failed names are absent. "
                "Absolute levels here are NOT achievable. Only the gap against the "
                "same-universe equal-weight buy-and-hold is quotable."
            ),
            "universe_size": len(chosen),
            "universe_selected_on": f"first {LIQUIDITY_BARS} sessions by median traded value",
            "traded_from": str(traded.index[0].date()),
            "traded_to": str(traded.index[-1].date()),
            "stock_round_trip_pct": round(STOCK_COSTS.round_trip_pct(), 4),
            "trials": trials,
            "variants_beating_benchmark_in_both_halves": len(survivors),
        },
        "benchmarks": {
            "same_universe_equal_weight_buy_and_hold": strip(bench),
            "niftybees_same_window": nifty_metrics,
        },
        "frontier_by_rebalance_frequency": frontier,
        "best_rule": {
            "rule": _label(best),
            "passed_both_halves": robust(best),
            "full": best["full"],
            "half1": best["half1"],
            "half2": best["half2"],
            "excess_over_same_universe_benchmark": {
                "full_cagr_pct": round(best["full"]["cagr_pct"] - bench["full"]["cagr_pct"], 2),
                "half1_cagr_pct": round(best["half1"]["cagr_pct"] - bench["half1"]["cagr_pct"], 2),
                "half2_cagr_pct": round(best["half2"]["cagr_pct"] - bench["half2"]["cagr_pct"], 2),
                "full_sharpe_gap": round(best["full"]["sharpe"] - bench["full"]["sharpe"], 3),
                "half1_sharpe_gap": round(best["half1"]["sharpe"] - bench["half1"]["sharpe"], 3),
                "half2_sharpe_gap": round(best["half2"]["sharpe"] - bench["half2"]["sharpe"], 3),
            },
            "deflated_sharpe": round(dsr, 3),
            "compare": comparison,
        },
        "all_variants": [dict(strip(r), rule=_label(r)) for r in records],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    _report(payload, bench)
    print(f"\nwrote {OUT}")


def _label(rec: dict) -> str:
    tail = f"{rec['sizing']}" + ("+hysteresis" if rec["hysteresis"] else "")
    return f"{rec['signal']}/top{rec['k']}/{rec['freq']}/{tail}"


def _slim(rec: dict) -> dict:
    keys = ("cagr_pct", "sharpe", "max_drawdown_pct", "turnover_per_yr", "cost_drag_pct_yr")
    return {"full": {k: rec["full"][k] for k in keys},
            "half1": {k: rec["half1"][k] for k in keys},
            "half2": {k: rec["half2"][k] for k in keys}}


def _report(payload: dict, bench: dict) -> None:
    print("\nNET RETURN vs REBALANCE FREQUENCY (survivorship-biased levels)")
    print(f"{'freq':<13}{'med net':>9}{'med drag':>10}{'med turn':>10}{'best net':>10}  best rule")
    for freq, row in payload["frontier_by_rebalance_frequency"].items():
        b = row["best_cagr"]
        print(f"{freq:<13}{row['median_net_cagr_pct']:>9.2f}{row['median_cost_drag_pct_yr']:>10.2f}"
              f"{row['median_turnover_per_yr']:>10.1f}{b['full']['cagr_pct']:>10.2f}  {b['rule']}")
    print(f"{'EW hold':<13}{bench['full']['cagr_pct']:>9.2f}{bench['full']['cost_drag_pct_yr']:>10.2f}"
          f"{bench['full']['turnover_per_yr']:>10.1f}")

    best = payload["best_rule"]
    print(f"\nBEST: {best['rule']}  (passed both halves: {best['passed_both_halves']})")
    for part in ("full", "half1", "half2"):
        m = best[part]
        print(f"  {part:<6} cagr {m['cagr_pct']:>7.2f}  sharpe {m['sharpe']:>6.2f}  "
              f"maxDD {m['max_drawdown_pct']:>7.2f}  turn {m['turnover_per_yr']:>6.1f}  "
              f"drag {m['cost_drag_pct_yr']:>5.2f}")
    print("  excess over same-universe EW:", best["excess_over_same_universe_benchmark"])
    print(f"  deflated sharpe {best['deflated_sharpe']} over {payload['meta']['trials']} trials")


if __name__ == "__main__":
    main()
