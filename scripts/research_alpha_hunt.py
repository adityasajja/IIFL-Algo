"""Score the pre-registered candidates, each against a null that removes only its signal.

The candidates and their priors are written down in
``atr.strategy.strategies.alpha_candidates`` **before** this script was run. That
ordering is the point: the multiple-testing hurdle is only meaningful if the set
of ideas was fixed in advance, and the harness counts every trial so that
searching harder cannot manufacture a win.

The nulls, per hypothesis
-------------------------
* **H1 reversal** — same universe, same cadence, same number of names, chosen at
  random. Removes the ranking and nothing else.
* **H2 vol-managed / H3 trend** — the signal is read ``k`` sessions away from the
  bar being traded (a circular shift). That preserves the exact sequence of
  exposure regimes — every run length, the same average exposure — and destroys
  only their alignment with returns. "Same sizing, wrong dates."

The control is given the **same parameter grid** as the strategy, so it gets the
same search. An unsearched control would be a weaker null, which biases toward
false positives; this way beating it has to mean something.

Usage::

    .venv/Scripts/python.exe scripts/research_alpha_hunt.py
    .venv/Scripts/python.exe scripts/research_alpha_hunt.py --universes midcap150
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pandas as pd  # noqa: E402

from atr.backtest.costs import CommissionModel, IndianDeliveryCosts, SlippageModel  # noqa: E402
from atr.backtest.engine import BacktestConfig  # noqa: E402
from atr.execution.risk import RiskLimits  # noqa: E402
from atr.research.validate import (  # noqa: E402
    ValidationConfig,
    WalkForwardConfig,
    walk_forward,
)
from atr.strategy.strategies import STRATEGIES  # noqa: E402
from research_episodic_pivot import UNIVERSES, universe_symbols  # noqa: E402
from validate_paper_strategies import build_feed, load_series  # noqa: E402

#: Pre-registered. Small on purpose: each extra combination raises the bar the
#: winner must clear, so a wide grid makes a *result* harder, not easier.
HYPOTHESES: dict[str, dict] = {
    "reversal_short_horizon": {
        "class": "reversal_short_horizon",
        "prior": "Lehmann (1990), Jegadeesh (1990); stronger in less liquid names",
        "grid": {
            "lookback": [5, 20, 60],
            "rebalance_days": [10, 20],
            "frac": [0.10],
        },
        "control": {"random_select": [True], "seed": None},
        "control_seeds": [0, 1, 2, 3, 4, 5, 6, 7],
    },
    "vol_managed_exposure": {
        "class": "vol_managed_exposure",
        "prior": "Moreira & Muir (2017) — volatility is forecastable, return is not",
        "grid": {
            "target_vol": [12.0, 15.0, 20.0],
            "rebalance_days": [5, 10],
        },
        "control": {"signal_shift": None},          # filled with a random shift
        "control_seeds": [0, 1, 2, 3, 4, 5, 6, 7],
    },
    "trend_filtered_exposure": {
        "class": "trend_filtered_exposure",
        "prior": "Moskowitz, Ooi & Pedersen (2012) time-series momentum",
        "grid": {
            "sma_window": [100, 200],
            "rebalance_days": [5, 10],
        },
        "control": {"signal_shift": None},
        "control_seeds": [0, 1, 2, 3, 4, 5, 6, 7],
    },
}


def _metrics(res) -> dict:
    m = res.oos_metrics
    b = res.benchmark_metrics
    return {
        "oos_return_pct": round(m.total_return_pct, 3),
        "oos_sharpe": round(m.sharpe, 4),
        "oos_calmar": round(m.calmar, 4),
        "oos_max_drawdown_pct": round(m.max_drawdown_pct, 3),
        "oos_vol_pct": round(m.annualized_vol_pct, 3),
        "exposure_pct": round(m.exposure_pct, 2),
        "trades": int(m.num_trades),
        "commission": round(float(m.total_commission), 1),
        "bench_return_pct": round(b.total_return_pct, 3),
        "bench_sharpe": round(b.sharpe, 4),
        "bench_calmar": round(b.calmar, 4),
        "bench_max_drawdown_pct": round(b.max_drawdown_pct, 3),
        "folds": len(res.folds),
        "n_trials": res.n_trials,
        "deflated_sharpe": round(res.deflated_sharpe, 4),
        "required_sharpe": round(res.required_sharpe, 4),
        "checks": [{"name": c[0], "ok": bool(c[1]), "detail": c[2]}
                   for c in res.verdict.checks],
    }


def run_control(feed, cls, spec, config, backtest, validation):
    """Average the null over seeds. Same grid as the strategy, plus the switch.

    The seed has to enter through the *grid*, not through a constructor default:
    `walk_forward` instantiates the class from the grid alone, so a seed left in
    the class default silently gives every "independent" control run the same
    random draw — and a control with zero variance reads as a null the strategy
    can never clear, which is the opposite of a test.
    """
    sharpes, calmars, returns = [], [], []
    for seed in spec["control_seeds"]:
        grid = {key: list(values) for key, values in spec["grid"].items()}
        for key, values in spec["control"].items():
            if key == "seed":
                continue
            if values is None:
                # Circular shift: same exposure sequence, wrong dates. Offset by
                # a random chunk so the null cannot accidentally re-align.
                grid[key] = [random.Random(9100 + seed).randint(7, 400)]
            else:
                grid[key] = list(values)
        if "seed" in spec["control"]:
            grid["seed"] = [seed]
        try:
            res = walk_forward(feed, cls, grid, config=config, backtest=backtest,
                               validation=validation)
        except Exception as exc:  # noqa: BLE001
            print(f"      control seed {seed} failed: {exc}")
            continue
        sharpes.append(res.oos_metrics.sharpe)
        calmars.append(res.oos_metrics.calmar)
        returns.append(res.oos_metrics.total_return_pct)

    def _mean(values):
        return round(statistics.fmean(values), 4) if values else None

    def _sd(values):
        return round(statistics.stdev(values), 4) if len(values) > 1 else 0.0

    return {
        "seeds": len(sharpes),
        "sharpe_mean": _mean(sharpes),
        "sharpe_sd": _sd(sharpes),
        "calmar_mean": _mean(calmars),
        "calmar_sd": _sd(calmars),
        "return_mean_pct": _mean(returns),
    }


def average_exposure(feed, cls, params, backtest) -> float:
    """Mean gross exposure of one full-sample run, in percent.

    `walk_forward`'s out-of-sample metrics are computed without the exposure
    series, so `exposure_pct` comes back 0.0 — a plausible-looking zero that
    says nothing. This measures it directly instead. It is a characterisation of
    the strategy's behaviour, not a performance number, so using the full sample
    here is fine and it is never reported as a result.
    """
    from atr.backtest.engine import BacktestEngine

    try:
        result = BacktestEngine(feed, cls(**params), backtest).run()
    except Exception:  # noqa: BLE001
        return None
    if result.exposure.empty:
        return None
    return round(float(result.exposure.mean()) * 100.0, 2)



#: One-sided 95% critical values of Student's t, by degrees of freedom.
#:
#: The control's spread is estimated from a handful of runs, so the threshold
#: for "the strategy beat its null" is not 2.0. With 3 control runs (df=2) it is
#: **2.92**; with 8 (df=7) it is 1.89. Using a flat 2.0 across the board would
#: pass a 3-seed comparison that the data does not support — and the first run of
#: this script did exactly that, reporting "+2.51σ" off three samples.
_T95_ONE_SIDED = {
    1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, 5: 2.015, 6: 1.943, 7: 1.895,
    8: 1.860, 9: 1.833, 10: 1.812, 12: 1.782, 15: 1.753, 20: 1.725,
    25: 1.708, 30: 1.697, 40: 1.684, 60: 1.671, 120: 1.658,
}


def t_critical_one_sided(df: int) -> float:
    """Nearest tabulated one-sided 95% t value (1.645 as df -> infinity)."""
    if df <= 0:
        return float("inf")
    if df in _T95_ONE_SIDED:
        return _T95_ONE_SIDED[df]
    keys = sorted(_T95_ONE_SIDED)
    for lower, upper in zip(keys, keys[1:]):
        if lower < df < upper:
            span = upper - lower
            weight = (df - lower) / span
            return _T95_ONE_SIDED[lower] + weight * (
                _T95_ONE_SIDED[upper] - _T95_ONE_SIDED[lower]
            )
    return 1.645


def verdict(res_metrics: dict, control: dict) -> dict:
    """Does this clear the four pre-registered bars?"""
    sharpe = res_metrics["oos_sharpe"]
    calmar = res_metrics["oos_calmar"]
    bench_sharpe = res_metrics["bench_sharpe"]
    bench_calmar = res_metrics["bench_calmar"]

    seeds = control.get("seeds") or 0
    sd = control.get("sharpe_sd") or 0.0
    mean = control.get("sharpe_mean")
    z = None if mean is None or sd <= 1e-9 else (sharpe - mean) / sd
    critical = t_critical_one_sided(seeds - 1) if seeds > 1 else float("inf")

    checks = [
        ("positive out-of-sample Sharpe", sharpe > 0, f"{sharpe:+.3f}"),
        (
            f"beats matched control at 95% (t >= {critical:.2f}, df={seeds - 1})",
            bool(z is not None and z >= critical),
            "n/a — control sd is zero" if z is None else f"{z:+.2f} vs {mean:+.3f}",
        ),
        (
            "P(edge is real) >= 0.95",
            res_metrics["deflated_sharpe"] >= 0.95,
            f"{res_metrics['deflated_sharpe']:.3f} over {res_metrics['n_trials']} trials",
        ),
        (
            "beats buy & hold on Sharpe or Calmar",
            sharpe > bench_sharpe or calmar > bench_calmar,
            f"Sharpe {sharpe:.3f} vs {bench_sharpe:.3f} · Calmar {calmar:.3f} vs {bench_calmar:.3f}",
        ),
    ]
    return {
        "passed": all(ok for _, ok, _ in checks),
        "sharpe_z_vs_control": None if z is None else round(z, 3),
        "t_critical": None if critical == float("inf") else round(critical, 3),
        "control_df": seeds - 1,
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universes", default="nifty50,midcap150")
    ap.add_argument("--hypotheses", default=",".join(HYPOTHESES))
    ap.add_argument("--min-bars", type=int, default=1000)
    ap.add_argument("--cash", type=float, default=1_000_000.0)
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--train", type=int, default=500)
    ap.add_argument("--test", type=int, default=250)
    ap.add_argument("--warmup", type=int, default=260)
    ap.add_argument("--min-trades", type=int, default=25)
    ap.add_argument(
        "--costs",
        choices=("ibkr", "india"),
        default="ibkr",
        help=(
            "ibkr = the historical default (Rs 0.005/share, no statutory levy); "
            "india = realistic NSE delivery costs, ~0.28%% of turnover per round "
            "trip, dominated by STT"
        ),
    )
    ap.add_argument("--max-daily-loss-pct", type=float, default=25.0)
    ap.add_argument(
        "--no-control",
        action="store_true",
        help="skip the matched null — for sensitivity sweeps, where the null is "
             "not the question and costs most of the runtime",
    )
    ap.add_argument("--out", default="data/self_learning/alpha_hunt.json")
    args = ap.parse_args()

    names = [n.strip() for n in args.hypotheses.split(",") if n.strip()]
    config = WalkForwardConfig(
        train_bars=args.train, test_bars=args.test, warmup_bars=args.warmup
    )
    commission = IndianDeliveryCosts() if args.costs == "india" else CommissionModel()
    backtest = BacktestConfig(
        initial_cash=args.cash,
        commission=commission,
        slippage=SlippageModel(bps=args.slippage_bps),
        risk=RiskLimits(max_daily_loss=args.cash * args.max_daily_loss_pct / 100.0),
    )
    validation = ValidationConfig(min_trades=args.min_trades)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pre_registered": {
            name: {"prior": HYPOTHESES[name]["prior"], "grid": HYPOTHESES[name]["grid"]}
            for name in names if name in HYPOTHESES
        },
        "config": {
            "train_bars": args.train, "test_bars": args.test, "warmup_bars": args.warmup,
            "slippage_bps": args.slippage_bps, "initial_cash": args.cash,
            "min_trades": args.min_trades,
            "cost_model": args.costs,
            "bar": "positive Sharpe, beats a matched control at 95% (t table), "
                   "P(edge)>=0.95, and beats buy & hold on Sharpe or Calmar",
        },
        "universes": {},
    }

    for universe in [u.strip() for u in args.universes.split(",") if u.strip()]:
        if universe not in UNIVERSES:
            print(f"unknown universe {universe!r}", file=sys.stderr)
            return 1
        series = load_series(universe_symbols(universe), min_bars=args.min_bars)
        if not series:
            print(f"{universe}: no cached history", file=sys.stderr)
            continue
        feed, _ = build_feed(series)
        snaps = feed.load()
        span = f"{snaps[0].ts:%Y-%m-%d} → {snaps[-1].ts:%Y-%m-%d}"
        print(f"\n══ {universe}: {len(series)} symbols, {len(snaps)} sessions, {span}")

        entry: dict = {"symbols": len(series), "sessions": len(snaps), "window": span,
                       "hypotheses": {}}

        for name in names:
            spec = HYPOTHESES.get(name)
            if spec is None:
                continue
            cls = STRATEGIES.get(spec["class"])
            combos = 1
            for values in spec["grid"].values():
                combos *= len(values)
            print(f"\n  ── {name}  ({combos} combos × {len(spec['control_seeds'])} control seeds)")

            try:
                res = walk_forward(feed, cls, spec["grid"], config=config,
                                   backtest=backtest, validation=validation)
            except Exception as exc:  # noqa: BLE001
                print(f"     FAILED: {exc}")
                entry["hypotheses"][name] = {"error": str(exc)}
                continue

            m = _metrics(res)
            primary = dict(spec["grid"])
            for key, values in primary.items():
                primary[key] = values[0]
            m["avg_exposure_pct"] = average_exposure(feed, cls, primary, backtest)
            print(f"     OOS return {m['oos_return_pct']:+.2f}%  Sharpe {m['oos_sharpe']:+.3f}  "
                  f"Calmar {m['oos_calmar']:+.3f}  maxDD {m['oos_max_drawdown_pct']:.2f}%")
            print(f"     buy & hold return {m['bench_return_pct']:+.2f}%  "
                  f"Sharpe {m['bench_sharpe']:+.3f}  Calmar {m['bench_calmar']:+.3f}  "
                  f"maxDD {m['bench_max_drawdown_pct']:.2f}%")
            print(f"     avg exposure {m['avg_exposure_pct']}%  trades {m['trades']}  "
                  f"commission {m['commission']:,.0f}  P(edge) {m['deflated_sharpe']:.3f} "
                  f"over {m['n_trials']} trials")

            if args.no_control:
                control = {"seeds": 0, "sharpe_mean": None, "sharpe_sd": 0.0}
                print("     control  skipped (--no-control)")
            else:
                control = run_control(feed, cls, spec, config, backtest, validation)
                print(f"     control  Sharpe {control['sharpe_mean']} "
                      f"(sd {control['sharpe_sd']}, n={control['seeds']})  "
                      f"Calmar {control['calmar_mean']}")
            v = verdict(m, control)
            print("     VERDICT: " + ("PASS" if v["passed"] else "FAIL"))
            for check in v["checks"]:
                print(f"       [{'x' if check['ok'] else ' '}] {check['name']}: {check['detail']}")
            per_fold = [round(f.test_metrics.sharpe, 2) for f in res.folds]
            print(f"     per-fold Sharpe: {per_fold}")

            entry["hypotheses"][name] = {
                "prior": spec["prior"],
                "metrics": m,
                "control": control,
                "verdict": v,
                "chosen_params": [f.params for f in res.folds],
                # Per-fold, because a headline Sharpe can be one lucky window
                # wearing a good average. If the edge is real it should be
                # visible in most folds, not concentrated in one.
                "folds": [
                    {
                        "fold": f.fold,
                        "test_start": str(f.test_start),
                        "test_end": str(f.test_end),
                        "test_sharpe": round(f.test_metrics.sharpe, 3),
                        "test_return_pct": round(f.test_metrics.total_return_pct, 2),
                        "test_max_dd_pct": round(f.test_metrics.max_drawdown_pct, 2),
                        "params": f.params,
                    }
                    for f in res.folds
                ],
            }

        payload["universes"][universe] = entry

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf8")
    print(f"\nwrote {out}")

    for universe, entry in payload["universes"].items():
        scored = [h for h in entry["hypotheses"].values() if "error" not in h]
        passed = [h for h in scored if h["verdict"]["passed"]]
        print(f"{universe}: {len(passed)}/{len(scored)} hypotheses pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
