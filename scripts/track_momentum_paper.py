"""Forward paper-trading record for the weekly momentum rule.

The backtest says the top-10 momentum basket averages about +0.70% per week with
roughly a 20% chance that any single pick gains 5%. Every one of those numbers
was measured on history that the rule was selected against, so none of them can
confirm the rule is still alive. Only forward data can, and forward data only
accumulates if something writes it down every week.

This does that. Two append-only files:

* ``data/paper_momentum/picks.jsonl`` — one record per week: the names, their
  entry prices, and the backtest's expectation, written **before** the outcome
  is known. Recording the expectation first is the point: it makes the forward
  result a test rather than a story told afterwards.
* ``data/paper_momentum/settlements.jsonl`` — one record per settled week: what
  each pick actually did, and the basket's realised return.

Both are append-only so the record survives a restart and cannot be quietly
revised. Nothing here trades; it is a measurement harness.

Usage::

    .venv/Scripts/python.exe scripts/track_momentum_paper.py          # settle + record
    .venv/Scripts/python.exe scripts/track_momentum_paper.py --status # running record
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
PICKS_JSON = ROOT / "data" / "self_learning" / "weekly_stock_picks.json"
LEDGER = ROOT / "data" / "paper_momentum"
PICKS_LOG = LEDGER / "picks.jsonl"
SETTLE_LOG = LEDGER / "settlements.jsonl"

#: The rule the weekly job actually applies, and whose stats it quotes: rank the
#: liquid universe by 26-week momentum, hold the top 10. The near-52w-high and
#: volume-surge conditions were tested and add nothing (lift 1.00–1.05x), so the
#: ranking is momentum alone — label the picks with the stats of *that* rule,
#: not of a filter the selection does not apply.
FILTER = "top_decile_ret_26w"
TARGET = 0.05
SETTLE_DAYS = 7


def load_close(symbol: str) -> pd.Series | None:
    path = DAILY / f"{symbol}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path, columns=["ts", "close"])
    except Exception:  # noqa: BLE001
        return None
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0].sort_values("ts")
    if df.empty:
        return None
    s = pd.Series(df["close"].to_numpy(), index=pd.DatetimeIndex(df["ts"]).normalize())
    return s[~s.index.duplicated(keep="last")]


def close_on_or_before(s: pd.Series, when: pd.Timestamp) -> float | None:
    sub = s.loc[s.index <= when]
    return float(sub.iloc[-1]) if len(sub) else None


def close_at(s: pd.Series, when: pd.Timestamp) -> float | None:
    """Close on the last session at or before ``when`` — but only if the data
    actually reaches ``when``.

    Falling back to the last available bar would return *today's* price as
    *next week's* price and report a clean 0.00% week. That is a missing value
    wearing a measurement's clothes, so this refuses instead.
    """
    if s.empty or s.index.max() < when:
        return None
    return close_on_or_before(s, when)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A half-written line is not a record. Skip it rather than
                # letting a parse error discard the whole history.
                continue
    return out


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def settle() -> int:
    """Score any recorded week whose outcome is now knowable."""
    picks = read_jsonl(PICKS_LOG)
    done = {r["week"] for r in read_jsonl(SETTLE_LOG)}
    n = 0
    for rec in picks:
        week = rec["week"]
        if week in done:
            continue
        entry_ts = pd.Timestamp(week)
        exit_ts = entry_ts + pd.Timedelta(days=SETTLE_DAYS)
        returns, missing = {}, []
        for p in rec["picks"]:
            s = load_close(p["symbol"])
            if s is None:
                missing.append(p["symbol"])
                continue
            entry = p["entry"]
            exit_px = close_at(s, exit_ts)
            if exit_px is None or not np.isfinite(exit_px) or entry <= 0:
                missing.append(p["symbol"])
                continue
            returns[p["symbol"]] = round(exit_px / entry - 1.0, 6)
        if not returns:
            # Not settleable yet. Leave it unsettled rather than writing a
            # 0.00% week that would look like a flat outcome.
            print(f"  {week}: not settleable yet (no data through {exit_ts.date()})")
            continue
        vals = np.array(list(returns.values()))
        append_jsonl(SETTLE_LOG, {
            "week": week,
            "settled_at": datetime.now().isoformat(timespec="seconds"),
            "exit_window_end": str(exit_ts.date()),
            "returns": returns,
            "n_settled": len(returns),
            "missing": missing,
            "portfolio_return": round(float(vals.mean()), 6),
            "hits_ge_5pct": int((vals >= TARGET).sum()),
            "hit_rate_ge_5pct": round(float((vals >= TARGET).mean()), 4),
            "best": round(float(vals.max()), 4),
            "worst": round(float(vals.min()), 4),
        })
        n += 1
        print(f"  settled {week}: basket {vals.mean() * 100:+.2f}%  "
              f"{(vals >= TARGET).sum()}/{len(vals)} picks ≥5%  "
              f"(best {vals.max() * 100:+.1f}%, worst {vals.min() * 100:+.1f}%)")
    return n


def record() -> int:
    """Log the current week's picks with their entry prices, before the outcome."""
    if not PICKS_JSON.exists():
        print("no picks file — run research_weekly_stock_picks.py first", file=sys.stderr)
        return 0
    data = json.loads(PICKS_JSON.read_text(encoding="utf8"))
    week = data.get("latest_week")
    if not week:
        return 0
    existing = {r["week"] for r in read_jsonl(PICKS_LOG)}
    if week in existing:
        print(f"  {week} already recorded")
        return 0

    entry = data["picks"].get(FILTER)
    if not entry:
        print(f"  filter {FILTER} absent from picks file", file=sys.stderr)
        return 0
    hist = data["filters"].get(FILTER, {})
    ts = pd.Timestamp(week)
    picks = []
    for p in entry["names"]:
        s = load_close(p["symbol"])
        px = close_at(s, ts) if s is not None else None
        if px is None or not np.isfinite(px) or px <= 0:
            print(f"  no entry price for {p['symbol']} — skipped", file=sys.stderr)
            continue
        picks.append({"symbol": p["symbol"], "entry": round(px, 4)})
    if not picks:
        return 0

    append_jsonl(PICKS_LOG, {
        "week": week,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "filter": FILTER,
        "n_picks": len(picks),
        "picks": picks,
        # The expectation is written down now, before the outcome exists. A
        # prediction recorded after the fact is not a prediction.
        "backtest_expectation": {
            "mean_weekly_pct": hist.get("mean_next_week_pct"),
            "hit_rate_ge_5pct": hist.get("hit_rate_ge_5pct"),
            "pct_le_minus5pct": hist.get("pct_le_minus5pct"),
            "source": "weekly_stock_picks.json filters." + FILTER,
        },
    })
    print(f"  recorded {week}: {len(picks)} picks — "
          + ", ".join(p["symbol"] for p in picks))
    return 1


def status() -> None:
    picks = {r["week"]: r for r in read_jsonl(PICKS_LOG)}
    settles = read_jsonl(SETTLE_LOG)
    if not picks:
        print("no paper record yet")
        return
    print(f"paper record: {len(picks)} weeks recorded, {len(settles)} settled")
    if not settles:
        print("nothing settled yet — the first outcome needs one week of data")
        for w in sorted(picks):
            print(f"  {w}: awaiting outcome")
        return

    rets = [s["portfolio_return"] for s in settles]
    hits = [s["hit_rate_ge_5pct"] for s in settles]
    # Backfilled weeks carry no expectation, so filter the Nones out rather
    # than crashing — and report how many settled weeks the comparison covers.
    exp_mean = [picks[s["week"]]["backtest_expectation"]["mean_weekly_pct"]
                for s in settles if s["week"] in picks
                and picks[s["week"]]["backtest_expectation"]["mean_weekly_pct"] is not None]
    exp_hit = [picks[s["week"]]["backtest_expectation"]["hit_rate_ge_5pct"]
               for s in settles if s["week"] in picks
               and picks[s["week"]]["backtest_expectation"]["hit_rate_ge_5pct"] is not None]

    equity = float(np.prod([1 + r for r in rets]))
    print(f"\n  weeks settled      {len(rets)}")
    print(f"  basket mean/week   {100 * statistics.fmean(rets):+.3f}%")
    print(f"  per-pick hit ≥5%   {100 * statistics.fmean(hits):.2f}%   "
          f"(backtest expects 20.17%)")
    print(f"  cumulative         {100 * (equity - 1):+.2f}%")
    print(f"  best / worst week  {100 * max(rets):+.2f}% / {100 * min(rets):+.2f}%")
    wins = sum(1 for r in rets if r > 0)
    print(f"  positive weeks     {wins}/{len(rets)}")
    if exp_mean:
        print(f"\n  vs backtest, on the {len(exp_mean)} live weeks that carry an "
              f"expectation:")
        print(f"    basket mean/week {100 * statistics.fmean(exp_mean):+.3f}%")
        print(f"    per-pick hit     {statistics.fmean(exp_hit):.2f}%")

    # A forward record needs ~a year before it can distinguish anything. Say so
    # rather than letting a handful of weeks read as confirmation.
    if len(rets) < 52:
        print(f"\n  NOTE: {len(rets)} of ~52 weeks needed. Too short to confirm or "
              f"refute the backtest — treat it as a running sanity check only.")
    print("\n  per week:")
    for s in sorted(settles, key=lambda x: x["week"]):
        print(f"    {s['week']}  {100 * s['portfolio_return']:+6.2f}%  "
              f"{s['hits_ge_5pct']}/{s['n_settled']} ≥5%")


def backfill(n_weeks: int) -> int:
    """Write picks for the last N historical weeks, so the settlement path is exercised.

    These weeks are **in-sample** — the rule was selected on this history — so
    they are not evidence about whether the rule still works. They are here to
    prove the harness settles correctly, and because they recompute the rule from
    the panel and then settle it from the *daily cache*: if the two agree, both
    the panel and this ledger are validated against each other.
    """
    panel_path = ROOT / "data" / "self_learning" / "stock_week_panel.parquet"
    if not panel_path.exists():
        print("no panel — run research_weekly_stock_picks.py --rebuild", file=sys.stderr)
        return 0
    panel = pd.read_parquet(panel_path)
    panel = panel[np.isfinite(panel["next_ret"]) & ~panel["extreme"]]
    weeks = sorted(panel["week"].unique())[-n_weeks:]
    existing = {r["week"] for r in read_jsonl(PICKS_LOG)}
    n = 0
    for wk in weeks:
        week = str(pd.Timestamp(wk).date())
        if week in existing:
            continue
        grp = panel[panel["week"] == wk].copy()
        grp = grp.dropna(subset=["ret_26w", "vol_surge", "near_52w_high"])
        # The same rule the live job applies: rank the liquid universe by
        # 26-week momentum and hold the top 10. Applying all three quantile
        # masks here would leave ~0.1% of the universe and silently backfill
        # nothing — which is what the first attempt did.
        if len(grp) < 10:
            continue
        picks = [
            {"symbol": r["symbol"], "entry": round(float(r["close"]), 4),
             "panel_next_ret": round(float(r["next_ret"]), 6)}
            for _, r in grp.nlargest(10, "ret_26w").iterrows()
        ]
        append_jsonl(PICKS_LOG, {
            "week": week,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "filter": FILTER,
            "source": "backfill (in-sample — harness validation only)",
            "n_picks": len(picks),
            "picks": picks,
            "backtest_expectation": {
                "mean_weekly_pct": None, "hit_rate_ge_5pct": None,
                "source": "n/a — backfilled week",
            },
        })
        n += 1
    print(f"  backfilled {n} weeks")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--backfill", type=int, default=0,
                    help="write picks for the last N historical weeks to exercise "
                         "the settlement path (in-sample; harness validation only)")
    args = ap.parse_args()
    LEDGER.mkdir(parents=True, exist_ok=True)
    if args.status:
        status()
        return 0
    if args.backfill:
        backfill(args.backfill)
    print("settling outstanding weeks…")
    settle()
    print("recording the current week…")
    record()
    print()
    status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
