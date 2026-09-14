"""Score the Episodic Pivot rules out of sample, on the universe they are asked to trade.

Two questions, deliberately separated
-------------------------------------
1. ``scripts/research_episodic_pivot.py`` asks whether the playbook's *premise*
   exists on the universe (routine 20–40% gaps, 100–300% repricings). That is a
   property of the pond.
2. This script asks whether the *rules* extract anything from it — walk-forward,
   parameters chosen only on prior data, scored on windows they never saw,
   corrected for the number of combinations tried.

Keeping them apart matters because a null result has two very different causes.
If the premise is absent, "no trades" means the universe cannot express the
setup. If the premise is present and the rules still lose, the rules are wrong.
Reporting one number for both would hide which.

The control
-----------
Same symbols, same holding periods, same position sizes, same costs — only the
*entry date* is randomised. That is the sharp version of the question, because
the EP claim is specifically about timing: that the catalyst day is the moment
to be long. A control that also randomised the symbol or the holding period
would let the strategy win on selection or on duration while its timing
contributed nothing.

Usage::

    .venv/Scripts/python.exe scripts/validate_episodic_pivot.py
    .venv/Scripts/python.exe scripts/validate_episodic_pivot.py --universe nifty50 --control-runs 4
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pandas as pd  # noqa: E402

from atr.backtest.costs import SlippageModel  # noqa: E402
from atr.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from atr.execution.risk import RiskLimits  # noqa: E402
from atr.research.validate import (  # noqa: E402
    ValidationConfig,
    WalkForwardConfig,
    walk_forward,
)
from atr.strategy.base import Strategy  # noqa: E402
from atr.strategy.strategies import STRATEGIES  # noqa: E402
from research_episodic_pivot import UNIVERSES, universe_symbols  # noqa: E402
from validate_paper_strategies import build_feed, load_series  # noqa: E402

#: Parameters searched per variant. Each list is a real choice a trader makes;
#: every combination counts toward the multiple-testing hurdle, which is the
#: point — searching harder must make the bar higher, not the answer better.
PARAM_GRIDS: dict[str, dict[str, list]] = {
    "episodic_pivot_day1": {
        "gap_min": [3.0, 5.0, 8.0],
        "neglect_max_vol": [99.0, 2.0],
        "trail_bars": [5, 10],
        "be_trigger_r": [1.0, 2.0],
    },
    "episodic_pivot_delayed": {
        "gap_min": [3.0, 5.0],
        "vol_mult": [2.0, 3.0, 5.0],
        "delay_max": [10, 20],
        "trail_bars": [5, 10],
    },
    "episodic_pivot_9m": {
        "vol_mult": [2.0, 3.0, 5.0],
        "neglect_max_vol": [99.0, 2.5],
        "min_turnover_cr": [0.0, 50.0],
        "trail_bars": [5, 10],
    },
}


# ---------------------------------------------------------------------------
# The control: same everything except the entry date
# ---------------------------------------------------------------------------


def pilot_trades(feed, strategy_cls, backtest) -> pd.DataFrame:
    """One in-sample pass to learn the strategy's own trade shape.

    Used only to *characterise* the trades (which symbols, how long held, how
    big) so the control can be matched to them. No performance number from this
    pass is reported — it sees the whole history, so it is not evidence.
    """
    engine = BacktestEngine(feed, strategy_cls(), backtest)
    return engine.run().trades


def build_control_schedule(
    trades: pd.DataFrame, dates: list, seed: int
) -> dict:
    """Randomise each trade's entry date; keep symbol, duration and weight.

    Duration is measured in trading sessions so it survives a random date. The
    weight is the realised position notional as a fraction of equity at entry,
    which is what makes the control's risk identical rather than merely similar.
    """
    rng = random.Random(seed)
    schedule: dict = defaultdict(list)
    if trades is None or trades.empty:
        return schedule
    for row in trades.itertuples():
        symbol = getattr(row, "symbol", None)
        if symbol is None:
            continue
        hold = int(getattr(row, "duration_days", 0) or 0)
        if hold <= 0:
            continue
        entry_price = float(getattr(row, "entry_price", 0) or 0)
        quantity = float(getattr(row, "quantity", 0) or 0)
        notional = entry_price * quantity
        if notional <= 0:
            continue
        weight = min(max(notional / 1_000_000.0, 0.02), 0.30)
        when = rng.choice(dates)
        schedule[when].append((symbol, hold, weight))
    return schedule


def make_control_strategy(schedule: dict):
    """A strategy that is long the EP symbols at random dates, nothing else."""

    class MatchedRandomEntryControl(Strategy):
        name = "ep_control_matched"

        def __init__(self, **params) -> None:
            super().__init__(**params)
            self._exits: dict[int, list[str]] = defaultdict(list)
            self._open: dict[str, tuple[int, float]] = {}

        def on_bar(self, ctx) -> None:
            ts = pd.Timestamp(ctx.now)

            # --- exits scheduled for this bar --------------------------
            for symbol in self._exits.pop(ctx.index, []):
                if ctx.has_position(symbol):
                    ctx.close(symbol, tag="control-exit")
                self._open.pop(symbol, None)

            # --- entries scheduled for this bar ------------------------
            for symbol, hold, weight in schedule.get(ts, []):
                if ctx.has_position(symbol):
                    continue
                close = ctx.row(symbol).get("close")
                if close is None or not (close == close) or close <= 0:
                    continue
                equity = ctx.equity
                # `NaN <= 0` is False, so a bare comparison lets a NaN equity
                # through and it detonates as `int(nan)` — which is how three
                # of six control runs used to die.
                if equity is None or not (equity == equity) or equity <= 0:
                    continue
                instrument = ctx.instruments[symbol]
                step = max(getattr(instrument, "quantity_step", 1) or 1, 1)
                qty = int((equity * weight / float(close)) / step) * step
                if qty <= 0:
                    continue
                ctx.target(symbol, qty, tag="control-entry")
                self._open[symbol] = (ctx.index, float(close))
                self._exits[ctx.index + hold].append(symbol)

    return MatchedRandomEntryControl


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def measured(trades: pd.DataFrame) -> dict:
    """Win rate, payoff ratio and expectancy from the realised trade log."""
    if trades is None or trades.empty:
        return {"win_rate": None, "trades": 0, "payoff": None, "expectancy_r": None,
                "avg_win_pct": None, "avg_loss_pct": None, "best_pct": None,
                "median_hold": None}
    pnl = pd.to_numeric(trades["net_pnl"], errors="coerce").dropna()
    ret = pd.to_numeric(trades.get("return_pct"), errors="coerce").dropna()
    if pnl.empty:
        return {"win_rate": None, "trades": 0, "payoff": None, "expectancy_r": None,
                "avg_win_pct": None, "avg_loss_pct": None, "best_pct": None,
                "median_hold": None}
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    win_rate = len(wins) / len(pnl)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(abs(losses.mean())) if len(losses) else 0.0
    payoff = avg_win / avg_loss if avg_loss > 0 else None
    expectancy = win_rate * (payoff or 0.0) - (1.0 - win_rate)
    hold = pd.to_numeric(trades.get("duration_days"), errors="coerce").dropna()
    return {
        "win_rate": round(win_rate, 4),
        "trades": int(len(pnl)),
        "payoff": None if payoff is None else round(payoff, 3),
        "expectancy_r": round(expectancy, 4),
        "avg_win_pct": round(float(wins.mean() / 1_000_000.0), 4) if len(wins) else None,
        "avg_win_return_pct": round(float(ret[ret > 0].mean()), 2) if (ret > 0).any() else None,
        "avg_loss_return_pct": round(float(ret[ret <= 0].mean()), 2) if (ret <= 0).any() else None,
        "best_trade_return_pct": round(float(ret.max()), 1) if len(ret) else None,
        "worst_trade_return_pct": round(float(ret.min()), 1) if len(ret) else None,
        "median_hold_days": int(hold.median()) if len(hold) else None,
    }


def run_control(feed, dates, trades, backtest, cfg, val, runs: int) -> dict:
    sharpes, returns, trade_counts = [], [], []
    for seed in range(runs):
        schedule = build_control_schedule(trades, dates, seed=7000 + seed)
        cls = make_control_strategy(schedule)
        try:
            res = walk_forward(feed, cls, {}, config=cfg, backtest=backtest, validation=val)
        except Exception as exc:  # noqa: BLE001
            print(f"    control seed {seed} failed: {exc}")
            continue
        sharpes.append(res.oos_metrics.sharpe)
        returns.append(res.oos_metrics.total_return_pct)
        trade_counts.append(res.oos_metrics.num_trades)
    mean = statistics.fmean(sharpes) if sharpes else float("nan")
    sd = statistics.stdev(sharpes) if len(sharpes) > 1 else 0.0
    return {
        "runs": len(sharpes),
        "sharpe_mean": None if mean != mean else round(mean, 4),
        "sharpe_sd": round(sd, 4),
        "sharpes": [round(s, 4) for s in sharpes],
        "returns_pct": [round(r, 3) for r in returns],
        "return_mean_pct": round(statistics.fmean(returns), 3) if returns else None,
        "trades_mean": round(statistics.fmean(trade_counts), 1) if trade_counts else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="nifty50,midcap150")
    ap.add_argument("--variants", default=",".join(PARAM_GRIDS))
    ap.add_argument("--min-bars", type=int, default=1000)
    ap.add_argument("--cash", type=float, default=1_000_000.0)
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--train", type=int, default=400)
    ap.add_argument("--test", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=260)
    #: The playbook itself says "pure earnings/sales EPs only offer about 5–10
    #: trades a year". A high-frequency hurdle would fail EP by design rather
    #: than on merit, so the bar is set for a low-frequency model and the raw
    #: trade count is reported alongside it either way.
    ap.add_argument("--min-trades", type=int, default=25)
    ap.add_argument("--control-runs", type=int, default=6)
    ap.add_argument("--max-daily-loss-pct", type=float, default=25.0)
    ap.add_argument("--out", default="data/self_learning/episodic_pivot_validation.json")
    args = ap.parse_args()

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    config = WalkForwardConfig(
        train_bars=args.train, test_bars=args.test, warmup_bars=args.warmup
    )
    backtest = BacktestConfig(
        initial_cash=args.cash,
        slippage=SlippageModel(bps=args.slippage_bps),
        risk=RiskLimits(max_daily_loss=args.cash * args.max_daily_loss_pct / 100.0),
    )
    validation = ValidationConfig(min_trades=args.min_trades)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config": {
            "train_bars": args.train, "test_bars": args.test, "warmup_bars": args.warmup,
            "slippage_bps": args.slippage_bps, "initial_cash": args.cash,
            "min_trades": args.min_trades, "control_runs": args.control_runs,
            "max_daily_loss_pct": args.max_daily_loss_pct,
            "grids": PARAM_GRIDS,
            "note": (
                "fills at next open; stops are resting GTC SELL STOPs; gross "
                "exposure capped at 1.0x equity by the strategy (the broker "
                "funds each order against total equity in isolation, so without "
                "this the book reached 3.49x)"
            ),
        },
        "universes": {},
    }

    for name in [u.strip() for u in args.universe.split(",") if u.strip()]:
        if name not in UNIVERSES:
            print(f"unknown universe {name!r}", file=sys.stderr)
            return 1
        series = load_series(universe_symbols(name), min_bars=args.min_bars)
        if not series:
            print(f"{name}: no cached history", file=sys.stderr)
            continue
        feed, instruments = build_feed(series)
        snaps = feed.load()
        dates = [pd.Timestamp(s.ts) for s in snaps]
        span = f"{snaps[0].ts:%Y-%m-%d} → {snaps[-1].ts:%Y-%m-%d}"
        print(f"\n══ {name}: {len(series)} symbols, {len(snaps)} sessions, {span}")

        entry = {
            "symbols": len(series),
            "sessions": len(snaps),
            "symbol_bars": int(sum(len(f) for f in series.values())),
            "window": span,
            "variants": {},
        }

        for variant in variants:
            cls = STRATEGIES.get(variant)
            if cls is None:
                print(f"  unknown variant {variant!r}")
                continue
            grid = PARAM_GRIDS[variant]
            n_combos = 1
            for values in grid.values():
                n_combos *= len(values)
            print(f"\n  ── {variant}  ({n_combos} parameter combinations per fold)")

            try:
                res = walk_forward(feed, cls, grid, config=config, backtest=backtest,
                                   validation=validation)
            except Exception as exc:  # noqa: BLE001
                print(f"     FAILED: {exc}")
                entry["variants"][variant] = {"error": str(exc)}
                continue

            non_empty = [f.test_trades for f in res.folds if not f.test_trades.empty]
            trades = pd.concat(non_empty) if non_empty else pd.DataFrame()
            stats = measured(trades)
            oos, bench = res.oos_metrics, res.benchmark_metrics
            z = None

            print(res.verdict.summary())
            print(f"     OOS return        : {oos.total_return_pct:+.2f}% "
                  f"(buy & hold {bench.total_return_pct:+.2f}%)")
            print(f"     OOS Sharpe        : {oos.sharpe:+.3f} "
                  f"(buy & hold {bench.sharpe:+.3f})")
            print(f"     max drawdown      : {oos.max_drawdown_pct:.2f}%")
            print(f"     P(edge is real)   : {res.deflated_sharpe:.3f} "
                  f"vs hurdle Sharpe {res.required_sharpe:.4f} over {res.n_trials} trials")
            print(f"     trades            : {stats['trades']} "
                  f"(win {stats['win_rate']}, payoff {stats['payoff']}, "
                  f"expectancy {stats['expectancy_r']}R)")
            if stats["median_hold_days"] is not None:
                print(f"     hold              : median {stats['median_hold_days']} sessions, "
                      f"best {stats['best_trade_return_pct']}%, "
                      f"worst {stats['worst_trade_return_pct']}%")

            control = {"runs": 0}
            if args.control_runs and stats["trades"]:
                pilot = pilot_trades(feed, cls, backtest)
                print(f"     control           : {args.control_runs} matched runs "
                      f"(same symbols/durations/sizes, random entry dates)")
                control = run_control(feed, dates, pilot, backtest, config, validation,
                                      args.control_runs)
                if control["runs"]:
                    if control["sharpe_sd"] > 1e-9:
                        z = (oos.sharpe - control["sharpe_mean"]) / control["sharpe_sd"]
                    print(f"       control Sharpe  : {control['sharpe_mean']:+.3f} "
                          f"(sd {control['sharpe_sd']:.3f}, n={control['runs']})")
                    print(f"       strategy vs ctrl: "
                          f"{'n/a (sd=0)' if z is None else f'{z:+.2f}σ'}")

            entry["variants"][variant] = {
                "passed": bool(res.verdict.passed),
                "folds": len(res.folds),
                "oos_return_pct": round(oos.total_return_pct, 3),
                "oos_sharpe": round(oos.sharpe, 4),
                "oos_max_drawdown_pct": round(oos.max_drawdown_pct, 3),
                "oos_trades": int(oos.num_trades),
                "benchmark_return_pct": round(bench.total_return_pct, 3),
                "benchmark_sharpe": round(bench.sharpe, 4),
                "deflated_sharpe": round(res.deflated_sharpe, 4),
                "required_sharpe": round(res.required_sharpe, 4),
                "n_trials": res.n_trials,
                "sharpe_z_vs_control": None if z is None else round(z, 3),
                "control": control,
                "measured": stats,
                "chosen_params": [f.params for f in res.folds],
                "checks": [{"name": c[0], "ok": bool(c[1]), "detail": c[2]}
                           for c in res.verdict.checks],
            }

        payload["universes"][name] = entry

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf8")
    print(f"\nwrote {out}")

    for universe, entry in payload["universes"].items():
        scored = [v for v in entry["variants"].values() if "error" not in v]
        passed = [v for v in scored if v["passed"]]
        print(f"{universe}: {len(passed)}/{len(scored)} variants passed every check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
