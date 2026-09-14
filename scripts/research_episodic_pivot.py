"""Does the Episodic Pivot's *premise* exist on the tested universe?

Why this runs before any backtest
---------------------------------
The playbook is explicit about the raw material it needs: a stock that has been
"ignored for a long time", a catalyst that arrives as "gaps of 20–40% or more",
and repricing of "50–300% or more in a short period", "often in as little as
10–20 trading days". Its worked examples are SMCI (+189% in 28 sessions), ROOT
(+358% in 16) and ANF (+240%) — all small, genuinely neglected US companies.

Those are measurable claims about a *pond*. If the pond does not contain the
fish, no amount of careful entry logic will produce the trade, and a backtest
returning zero trades would be misread as "the rules are wrong" rather than
"the universe cannot express the setup". So this script measures the premise
first, and measures it on more than one universe, because the interesting
comparison is Nifty 50 against the mid-cap pond the playbook actually fishes in.

What is measured, per universe
------------------------------
* **Gap frequency** — how often a session opens >= 3 / 5 / 10 / 20% above the
  prior close.
* **Volume-spike frequency** — how often volume is >= 3 / 5 / 10 × its own
  trailing 20-session average.
* **Joint catalyst frequency** — gap >= 5% *and* volume >= 3×, the two-pillar
  trigger every EP rule in ``episodic_pivot.py`` is built on.
* **Quiet-tape frequency** — share of sessions where 120-session realised
  volatility is under 2.5%/day, the playbook's "neglect".
* **The payoff, measured not quoted** — forward 10- and 20-session return after
  a joint catalyst, versus the unconditional distribution over the same bars.
  This is the EP bet: does the catalyst day actually predict anything?
* **Tail availability** — the best 20-session return anywhere in the history,
  and how many 20-session windows exceeded +50% / +100%. The playbook's headline
  numbers live out here.

Usage::

    .venv/Scripts/python.exe scripts/research_episodic_pivot.py
    .venv/Scripts/python.exe scripts/research_episodic_pivot.py --universe midcap150
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = ROOT / "data" / "iifl_daily" / "NSEEQ"
UNIVERSE_DIR = ROOT / "data" / "universe"

UNIVERSES = {
    "nifty50": UNIVERSE_DIR / "ind_nifty50list.csv",
    "next50": UNIVERSE_DIR / "ind_niftynext50list.csv",
    "midcap150": UNIVERSE_DIR / "ind_niftymidcap150list.csv",
    "smallcap250": UNIVERSE_DIR / "ind_niftysmallcap250list.csv",
}


def universe_symbols(name: str) -> list[str]:
    path = UNIVERSES[name]
    with path.open(encoding="utf-8-sig") as handle:
        return [r["Symbol"].strip().upper() for r in csv.DictReader(handle) if r.get("Symbol")]


def load(symbols: list[str], min_bars: int) -> dict[str, pd.DataFrame]:
    from atr.data.hygiene import drop_reverting_spikes

    out: dict[str, pd.DataFrame] = {}
    dropped_total = 0
    for symbol in symbols:
        path = CACHE_ROOT / f"{symbol}.parquet"
        if not path.exists():
            continue
        frame = pd.read_parquet(path)
        frame.columns = [c.strip().lower() for c in frame.columns]
        if "ts" not in frame.columns or len(frame) < min_bars:
            continue
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        frame = (
            frame.dropna(subset=["open", "high", "low", "close"])
            .sort_values("ts")
            .drop_duplicates("ts", keep="last")
            .reset_index(drop=True)
        )
        # A 3x bar on a quiet tape *is* an Episodic Pivot as far as any of these
        # statistics can tell. Left in, the vendor's 2021-09-15 glitch would be
        # reported as the best 20-session move in the index.
        frame, dropped = drop_reverting_spikes(frame)
        dropped_total += len(dropped)
        if len(frame) >= min_bars:
            out[symbol] = frame
    if dropped_total:
        print(f"  dropped {dropped_total} corrupt bar(s) — see atr.data.hygiene")
    return out


def enrich(frame: pd.DataFrame) -> pd.DataFrame:
    """The same columns ``episodic_pivot.py`` builds, computed independently.

    Deliberately duplicated rather than imported: this script is the check on
    that module, and a check that calls the thing it is checking proves nothing.
    """
    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float).where(lambda v: v > 0)
    prev = close.shift(1)
    out = pd.DataFrame(index=frame.index)
    out["gap_pct"] = (frame["open"].astype(float) / prev - 1.0) * 100.0
    out["ret_1d"] = close.pct_change() * 100.0
    out["vol_ratio"] = volume / volume.rolling(20, min_periods=10).mean().shift(1)
    out["vol_120"] = close.pct_change().rolling(120, min_periods=60).std() * 100.0
    out["turnover_cr"] = volume * close / 1e7
    out["fwd_10"] = close.shift(-10) / close - 1.0
    out["fwd_20"] = close.shift(-20) / close - 1.0
    out["fwd_60"] = close.shift(-60) / close - 1.0
    out["roll20_max"] = close.shift(-1).rolling(20).max().shift(-19) / close - 1.0
    return out


def pct(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


def describe(name: str, series: dict[str, pd.DataFrame], quiet_max: float = 2.5) -> dict:
    frames = {s: enrich(f) for s, f in series.items()}
    stack = pd.concat(frames.values(), keys=frames.keys(), names=["symbol", "i"])
    stack = stack.reset_index(level=0)
    valid = stack.dropna(subset=["gap_pct", "vol_120"])
    n = len(valid)

    gaps = {t: int((valid["gap_pct"] >= t).sum()) for t in (3, 5, 10, 20)}
    spikes = {t: int((valid["vol_ratio"] >= t).sum()) for t in (3, 5, 10)}
    catalyst = valid[(valid["gap_pct"] >= 5) & (valid["vol_ratio"] >= 3)]
    quiet = valid[valid["vol_120"] <= quiet_max]

    fwd_all = valid["fwd_20"].dropna()
    fwd_cat = catalyst["fwd_20"].dropna()
    fwd_quiet_cat = catalyst[catalyst["vol_120"] <= quiet_max]["fwd_20"].dropna()

    roll20 = valid["roll20_max"].dropna()
    # `valid` is a stack, so its row index repeats across symbols: `.loc[idxmax()]`
    # selects *every* symbol's row at that position and `.iloc[0]` then reports
    # whichever symbol happens to sit first — a wrong answer that looks like a
    # measurement. Positional argmax is the only unambiguous form here.
    best_symbol, best_move = None, None
    if len(roll20):
        pos = int(np.nanargmax(valid["roll20_max"].to_numpy(dtype=float)))
        best_symbol = str(valid["symbol"].iloc[pos])
        best_move = round(float(valid["roll20_max"].iloc[pos]) * 100, 1)

    return {
        "universe": name,
        "symbols": len(series),
        "symbol_bars": n,
        "window": f"{min(f['ts'].min() for f in series.values()):%Y-%m-%d} → "
                  f"{max(f['ts'].max() for f in series.values()):%Y-%m-%d}",
        "gap_days": gaps,
        "gap_pct_of_days": {k: round(pct(v, n), 3) for k, v in gaps.items()},
        "volume_spike_days": spikes,
        "volume_spike_pct_of_days": {k: round(pct(v, n), 3) for k, v in spikes.items()},
        "joint_catalyst_days": int(len(catalyst)),
        "joint_catalyst_pct_of_days": round(pct(len(catalyst), n), 3),
        "quiet_tape_pct_of_days": round(pct(len(quiet), n), 2),
        "quiet_and_catalyst_days": int(len(catalyst[catalyst["vol_120"] <= quiet_max])),
        "fwd20_unconditional_pct": round(float(fwd_all.mean()) * 100, 2) if len(fwd_all) else None,
        "fwd20_after_catalyst_pct": round(float(fwd_cat.mean()) * 100, 2) if len(fwd_cat) else None,
        "fwd20_after_quiet_catalyst_pct": (
            round(float(fwd_quiet_cat.mean()) * 100, 2) if len(fwd_quiet_cat) else None
        ),
        "fwd20_after_catalyst_hit_rate": (
            round(float((fwd_cat > 0).mean()) * 100, 1) if len(fwd_cat) else None
        ),
        "fwd20_after_catalyst_n": int(len(fwd_cat)),
        "best_20d_move_pct": best_move,
        "best_20d_symbol": best_symbol,
        "windows_20d_over_50pct": int((roll20 > 0.50).sum()),
        "windows_20d_over_100pct": int((roll20 > 1.00).sum()),
        "windows_20d_over_50pct_share": round(pct(int((roll20 > 0.50).sum()), len(roll20)), 4),
        "one_day_moves_over_10pct": int((valid["ret_1d"].abs() >= 10).sum()),
        "one_day_moves_over_20pct": int((valid["ret_1d"].abs() >= 20).sum()),
    }


def render(reports: list[dict]) -> str:
    lines: list[str] = []
    for r in reports:
        g, gp = r["gap_days"], r["gap_pct_of_days"]
        v, vp = r["volume_spike_days"], r["volume_spike_pct_of_days"]
        lines += [
            f"── {r['universe']} " + "─" * max(0, 62 - len(r["universe"])),
            f"  {r['symbols']} symbols, {r['symbol_bars']:,} symbol-bars, {r['window']}",
            "",
            "  gap up on the open, share of all sessions",
            f"    >= 3%  : {g[3]:>6,}  ({gp[3]:.2f}%)",
            f"    >= 5%  : {g[5]:>6,}  ({gp[5]:.2f}%)",
            f"    >= 10% : {g[10]:>6,}  ({gp[10]:.2f}%)   ← playbook calls 20–40% routine",
            f"    >= 20% : {g[20]:>6,}  ({gp[20]:.2f}%)",
            "",
            "  volume vs its own trailing 20-session average",
            f"    >= 3x  : {v[3]:>6,}  ({vp[3]:.2f}%)",
            f"    >= 5x  : {v[5]:>6,}  ({vp[5]:.2f}%)",
            f"    >= 10x : {v[10]:>6,}  ({vp[10]:.2f}%)",
            "",
            "  the EP trigger (gap >= 5% AND volume >= 3x)",
            f"    sessions            : {r['joint_catalyst_days']:,} "
            f"({r['joint_catalyst_pct_of_days']:.3f}% of all)",
            f"    of those, quiet tape: {r['quiet_and_catalyst_days']:,} "
            f"(120d vol <= 2.5%/day; quiet tape is {r['quiet_tape_pct_of_days']:.1f}% of sessions)",
            "",
            "  does the catalyst predict anything? (forward 20-session return)",
            f"    unconditional       : {r['fwd20_unconditional_pct']:+.2f}%",
            f"    after a catalyst    : {r['fwd20_after_catalyst_pct']:+.2f}% "
            f"over n={r['fwd20_after_catalyst_n']:,} "
            f"(up {r['fwd20_after_catalyst_hit_rate']:.1f}% of the time)",
            f"    after quiet+catalyst: {r['fwd20_after_quiet_catalyst_pct']:+.2f}%",
            "",
            "  the playbook's headline payoffs (best 20-session window, any symbol)",
            f"    best anywhere       : {r['best_20d_move_pct']:+.1f}% "
            f"({r['best_20d_symbol']})   — SMCI +189%, ROOT +358%, ANF +240%",
            f"    20d windows > +50%  : {r['windows_20d_over_50pct']:,} "
            f"({r['windows_20d_over_50pct_share']:.3f}% of windows)",
            f"    20d windows > +100% : {r['windows_20d_over_100pct']:,}",
            f"    1-day moves >= 10%  : {r['one_day_moves_over_10pct']:,}"
            f"   >= 20%: {r['one_day_moves_over_20pct']:,}",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="nifty50,midcap150",
                    help="comma-separated keys of data/universe/*.csv")
    ap.add_argument("--min-bars", type=int, default=1000)
    ap.add_argument("--quiet-max-vol", type=float, default=2.5,
                    help="120-session realised vol, %% per day, counted as 'neglect'")
    ap.add_argument("--out", default="data/self_learning/episodic_pivot_premise.json")
    args = ap.parse_args()

    reports: list[dict] = []
    for name in [n.strip() for n in args.universe.split(",") if n.strip()]:
        if name not in UNIVERSES:
            print(f"unknown universe {name!r}", file=sys.stderr)
            return 1
        series = load(universe_symbols(name), args.min_bars)
        if not series:
            print(f"{name}: no cached history — run scripts/fetch_long_history.py",
                  file=sys.stderr)
            continue
        print(f"  {name}: {len(series)} symbols with >= {args.min_bars} bars")
        reports.append(describe(name, series, args.quiet_max_vol))

    if not reports:
        return 1
    print()
    print(render(reports))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"generated_at": datetime.now().isoformat(timespec="seconds"),
             "quiet_max_vol": args.quiet_max_vol,
             "universes": reports},
            indent=2,
        ),
        encoding="utf8",
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
