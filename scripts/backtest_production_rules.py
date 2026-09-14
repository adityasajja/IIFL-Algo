"""Run the PRODUCTION entry rules through walk-forward and print one table.

The point of this script is to answer "can we trade this?" with numbers rather
than a narrative. It runs `SignalEntryStrategy` -- the same class the live
scanner and the backtester both use -- over real daily history, and reports for
each fold split:

  * the out-of-sample return and Sharpe (never in-sample)
  * buy & hold on the identical window and universe
  * the multiple-testing-corrected probability the edge is real
  * the verdict the harness reaches

Run:
    ./.venv/Scripts/python.exe scripts/backtest_production_rules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atr.backtest.costs import CommissionModel, SlippageModel  # noqa: E402
from atr.backtest.engine import BacktestConfig  # noqa: E402
from atr.data.base import ListFeed, pivot_to_snapshots  # noqa: E402
from atr.data.history import load_cached  # noqa: E402
from atr.research.validate import (  # noqa: E402
    ValidationConfig,
    WalkForwardConfig,
    walk_forward,
)
from atr.signals.strategy import SignalEntryStrategy  # noqa: E402

UNIVERSE = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK",
    "KOTAKBANK", "LT", "TITAN", "SUNPHARMA", "ULTRACEMCO", "MARUTI",
    "BHARTIARTL", "HCLTECH", "TATASTEEL", "WIPRO", "ADANIENT",
]

# Comma-separated "train:test" pairs to test how much the split matters.
SPLITS = [(500, 250), (400, 200), (600, 300), (700, 350)]


def build_feed():
    """One long panel of every symbol, then pivot to snapshots.

    `pivot_to_snapshots` needs a `symbol` column and an explicit timeframe, and
    `ListFeed` needs an instruments map -- `load_cached` frames carry neither.
    Every symbol must appear on every bar: `BacktestEngine._build_frames`
    NaN-pads any symbol missing at a step, so a sparse grid leaves
    `ctx.history(sym)` with no prices and the rules never fire -- which looks
    like "no edge".
    """
    import pandas as pd

    from atr.core.enums import Timeframe
    from atr.core.models import Instrument

    frames = load_cached("NSEEQ", UNIVERSE)
    if not frames:
        raise SystemExit("no cached history -- run scripts/fetch_long_history.py")

    # `load_cached` silently omits symbols it has no long-history file for.
    # Report that loudly: a quietly smaller universe changes the benchmark,
    # and a benchmark you did not notice changing is not a benchmark.
    dropped = [s for s in UNIVERSE if s not in frames]
    if dropped:
        print(f"NOTE: {len(dropped)} symbol(s) absent from the long-history "
              f"cache and therefore excluded: {', '.join(dropped)}")
        print("      (fetch them with scripts/fetch_long_history.py to "
              "include them)\n")

    parts = []
    for symbol, frame in frames.items():
        f = frame.copy()
        f["symbol"] = symbol
        parts.append(f)
    combined = pd.concat(parts, ignore_index=True)

    snapshots = pivot_to_snapshots(combined, Timeframe.DAY_1)
    instruments = {
        s: Instrument(symbol=s, exchange="NSEEQ") for s in frames
    }
    return ListFeed(snapshots, instruments), len(frames)


def main() -> int:
    feed, n_sym = build_feed()
    snaps = feed.load()
    print(f"universe: {n_sym}/{len(UNIVERSE)} symbols, {len(snaps)} sessions")
    first = snaps[0].ts
    last = snaps[-1].ts
    print(f"window  : {first:%Y-%m-%d} -> {last:%Y-%m-%d}\n")

    hdr = (f"{'train/test':>10} {'folds':>5} {'OOS ret':>9} {'OOS Shrp':>9} "
           f"{'B&H ret':>9} {'B&H Shrp':>9} {'MaxDD':>7} {'trades':>7} "
           f"{'P(real)':>8}  verdict")
    print(hdr)
    print("-" * len(hdr))

    rows = []
    bt = BacktestConfig(
        initial_cash=100_000.0,
        commission=CommissionModel(pct_of_notional=0.0003),
        slippage=SlippageModel(bps=5.0),
    )

    for train, test in SPLITS:
        result = walk_forward(
            feed,
            SignalEntryStrategy,
            {},
            config=WalkForwardConfig(train_bars=train, test_bars=test),
            validation=ValidationConfig(),
            backtest=bt,
        )
        oos = result.oos_metrics
        bench = result.benchmark_metrics
        passed = result.verdict.passed
        rows.append((train, test, oos, bench, result))

        print(
            f"{train:>4}/{test:<5} {len(result.folds):>5} "
            f"{oos.total_return_pct:>8.2f}% {oos.sharpe:>9.2f} "
            f"{bench.total_return_pct:>8.2f}% {bench.sharpe:>9.2f} "
            f"{oos.max_drawdown_pct:>6.2f}% {oos.num_trades:>7} "
            f"{result.deflated_sharpe:>8.3f}  "
            f"{'PASS' if passed else 'FAIL'}"
        )

    # ---- the two questions that actually decide it ----------------------
    print("\n" + "=" * 66)
    sharpes = [r[2].sharpe for r in rows]
    beats = sum(1 for r in rows if r[2].sharpe > r[3].sharpe)
    passes = sum(1 for r in rows if r[4].verdict.passed)

    print(f"OOS Sharpe across splits : {min(sharpes):.2f} .. {max(sharpes):.2f} "
          f"(spread {max(sharpes) - min(sharpes):.2f})")
    print(f"Beat buy & hold          : {beats}/{len(rows)} splits")
    print(f"Passed the harness       : {passes}/{len(rows)} splits")

    verdict = "DO NOT TRADE" if passes == 0 else "review further"
    print(f"\nCONCLUSION: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
