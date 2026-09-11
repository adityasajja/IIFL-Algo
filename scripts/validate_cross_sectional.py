"""Walk-forward the cross-sectional momentum strategy on a broad universe.

A research run rather than a product command: it fetches years of dailies for
many symbols, which is too slow to sit behind a CLI verb.

Usage:
    ./.venv/Scripts/python.exe scripts/validate_cross_sectional.py [--limit 120]
"""

# ruff: noqa: I001  — the src/ path shim must run before the atr imports.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atr.backtest.engine import BacktestConfig  # noqa: E402
from atr.brokers.iifl.client import IiflClient  # noqa: E402
from atr.brokers.iifl.contracts import InstrumentMaster  # noqa: E402
from atr.config.settings import get_settings  # noqa: E402
from atr.core.enums import Timeframe  # noqa: E402
from atr.core.models import Instrument  # noqa: E402
from atr.data.base import ListFeed, pivot_to_snapshots  # noqa: E402
from atr.research.validate import (  # noqa: E402
    ValidationConfig,
    WalkForwardConfig,
    walk_forward,
)
from atr.signals.cross_sectional import CrossSectionalMomentum  # noqa: E402
from atr.signals.engine import liquid_universe, load_daily  # noqa: E402
from loguru import logger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=120, help="universe size")
    parser.add_argument("--train", type=int, default=300)
    parser.add_argument("--test", type=int, default=200)
    parser.add_argument("--lookback", type=int, default=126)
    parser.add_argument("--skip", type=int, default=21)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--rebalance", type=int, default=21)
    parser.add_argument("--trend-sma", type=int, default=0)
    parser.add_argument("--refresh", action="store_true", help="refetch the panel")
    parser.add_argument("--ranking", default="momentum", choices=["momentum", "random"])
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    settings = get_settings()
    client = IiflClient(
        app_key=settings.iifl_app_key, app_secret=settings.iifl_app_secret
    )
    if not client.restore_session():
        logger.error("no IIFL session — run `atr login --print-url` first")
        return 1

    symbols = liquid_universe("NSEEQ", limit=args.limit)
    logger.info("universe: {} liquidity-screened names", len(symbols))

    import pandas as pd

    # Cache the fetched panel: pulling six years of dailies for 120 names takes
    # ~90s, which is fine once and intolerable when comparing configurations.
    panel = Path(f"data/signals/panel_nseeq_{args.limit}.parquet")
    if panel.exists() and not args.refresh:
        combined = pd.read_parquet(panel)
        logger.info("reused cached panel: {} rows", len(combined))
    else:
        frames = []
        with client:
            master = InstrumentMaster(client)
            master.load_cached(["NSEEQ"])
            for i, symbol in enumerate(symbols, 1):
                try:
                    conid = master.find(symbol, "NSEEQ").conid
                except KeyError:
                    continue
                frame = load_daily(symbol, "NSEEQ", client, conid, lookback_days=2200)
                if frame.empty:
                    continue
                frame = frame.copy()
                frame["symbol"] = symbol
                frames.append(frame)
                if i % 20 == 0:
                    logger.info("fetched {}/{}", i, len(symbols))
        if not frames:
            logger.error("no history fetched")
            return 1
        combined = pd.concat(frames, ignore_index=True)
        panel.parent.mkdir(parents=True, exist_ok=True)
        combined.to_parquet(panel, index=False)
        logger.info("cached panel -> {}", panel)

    symbol_count = int(combined["symbol"].nunique())
    logger.info(
        "{} symbols, {} -> {}",
        symbol_count, combined["ts"].min().date(), combined["ts"].max().date(),
    )

    snapshots = pivot_to_snapshots(combined, Timeframe.DAY_1)
    instruments = {s: Instrument(symbol=s, exchange="NSEEQ") for s in symbols}
    feed = ListFeed(snapshots, instruments)

    warmup = args.lookback + args.skip + 40
    result = walk_forward(
        feed,
        CrossSectionalMomentum,
        {
            "lookback": [args.lookback],
            "skip": [args.skip],
            "top_n": [args.top_n],
            "rebalance_days": [args.rebalance],
            "trend_sma": [args.trend_sma],
            "ranking": [args.ranking],
            "seed": [args.seed],
        },
        config=WalkForwardConfig(
            train_bars=args.train, test_bars=args.test, warmup_bars=warmup
        ),
        backtest=BacktestConfig(initial_cash=1_000_000.0),
        validation=ValidationConfig(min_folds=3, min_trades=20),
    )
    print(result.summary())

    out = Path("data/signals/validation_cross_sectional.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "strategy": "cross_sectional_momentum",
                "universe_size": len(frames),
                "params": {
                    "lookback": args.lookback, "skip": args.skip, "top_n": args.top_n,
                    "rebalance_days": args.rebalance, "trend_sma": args.trend_sma,
                    "ranking": args.ranking, "seed": args.seed,
                },
                "passed": result.verdict.passed,
                "oos_return_pct": result.oos_metrics.total_return_pct,
                "oos_sharpe": result.oos_metrics.sharpe,
                "benchmark_return_pct": result.benchmark_metrics.total_return_pct,
                "benchmark_sharpe": result.benchmark_metrics.sharpe,
                "deflated_sharpe": result.deflated_sharpe,
                "folds": len(result.folds),
                "trades": result.oos_metrics.num_trades,
                "checks": [
                    {"name": n, "ok": ok, "detail": d}
                    for n, ok, d in result.verdict.checks
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("wrote {}", out)
    return 0 if result.verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
