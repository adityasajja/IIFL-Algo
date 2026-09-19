"""Strategy list and the research (walk-forward) endpoint."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from atr.api.deps import get_principal
from atr.backtest.config import WARMUP_BARS

from atr.api.legacy.common import _authed_client, _clean, _equity_points
from atr.api.legacy.validation import _PAPER_VALIDATION_PATH

logger = logging.getLogger("atr.api")

router = APIRouter()


def _history_feed(symbols: list[str], exchange: str):
    """A ListFeed over the local parquet cache, limited to ``symbols``.

    Reads the cache instead of the API so a validation run needs no broker
    session and costs nothing to repeat.
    """
    from atr.core.enums import Timeframe
    from atr.core.models import Instrument
    from atr.data.base import ListFeed, pivot_to_snapshots
    from atr.data.history import CACHE_ROOT, load_cached
    from atr.scanner import UNIVERSE

    wanted = [s.upper() for s in symbols] or list(UNIVERSE)
    frames = load_cached(exchange, wanted)
    if not frames:
        if not (CACHE_ROOT / exchange).is_dir():
            raise HTTPException(
                503,
                f"history cache is empty for {exchange} — run "
                f"`atr history sync --exchange {exchange}`",
            )
        raise HTTPException(
            404,
            f"none of {wanted} are in the {exchange} cache — run "
            f"`atr history sync --exchange {exchange}`",
        )
    picked = {s: frames[s] for s in wanted if s in frames}
    if not picked:
        raise HTTPException(
            404,
            f"none of {wanted} are in the {exchange} cache "
            f"({len(frames)} symbols cached)",
        )
    combined = pd.concat(
        [df.assign(symbol=s) for s, df in picked.items()], ignore_index=True
    )
    snapshots = pivot_to_snapshots(combined, Timeframe.DAY_1)
    instruments = {s: Instrument(symbol=s, exchange=exchange) for s in picked}
    return ListFeed(snapshots, instruments), sorted(picked)


# ----------------------------------------------------------------------
# Research — walk-forward validation
# ----------------------------------------------------------------------
class ResearchRequest(BaseModel):
    strategy: str = "signals_entry"
    #: ``cache``  — local parquet dailies (fast, no session, ~1 year)
    #: ``fetch``  — pull deep history from IIFL (needs a session, years of data)
    #: ``synthetic`` — random walks; only useful for smoke-testing the harness
    source: str = "cache"
    symbols: list[str] = Field(default_factory=list)
    exchange: str = "NSEEQ"
    cash: float = 1_000_000.0
    train: int | None = None
    test: int | None = None
    step: int | None = None
    warmup: int | None = None
    lookback_days: int = 2200
    fast: list[int] = Field(default_factory=lambda: [10, 20, 30])
    slow: list[int] = Field(default_factory=lambda: [50, 100])
    search: bool = False
    min_trades: int = 20
    min_folds: int = 3
    confidence: float = 0.95
    slippage_bps: float = 5.0


#: Which parameters a strategy's grid actually varies. A grid the strategy
#: ignores would inflate the trial count and therefore the Sharpe hurdle,
#: making the test stricter for no reason.
_TUNABLE: dict[str, list[str]] = {
    "sma_crossover": ["fast", "slow"],
    "momentum_breakout": ["lookahead", "volume_multiple", "consecutive_breakout"],
    "signals_entry": ["trend_fast_sma", "trend_slow_sma", "pullback_rsi_low", "pullback_rsi_high"],
    "opening_range_breakout": [],
    "cross_sectional_momentum": [],
    # Paper models: min_confidence is a filter, not a fitted parameter, so
    # varying it would inflate the trial count without learning anything.
    "paper_jegadeesh_titman": [],
    "paper_avellaneda_lee": [],
    "paper_volatility_breakout": [],
    "paper_multi_factor_composite": [],
    "paper_iima_nse_momentum": [],
    "paper_nism_52w_high": [],
    "paper_sehgal_low_vol": [],
}

#: Bars of history each strategy needs before its rules will fire. A test
#: window shorter than this cannot produce trades, and "no trades" reads
#: exactly like "no edge" unless we say so out loud.
#:
#: The table itself now lives in ``atr.backtest.config`` because the runner
#: needs it too, and the backtest layer may not import this module. Kept as a
#: name here so existing callers and the ``/strategies`` response are unchanged.
_WARMUP_NEED: dict[str, int] = WARMUP_BARS


def _fetch_feed(symbols: list[str], exchange: str, lookback_days: int):
    """Deep daily history pulled from IIFL, one symbol at a time.

    Needed because the local cache holds about a year, which cannot be carved
    into folds that also leave room for indicator warmup.
    """
    from atr.brokers.iifl.contracts import InstrumentMaster
    from atr.core.enums import Timeframe
    from atr.core.models import Instrument
    from atr.data.base import ListFeed, pivot_to_snapshots
    from atr.scanner import UNIVERSE, resolve_conid
    from atr.signals.engine import load_daily

    client = _authed_client()
    wanted = [s.upper() for s in symbols] or list(UNIVERSE)
    master = InstrumentMaster(client)
    master.load_cached([exchange])

    frames, used = [], []
    with client:
        for symbol in wanted:
            try:
                conid = resolve_conid(master, symbol, exchange)
            except KeyError:
                continue
            frame = load_daily(symbol, exchange, client, conid, lookback_days=lookback_days)
            if frame.empty or "ts" not in frame.columns:
                continue
            frames.append(frame.assign(symbol=symbol))
            used.append(symbol)

    if not frames:
        raise HTTPException(
            503,
            "no daily history returned for any requested symbol — check the "
            "session and that the symbols exist on this exchange",
        )
    combined = pd.concat(frames, ignore_index=True).sort_values("ts", kind="mergesort")
    snapshots = pivot_to_snapshots(combined, Timeframe.DAY_1)
    instruments = {s: Instrument(symbol=s, exchange=exchange) for s in used}
    return ListFeed(snapshots, instruments), sorted(used)


#: Prior results that live in the project record rather than the current
#: ``paper_validation.json``. Reporting ``signals_entry`` as merely "untested"
#: would overstate it: it was tested and lost. Losing is a result.
_PRIOR_RESULTS: dict[str, dict[str, Any]] = {
    "signals_entry": {
        "state": "fail",
        "oos_return_pct": -5.05,
        "benchmark_return_pct": 57.39,
        "deflated_sharpe": 0.926,
        "note": "Walk-forward on 19 large caps, 2020-2026. Below the 0.95 bar and behind buy-and-hold.",
    },
    "cross_sectional_momentum": {
        "state": "fail",
        "deflated_sharpe": 0.976,
        "z_vs_control": -0.71,
        "note": "Cleared deflated Sharpe but sat at the 25th percentile of random selection — significance without usefulness.",
    },
}

_UNTESTED = {"state": "untested"}


@router.get("/strategies")
def strategies() -> dict[str, Any]:
    """Every strategy the engine can run, with its validation status.

    ``validation`` is attached from the last walk-forward run rather than left
    to the caller. A registry that lists an unvalidated strategy next to a
    validated one, with nothing to tell them apart, invites exactly the mistake
    this project is built to avoid.
    """
    from atr.strategy.strategies import STRATEGIES

    measured: dict[str, dict[str, Any]] = {}
    if _PAPER_VALIDATION_PATH.exists():
        try:
            payload = json.loads(_PAPER_VALIDATION_PATH.read_text(encoding="utf8"))
            for r in payload.get("results", []):
                measured[r.get("strategy", "")] = r
        except (OSError, ValueError):
            measured = {}

    rows = []
    for name in sorted(STRATEGIES):
        m = measured.get(name)
        rows.append(
            {
                "name": name,
                "tunable": _TUNABLE.get(name, []),
                "warmup_bars": _WARMUP_NEED.get(name),
                "validation": (
                    {
                        "state": "pass" if m.get("passed") else "fail",
                        "oos_sharpe": m.get("oos_sharpe"),
                        "oos_return_pct": m.get("oos_return_pct"),
                        "benchmark_sharpe": m.get("benchmark_sharpe"),
                        "deflated_sharpe": m.get("deflated_sharpe"),
                        "measured_win_rate": m.get("measured_win_rate"),
                        "measured_trades": m.get("measured_trades"),
                        "expectancy_r": m.get("measured_expectancy_r"),
                        "z_vs_control": m.get("sharpe_z_vs_control"),
                        "as_of": payload.get("generated_at") if measured else None,
                    }
                    if m
                    # A known loss is a result. Fall back to the project record
                    # before calling something untested.
                    else _PRIOR_RESULTS.get(name, _UNTESTED)
                ),
            }
        )

    return {
        "strategies": rows,
        "validation_as_of": payload.get("generated_at") if measured else None,
        "control_sharpe": (
            payload.get("control", {}).get("sharpe_mean") if measured else None
        ),
    }


@router.post("/research", dependencies=[Depends(get_principal)])
def research(request: ResearchRequest) -> dict[str, Any]:
    """Walk a strategy forward and report only the out-of-sample evidence.

    This is the endpoint to trust. ``/backtest`` scores one configuration on
    data it may already have seen; this one picks parameters on a training
    window, scores them once on the following unseen window, and then asks
    whether the winner clears a Sharpe hurdle scaled to how many
    combinations were tried.
    """
    from atr.backtest.costs import SlippageModel
    from atr.backtest.engine import BacktestConfig
    from atr.data.synthetic import SyntheticConfig, SyntheticFeed
    from atr.execution.risk import RiskLimits
    from atr.research.validate import (
        ValidationConfig,
        WalkForwardConfig,
        buy_and_hold_equity,
        walk_forward,
    )
    from atr.strategy.strategies import STRATEGIES

    strategy_cls = STRATEGIES.get(request.strategy)
    if strategy_cls is None:
        raise HTTPException(
            400, f"unknown strategy: {request.strategy} (have {sorted(STRATEGIES)})"
        )

    source = {"history": "cache"}.get(request.source, request.source)
    if source not in {"cache", "fetch", "synthetic"}:
        raise HTTPException(400, f"unknown source: {request.source}")

    symbols = [s.strip().upper() for s in request.symbols if s.strip()]
    if source == "synthetic":
        used = symbols or ["AAPL", "MSFT"]
        feed = SyntheticFeed(
            SyntheticConfig(
                symbols=tuple(used),
                start=datetime(2024, 1, 1, 9, 30),
                end=datetime(2024, 6, 28, 15, 59),
            )
        )
    elif source == "fetch":
        feed, used = _fetch_feed(symbols, request.exchange.upper(), request.lookback_days)
    else:
        feed, used = _history_feed(symbols, request.exchange.upper())

    snapshots = feed.load()
    total = len(snapshots)

    # Size the windows against the data actually in hand. The CLI defaults
    # (5000/1250) are tuned for intraday bars and would refuse to run on a
    # year of dailies, which is what the cache holds.
    folds_wanted = max(1, request.min_folds)
    test = request.test or max(2, total // (folds_wanted + 1))
    train = request.train or max(2, total - folds_wanted * test)
    step = request.step or test
    if total < train + test:
        raise HTTPException(
            422,
            f"{total} bars is too few for a {train}/{test} train/test split — "
            f"sync more history or use smaller windows",
        )

    warnings: list[str] = []
    need = _WARMUP_NEED.get(request.strategy, 0)
    if need and test < need:
        warnings.append(
            f"test window is {test} bars but {request.strategy} needs about {need} "
            f"bars of history before its rules fire — expect near-zero trades, "
            f"which is not evidence of no edge. Use source=fetch for a longer history."
        )
    if train < need:
        warnings.append(
            f"training window is {train} bars, below the ~{need} bars this strategy "
            f"needs to warm up; parameters are being chosen from a window where "
            f"the strategy can barely trade."
        )

    if request.strategy == "sma_crossover":
        grid: dict[str, list] = {"fast": request.fast, "slow": request.slow}
    elif request.strategy == "signals_entry" and request.search:
        from atr.signals.models import SEARCH_GRID

        grid = {k: list(v) for k, v in SEARCH_GRID.items()}
    else:
        grid = {}

    # Warmup is drawn from bars before the test window, which in practice
    # means the training window — asking for more than that is silently
    # clamped by the harness, so clamp it here and report the real number.
    warmup = max(0, min(request.warmup if request.warmup is not None else 130, train))

    try:
        result = walk_forward(
            feed,
            strategy_cls,
            grid,
            config=WalkForwardConfig(
                train_bars=train,
                test_bars=test,
                step_bars=step,
                warmup_bars=warmup,
            ),
            backtest=BacktestConfig(
                initial_cash=request.cash,
                slippage=SlippageModel(bps=request.slippage_bps),
                risk=RiskLimits(max_daily_loss=request.cash * 0.10),
            ),
            validation=ValidationConfig(
                min_folds=request.min_folds,
                min_trades=request.min_trades,
                min_confidence=request.confidence,
            ),
        )
    except ValueError as exc:  # window sizing / empty folds
        raise HTTPException(422, str(exc)) from exc

    if result.oos_metrics.num_trades == 0:
        warnings.append(
            "no trades were taken out of sample — the verdict below is a "
            "statement about the windows, not about the strategy."
        )

    # The benchmark is whatever you would otherwise have done: hold the same
    # names over the same out-of-sample window. A strategy that cannot beat
    # doing nothing is just paying commission.
    first, last = result.folds[0].test_start, result.folds[-1].test_end
    window = [s for s in snapshots if first <= s.ts <= last]
    benchmark_equity = buy_and_hold_equity(window, request.cash)

    return _clean(
        {
            "strategy": request.strategy,
            "source": source,
            "symbols": used,
            "bars": total,
            "train_bars": train,
            "test_bars": test,
            "step_bars": step,
            "warmup_bars": warmup,
            "n_trials": result.n_trials,
            "required_sharpe": result.required_sharpe,
            "deflated_sharpe": result.deflated_sharpe,
            "verdict": {
                "passed": result.verdict.passed,
                "checks": [
                    {"name": name, "ok": ok, "detail": detail}
                    for name, ok, detail in result.verdict.checks
                ],
            },
            "warnings": warnings,
            "oos": result.oos_metrics.as_dict(),
            "benchmark": result.benchmark_metrics.as_dict(),
            "folds": result.to_frame().to_dict(orient="records"),
            "equity": _equity_points(result.oos_equity),
            "benchmark_equity": _equity_points(benchmark_equity),
        }
    )
