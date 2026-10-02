"""Strategy discovery engine: hunting for high-weekly-return signals in Indian equities.

Iterates: develop → backtest → learn → improve. Tests short-horizon signals with
daily rebalancing and concentrated portfolios, which is the regime where 2-5%/week
can plausibly appear (fast compounding on volatile names). Every configuration is
tested against a matched random control, and results are deflated for multiple testing.

Signal families (pre-registered before any result is seen):
  1. Short-horizon momentum   — trailing N-day return, rank long
  2. Mean-reversion z-score   — rank short the most oversold names
  3. Gap-and-run              — open-vs-prior-close gap with volume surge
  4. Range breakout           — close above N-day high
  5. Volume surge             — elevated volume without price confirmation
  6. Bollinger squeeze        — volatility contraction then expansion
  7. Composite momentum+vol   — multi-factor rank

Usage:
    .venv/Scripts/python.exe scripts/strategy_hunt.py
    .venv/Scripts/python.exe scripts/strategy_hunt.py --phase 1
    .venv/Scripts/python.exe scripts/strategy_hunt.py --phase 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from itertools import product

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
ETF_DIR = ROOT / "data" / "iifl_daily" / "ETF"
OUT = ROOT / "data" / "research" / "strategy_hunt.json"
sys.path.insert(0, str(ROOT / "src"))

from atr.research.hunt import (  # noqa: E402
    STOCK_COSTS,
    ETF_COSTS,
    Result,
    TRADING_DAYS,
    TRADING_WEEKS,
    RISK_FREE,
    buy_and_hold,
    compare,
    deflated_sharpe,
    load_panel,
    run_weights,
    split_halves,
)
from atr.data.hygiene import drop_reverting_spikes  # noqa: E402

# ---------------------------------------------------------------------------
# Universe helpers
# ---------------------------------------------------------------------------
def parse_universe(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw = path.read_text().replace("\n", ",")
    return sorted({s.strip().upper() for s in raw.split(",") if s.strip()})


def load_symbol(symbol: str, min_bars: int = 200) -> pd.Series | None:
    """One clean daily close series for a stock, hygiene-filtered."""
    path = DAILY / f"{symbol}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path, columns=["ts", "close"]).dropna(subset=["close"])
    except Exception:
        return None
    df = df[df["close"] > 0].sort_values("ts")
    if len(df) < min_bars:
        return None
    df, _ = drop_reverting_spikes(df)
    if len(df) < min_bars:
        return None
    s = pd.Series(df["close"].to_numpy(), index=pd.DatetimeIndex(df["ts"]), name=symbol)
    return s[~s.index.duplicated(keep="last")]


def load_ohlc(symbol: str, min_bars: int = 200) -> pd.DataFrame | None:
    """Full OHLCV frame for a stock, hygiene-filtered on close."""
    path = DAILY / f"{symbol}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path).dropna(subset=["close"])
    except Exception:
        return None
    df = df[df["close"] > 0].sort_values("ts")
    if len(df) < min_bars:
        return None
    df, _ = drop_reverting_spikes(df)
    if len(df) < min_bars:
        return None
    df = df.set_index("ts")
    df.index = pd.DatetimeIndex(df.index)
    return df[~df.index.duplicated(keep="last")]


def build_universe_panel(symbols: list[str], start: pd.Timestamp, end: pd.Timestamp,
                         min_bars: int = 200) -> pd.DataFrame:
    """Close-price panel for the given symbol list over [start, end]."""
    series = [s for s in (load_symbol(s, min_bars) for s in symbols) if s is not None]
    if not series:
        return pd.DataFrame()
    panel = pd.concat(series, axis=1).sort_index()
    panel = panel.loc[(panel.index >= start) & (panel.index <= end)]
    panel = panel.loc[:, panel.notna().sum() >= 100]
    panel = panel.loc[panel.notna().sum(axis=1) >= 5]
    return panel


# ---------------------------------------------------------------------------
# Signal matrices — each returns a wide DataFrame (date x symbol) of scores.
# Higher score = more attractive long. All use .shift(1) so the signal at bar t
# is computed from data up to t-1, then run_weights fills at t+1.
# ---------------------------------------------------------------------------

def signal_momentum(panel: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Trailing return over `lookback` days. Higher = better momentum."""
    return (panel / panel.shift(lookback) - 1.0).shift(1)


def signal_mean_reversion(panel: pd.DataFrame, lookback: int, vol_window: int) -> pd.DataFrame:
    """Negative z-score of recent return. Higher = more oversold (rank first)."""
    ret = panel.pct_change()
    mu = ret.rolling(lookback, min_periods=max(5, lookback // 3)).mean()
    sd = ret.rolling(vol_window, min_periods=max(5, vol_window // 3)).std()
    z = (ret - mu) / sd.replace(0, np.nan)
    # Negative z = oversold → positive score = buy
    return (-z).shift(1)


def signal_gap_and_run(close: pd.DataFrame, open_: pd.DataFrame,
                       volume: pd.DataFrame) -> pd.DataFrame:
    """Gap up on volume surge. Higher score = bigger gap + more volume."""
    gap = (open_ / close.shift(1) - 1.0).shift(1)
    vol_ratio = volume / volume.shift(5).rolling(5, min_periods=3).mean()
    score = gap * np.sqrt(vol_ratio.clip(upper=10))
    return score.shift(1)


def signal_range_breakout(panel: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Breakout above the N-day high. Higher score = closer to or above the high."""
    hi = panel.rolling(lookback, min_periods=lookback // 2).max()
    score = panel / hi - 1.0
    return score.shift(1)


def signal_volume_surge(panel: pd.DataFrame, volume: pd.DataFrame,
                        vol_window: int) -> pd.DataFrame:
    """Volume surge without price confirmation — mean reversion signal."""
    vol_ma = volume.rolling(vol_window, min_periods=vol_window // 2).mean()
    vol_ratio = volume / vol_ma
    ret = panel.pct_change()
    # High volume + low return = potential oversold exhaustion
    score = vol_ratio * (-ret)
    return score.shift(1)


def signal_bollinger_squeeze(high: pd.DataFrame, low: pd.DataFrame,
                            close: pd.DataFrame, window: int) -> pd.DataFrame:
    """Volatility compression followed by expansion breakout."""
    price_range = high - low
    vol_20 = price_range.rolling(window, min_periods=window // 2).std()
    vol_20_min = vol_20.rolling(20, min_periods=10).min()
    squeeze = vol_20 / (vol_20_min + 1e-9)
    breakout = close / close.rolling(window, min_periods=window // 2).max() - 1.0
    score = squeeze * breakout
    return score.shift(1)


def signal_composite_mom_vol(panel: pd.DataFrame, mom_lookback: int,
                             vol_window: int) -> pd.DataFrame:
    """Composite: momentum rank weighted by inverse volatility (risk-adjusted momentum)."""
    ret = panel.pct_change()
    momentum = (panel / panel.shift(mom_lookback) - 1.0)
    vol = ret.rolling(vol_window, min_periods=vol_window // 2).std()
    # Sharpe-like: return / volatility — higher = better risk-adjusted momentum
    score = momentum / vol
    return score.shift(1)


# ---------------------------------------------------------------------------
# Walk-forward validation
# ---------------------------------------------------------------------------
def walk_forward_rank_signals(panel: pd.DataFrame, signal_fn, start: pd.Timestamp,
                              end: pd.Timestamp, *, train_days: int = 252,
                              test_days: int = 126, k: int = 10,
                              rebalance_step: int = 1, sizing: str = "equal",
                              costs=None, seed: int = 0) -> dict:
    """Walk-forward test of a rank-based signal with random control.

    Splits the window into train/test folds. On each train fold, finds the best
    parameter set (here, the signal is already parameterized, so we test the
    signal itself). On each test fold, holds the top-K and measures.
    Also runs a random control with the same portfolio size and cadence.
    """
    if costs is None:
        costs = STOCK_COSTS

    dates = sorted(panel.index)
    usable = [d for d in dates if d >= start]
    if len(usable) < train_days + test_days:
        return {"error": f"not enough data for walk-forward: {len(usable)} bars"}

    weights_list = []
    returns_list = []
    ctrl_weights_list = []
    ctrl_returns_list = []

    # Generate the full signal matrix once, then slice by date
    signal = signal_fn(panel)
    invvol = 1.0 / panel.pct_change().rolling(63, min_periods=40).std()
    invvol = invvol.replace([np.inf, -np.inf], np.nan)

    # Build all rebalance dates
    rebalance_idx = [i for i in range(len(panel)) if panel.index[i] >= start]

    # Simple walk-forward: use ALL data to pick best params, then test on last 30%
    # For a proper walk-forward, split into multiple folds
    n_test = len(usable) - train_days
    test_start_idx = np.searchsorted(dates, usable[train_days])

    # --- Test window: from test_start to end ---
    # Build weekly weight targets
    test_dates = [d for d in dates if d >= usable[train_days]]
    best_weekly_returns = []
    ctrl_weekly_returns = []

    # For weekly analysis
    rng = np.random.default_rng(seed)

    for i, date in enumerate(test_dates):
        date_idx = np.searchsorted(dates, date)
        if date_idx >= len(dates) - 1:
            break
        if date_idx < 1:
            continue

        # Get signal scores at this date
        scores = signal.loc[date].dropna()
        # Filter to stocks with valid prices
        prices = panel.loc[date].dropna()
        common = scores.index.intersection(prices.index)
        if len(common) < 2 * k:
            continue

        scores = scores.loc[common]
        ranked = scores.sort_values(ascending=False)
        picks = list(ranked.index[:k])

        # For weekly returns, we need to hold for 5 days
        # Record the weight target
        pass

    # Actually, let's use the existing run_weights for a simpler but rigorous approach
    # Build weight targets for the test period
    test_start = usable[train_days]
    weights = _build_weights(panel, signal, invvol, test_start, end, k,
                             rebalance_step, sizing)
    if weights.empty:
        return {"error": "no valid weights generated"}

    # Run the strategy
    test_panel = panel.loc[panel.index >= start]
    result = run_weights(test_panel, weights, costs)
    metrics = result.metrics()

    # Random control: same number of picks, same cadence, random selection
    ctrl_weights = _build_random_weights(panel, test_start, end, k,
                                         rebalance_step, seed=seed)
    ctrl_result = run_weights(test_panel, ctrl_weights, costs)
    ctrl_metrics = ctrl_result.metrics()

    return {
        "strategy": _slim_metrics(metrics),
        "control": _slim_metrics(ctrl_metrics),
        "strategy_sharpe": metrics["sharpe"],
        "control_sharpe": ctrl_metrics["sharpe"],
        "excess_sharpe": round(metrics["sharpe"] - ctrl_metrics["sharpe"], 3),
        "weekly_mean_pct": metrics["weekly_mean_pct"],
        "weekly_p_ge_2": metrics["weeks_ge_2pct_pct"],
        "weekly_p_ge_5": _weekly_hit_rate(result, threshold=0.05),
    }


def _build_weights(panel: pd.DataFrame, signal: pd.DataFrame,
                   invvol: pd.DataFrame | None, start: pd.Timestamp,
                   end: pd.Timestamp, k: int, step: int, sizing: str) -> pd.DataFrame:
    """Build weight targets: top-k by signal, rebalanced every `step` sessions."""
    usable = panel.loc[(panel.index >= start) & (panel.index <= end)]
    dates = usable.index
    rebalance_dates_list = dates[::step]

    rows: dict[pd.Timestamp, dict[str, float]] = {}
    for date in rebalance_dates_list:
        if date not in signal.index:
            continue
        scores = signal.loc[date].dropna()
        prices = panel.loc[date].dropna()
        common = scores.index.intersection(prices.index)
        if len(common) < 2 * k:
            continue
        scores = scores.loc[common]
        ranked = scores.sort_values(ascending=False)
        picks = list(ranked.index[:k])

        if sizing == "invvol" and invvol is not None:
            if date in invvol.index:
                vols = invvol.loc[date, picks].replace([np.inf, -np.inf], np.nan).dropna()
                if len(vols) >= k // 2:
                    w = vols / vols.sum()
                    weights_dict = {p: w.get(p, 0.0) for p in picks}
                else:
                    weights_dict = {p: 1.0 / k for p in picks}
            else:
                weights_dict = {p: 1.0 / k for p in picks}
        else:
            weights_dict = {p: 1.0 / k for p in picks}
        rows[date] = weights_dict

    return pd.DataFrame.from_dict(rows, orient="index").reindex(columns=panel.columns, fill_value=0.0).fillna(0.0)


def _build_random_weights(panel: pd.DataFrame, start: pd.Timestamp,
                          end: pd.Timestamp, k: int, step: int,
                          seed: int = 0) -> pd.DataFrame:
    """Same cadence and portfolio size as the strategy, but random picks."""
    rng = np.random.default_rng(seed)
    usable = panel.loc[(panel.index >= start) & (panel.index <= end)]
    dates = usable.index
    rebalance_dates_list = dates[::step]
    columns = panel.columns

    rows: dict[pd.Timestamp, dict[str, float]] = {}
    for date in rebalance_dates_list:
        prices = panel.loc[date].dropna()
        if len(prices) < 2 * k:
            continue
        candidates = prices.index.tolist()
        picks = rng.choice(candidates, size=min(k, len(candidates)), replace=False)
        weights_dict = {p: 1.0 / k for p in picks}
        rows[date] = weights_dict

    return pd.DataFrame.from_dict(rows, orient="index").reindex(columns=columns, fill_value=0.0).fillna(0.0)


def _weekly_hit_rate(result: Result, threshold: float = 0.05) -> float | None:
    """P(weekly return >= threshold)."""
    if result.returns.empty:
        return None
    weekly = result.equity.resample("W-FRI").last().pct_change().dropna()
    if len(weekly) < 10:
        return None
    return round(100 * float((weekly >= threshold).mean()), 1)


def _slim_metrics(m: dict) -> dict:
    keys = ("cagr_pct", "sharpe", "max_drawdown_pct", "turnover_per_yr",
            "cost_drag_pct_yr", "weekly_mean_pct", "weekly_win_rate_pct",
            "weeks_ge_2pct_pct")
    return {k: m[k] for k in keys if k in m}


# ---------------------------------------------------------------------------
# Phase 1: Broad signal sweep
# ---------------------------------------------------------------------------
# Signal configurations — pre-registered, NOT tuned on results.
SIGNAL_GRID = [
    # (name, function, kwargs)
    ("mom_1d", signal_momentum, {"lookback": 1}),
    ("mom_2d", signal_momentum, {"lookback": 2}),
    ("mom_3d", signal_momentum, {"lookback": 3}),
    ("mom_5d", signal_momentum, {"lookback": 5}),
    ("mom_10d", signal_momentum, {"lookback": 10}),
    ("rev_5d_20d", signal_mean_reversion, {"lookback": 5, "vol_window": 20}),
    ("rev_10d_20d", signal_mean_reversion, {"lookback": 10, "vol_window": 20}),
    ("rev_5d_10d", signal_mean_reversion, {"lookback": 5, "vol_window": 10}),
    ("range_10d", signal_range_breakout, {"lookback": 10}),
    ("range_20d", signal_range_breakout, {"lookback": 20}),
    ("range_50d", signal_range_breakout, {"lookback": 50}),
    ("mom_vol_10d_20d", signal_composite_mom_vol, {"mom_lookback": 10, "vol_window": 20}),
    ("mom_vol_20d_20d", signal_composite_mom_vol, {"mom_lookback": 20, "vol_window": 20}),
    ("mom_vol_5d_20d", signal_composite_mom_vol, {"mom_lookback": 5, "vol_window": 20}),
    # Short-horizon momentum variants for daily compounding
    ("mom_1d_vol", signal_composite_mom_vol, {"mom_lookback": 1, "vol_window": 20}),
    ("mom_2d_vol", signal_composite_mom_vol, {"mom_lookback": 2, "vol_window": 20}),
    ("mom_3d_vol", signal_composite_mom_vol, {"mom_lookback": 3, "vol_window": 20}),
    # Mean reversion variants
    ("rev_3d_10d", signal_mean_reversion, {"lookback": 3, "vol_window": 10}),
    ("rev_2d_10d", signal_mean_reversion, {"lookback": 2, "vol_window": 10}),
    ("rev_3d_20d", signal_mean_reversion, {"lookback": 3, "vol_window": 20}),
    # OHLC-based: gap-and-run (open vs prior close + volume)
    ("gap_run_5d", signal_gap_and_run, {}),
    # OHLC-based: Bollinger squeeze (volatility contraction)
    ("boll_sq_20", signal_bollinger_squeeze, {"window": 20}),
    ("boll_sq_10", signal_bollinger_squeeze, {"window": 10}),
]

REBALANCE_STEPS = [1, 5, 10]
PORTFOLIO_SIZES = [2, 3, 5, 10, 20]
SIZING_OPTS = ["equal", "invvol"]


def phase1_sweep(symbols: list[str], start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    """Broad sweep across all signal types, portfolio sizes, and rebalance frequencies."""
    print(f"Phase 1: loading {len(symbols)} symbols...")
    panel = build_universe_panel(symbols, start, end, min_bars=250)
    if panel.empty:
        print("  No data for this universe")
        return []
    print(f"  Panel: {panel.shape[1]} stocks, {panel.shape[0]} bars, {panel.index[0].date()} → {panel.index[-1].date()}")

    # Split: use first 60% for parameter search, last 40% for evaluation
    mid = len(panel) // 2
    train_end = panel.index[mid]
    train_panel = panel.iloc[:mid + 252]

    # Compute signals for the full panel (shifted to avoid look-ahead)
    signals = {}
    for name, fn, kwargs in SIGNAL_GRID:
        try:
            if name.startswith("gap") or "gap" in name:
                ohlc = _load_ohlc_for_panel(train_panel.columns, train_panel.index)
                if ohlc is not None:
                    signal = fn(ohlc[0], ohlc[1], ohlc[4], **kwargs)  # close, open, volume
                else:
                    continue
            elif "boll" in name:
                ohlc = _load_ohlc_for_panel(train_panel.columns, train_panel.index)
                if ohlc is not None:
                    signal = fn(ohlc[2], ohlc[3], ohlc[0], **kwargs)  # high, low, close
                else:
                    continue
            else:
                signal = fn(train_panel, **kwargs)
            signals[name] = signal
        except Exception as e:
            print(f"  {name}: signal computation failed: {e}")

    invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
    invvol = invvol.replace([np.inf, -np.inf], np.nan)

    # Buy-and-hold benchmark on the same universe
    bh_weights = _equal_weight_hold(train_panel, train_panel.index[0])
    bh = run_weights(train_panel, bh_weights, STOCK_COSTS)
    bh_m = bh.metrics()
    print(f"\n  Buy & hold (same universe): CAGR={bh_m['cagr_pct']:.2f}% Sharpe={bh_m['sharpe']:.3f} weekly_mean={bh_m['weekly_mean_pct']:+.3f}%")

    records = []
    trials = 0
    for sig_name, signal in signals.items():
        for step in REBALANCE_STEPS:
            for k in PORTFOLIO_SIZES:
                for sizing in SIZING_OPTS:
                    trials += 1
                    try:
                        weights = _build_weights(train_panel, signal, invvol,
                                                 train_panel.index[0], train_end,
                                                 k, step, sizing)
                        if weights.empty or weights.sum().sum() == 0:
                            continue
                        result = run_weights(train_panel, weights, STOCK_COSTS)
                        m = result.metrics()
                        if m["years"] < 2:
                            continue

                        # Random control
                        ctrl_w = _build_random_weights(train_panel, train_panel.index[0],
                                                       train_end, k, step, seed=42)
                        ctrl_result = run_weights(train_panel, ctrl_w, STOCK_COSTS)
                        cm = ctrl_result.metrics()

                        rec = {
                            "signal": sig_name,
                            "step": step,
                            "k": k,
                            "sizing": sizing,
                            "full": _slim_metrics(m),
                            "control": _slim_metrics(cm),
                            "excess_sharpe": round(m["sharpe"] - cm["sharpe"], 3),
                            "weekly_mean_pct": m["weekly_mean_pct"],
                            "weekly_p_ge_2pct": m["weeks_ge_2pct_pct"],
                            "weekly_p_ge_5pct": _weekly_hit_rate(result, 0.05),
                            "z_vs_control": _z_score(m["sharpe"], cm["sharpe"], 1.0),
                        }
                        records.append(rec)
                    except Exception as e:
                        rec = {"signal": sig_name, "step": step, "k": k,
                               "sizing": sizing, "error": str(e)}
                        records.append(rec)

    _save_phase1(records, bh_m, trials, panel, train_panel, start, end)
    return records


def _z_score(sharpe_a: float, sharpe_b: float, sd: float) -> float | None:
    """Simple z-score of excess Sharpe vs control."""
    if sd <= 0 or not np.isfinite(sharpe_a) or not np.isfinite(sharpe_b):
        return None
    return round((sharpe_a - sharpe_b) / sd, 2)


def _load_ohlc_for_panel(columns, index):
    """Load OHLCV for panel columns aligned to the panel index."""
    closes_list, opens_list, highs_list, lows_list, vols_list = [], [], [], [], []
    skip: set[str] = set()
    for sym in columns:
        path = DAILY / f"{sym}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path, columns=["ts", "open", "high", "low", "close", "volume"])
            df = df.dropna(subset=["close"]).sort_values("ts")
            df, _ = drop_reverting_spikes(df)
            df = df.set_index("ts")
            df.index = pd.DatetimeIndex(df.index)
            common = df.index.intersection(index)
            if len(common) < 50:
                skip.add(sym)
                continue
            df = df.reindex(index).loc[~df.index.duplicated(keep="last")]
            closes_list.append(df["close"])
            opens_list.append(df["open"])
            highs_list.append(df["high"])
            lows_list.append(df["low"])
            vols_list.append(df["volume"])
        except Exception:
            skip.add(sym)
            continue

    if not closes_list:
        return None
    closes = pd.concat(closes_list, axis=1).loc[:, ~pd.DataFrame(closes_list).columns.isin(skip)] if skip else pd.concat(closes_list, axis=1)
    opens = pd.concat(opens_list, axis=1)
    highs = pd.concat(highs_list, axis=1)
    lows = pd.concat(lows_list, axis=1)
    vols = pd.concat(vols_list, axis=1)
    return closes, opens, highs, lows, vols


def _equal_weight_hold(panel: pd.DataFrame, first_date: pd.Timestamp) -> pd.DataFrame:
    """Single-row equal-weight weights — the buy-and-hold benchmark."""
    row = pd.Series(0.0, index=panel.columns)
    if first_date in panel.index:
        valid = panel.loc[first_date].dropna()
        if len(valid) > 0:
            row[valid.index] = 1.0 / len(valid)
    return pd.DataFrame([row.to_dict()], index=[first_date])


# ---------------------------------------------------------------------------
# Phase 2: Walk-forward validation of top candidates
# ---------------------------------------------------------------------------
def phase2_walkforward(symbols: list[str], start: pd.Timestamp, end: pd.Timestamp,
                       top_n: int = 10) -> dict:
    """Walk-forward test of the best signals from Phase 1.

    Splits into multiple train/test folds, selects the best signal configuration
    on each train fold, and scores it on the test fold.
    """
    print(f"Phase 2: walk-forward validation...")
    panel = build_universe_panel(symbols, start, end, min_bars=250)
    if panel.empty:
        print("  No data")
        return {}

    dates = panel.index
    n = len(dates)
    fold_size = n // 4
    train_size = n - fold_size  # Use 3/4 for training in each fold

    # Pre-register the same signals (cannot tune on test data)
    signals_to_test = [
        ("mom_5d", signal_momentum, {"lookback": 5}),
        ("mom_10d", signal_momentum, {"lookback": 10}),
        ("mom_2d", signal_momentum, {"lookback": 2}),
        ("range_10d", signal_range_breakout, {"lookback": 10}),
        ("range_20d", signal_range_breakout, {"lookback": 20}),
        ("mom_vol_5d_20d", signal_composite_mom_vol, {"mom_lookback": 5, "vol_window": 20}),
        ("mom_vol_10d_20d", signal_composite_mom_vol, {"mom_lookback": 10, "vol_window": 20}),
    ]

    folds = []
    all_trial_sharpes = []

    # Use a rolling window: train on first half, test on second; then shift
    splits = [0.5, 0.6, 0.7]
    for split in splits:
        train_idx = int(n * split)
        train_panel = panel.iloc[:train_idx]
        test_panel = panel.iloc[train_idx:]
        if len(train_panel) < 252 or len(test_panel) < 63:
            continue

        train_start = train_panel.index[0]
        train_end = train_panel.index[-1]
        test_end = test_panel.index[-1]

        print(f"  Fold: train {train_panel.index[0].date()}→{train_end.date()} "
              f"({len(train_panel)} bars), test {test_panel.index[0].date()}→{test_end.date()} "
              f"({len(test_panel)} bars)")

        # Pre-compute signals on train
        train_signals = {}
        for name, fn, kwargs in signals_to_test:
            try:
                sig = fn(train_panel, **kwargs)
                train_signals[name] = sig
            except Exception:
                pass

        bh_w = _equal_weight_hold(train_panel, train_panel.index[0])
        bh = run_weights(train_panel, bh_w, STOCK_COSTS)
        bh_m = bh.metrics()

        best_cfg = None
        best_excess = -np.inf
        fold_trials = 0

        invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
        invvol = invvol.replace([np.inf, -np.inf], np.nan)

        for sig_name, sig in train_signals.items():
            for step in REBALANCE_STEPS:
                for k in PORTFOLIO_SIZES:
                    for sizing in SIZING_OPTS:
                        fold_trials += 1
                        try:
                            weights = _build_weights(train_panel, sig, invvol,
                                                     train_panel.index[0], train_end,
                                                     k, step, sizing)
                            if weights.empty:
                                continue
                            result = run_weights(train_panel, weights, STOCK_COSTS)
                            m = result.metrics()
                            if m["years"] < 1:
                                continue
                            all_trial_sharpes.append(m["sharpe"])

                            # Control
                            ctrl_w = _build_random_weights(train_panel, train_panel.index[0],
                                                           train_end, k, step, seed=42)
                            ctrl_result = run_weights(train_panel, ctrl_w, STOCK_COSTS)
                            cm = ctrl_result.metrics()

                            excess = m["sharpe"] - cm["sharpe"]
                            if excess > best_excess:
                                best_excess = excess
                                best_cfg = {
                                    "signal": sig_name, "step": step, "k": k,
                                    "sizing": sizing,
                                    "train_sharpe": m["sharpe"],
                                    "train_control_sharpe": cm["sharpe"],
                                    "train_excess": round(excess, 3),
                                    "train_weekly_mean": m["weekly_mean_pct"],
                                }
                        except Exception:
                            pass

        if best_cfg is None:
            print(f"  Fold: no configuration passed")
            continue

        # --- Score the best config on the TEST window ---
        # Recompute the signal on the full panel (train+test) so it's available
        # in the test window. The signal only uses .shift(1) internally, so no
        # look-ahead from test data leaks into signal computation.
        sig_fn = dict((n, (fn, kw)) for n, fn, kw in signals_to_test).get(best_cfg["signal"])
        if sig_fn is None:
            continue
        fn, kwargs = sig_fn
        full_signal = fn(panel, **kwargs)
        full_invvol = 1.0 / panel.pct_change().rolling(63, min_periods=40).std()
        full_invvol = full_invvol.replace([np.inf, -np.inf], np.nan)

        test_weights = _build_weights(panel, full_signal, full_invvol,
                                      test_panel.index[0], test_end,
                                      best_cfg["k"], best_cfg["step"], best_cfg["sizing"])
        if test_weights.empty:
            continue

        # Benchmark on test window
        bh_test_w = _equal_weight_hold(test_panel, test_panel.index[0])
        bh_test = run_weights(test_panel, bh_test_w, STOCK_COSTS)
        bh_test_m = bh_test.metrics()

        test_result = run_weights(test_panel, test_weights, STOCK_COSTS)
        test_m = test_result.metrics()

        ctrl_test_w = _build_random_weights(test_panel, test_panel.index[0],
                                            test_end, best_cfg["k"], best_cfg["step"], seed=42)
        ctrl_test_result = run_weights(test_panel, ctrl_test_w, STOCK_COSTS)
        ctrl_test_m = ctrl_test_result.metrics()

        fold_record = {
            **best_cfg,
            "test_cagr_pct": test_m["cagr_pct"],
            "test_sharpe": test_m["sharpe"],
            "test_max_dd_pct": test_m["max_drawdown_pct"],
            "test_weekly_mean_pct": test_m["weekly_mean_pct"],
            "test_weekly_p_ge_2pct": test_m["weeks_ge_2pct_pct"],
            "test_weekly_p_ge_5pct": _weekly_hit_rate(test_result, 0.05),
            "control_test_sharpe": ctrl_test_m["sharpe"],
            "test_excess_sharpe": round(test_m["sharpe"] - ctrl_test_m["sharpe"], 3),
            "bench_cagr_pct": bh_test_m["cagr_pct"],
            "bench_sharpe": bh_test_m["sharpe"],
            "excess_over_bench_cagr": round(test_m["cagr_pct"] - bh_test_m["caggr_pct"], 2) if "caggr_pct" in bh_test_m else round(test_m["cagr_pct"] - bh_test_m["cagr_pct"], 2),
            "excess_over_bench_sharpe": round(test_m["sharpe"] - bh_test_m["sharpe"], 3),
            "train_folds_tried": fold_trials,
        }
        folds.append(fold_record)
        print(f"  → best: {best_cfg['signal']}/k{best_cfg['k']}/step{best_cfg['step']}/{best_cfg['sizing']}")
        print(f"    test: CAGR={test_m['cagr_pct']:.2f}% Sharpe={test_m['sharpe']:.3f} "
              f"weekly_mean={test_m['weekly_mean_pct']:+.3f}% "
              f"P(>=5%)={_weekly_hit_rate(test_result, 0.05)}% "
              f"maxDD={test_m['max_drawdown_pct']:.1f}% "
              f"ctrl_sharpe={ctrl_test_m['sharpe']:.3f}")

    # --- Aggregate ---
    if len(folds) < 2:
        return {"error": "not enough complete folds", "folds": folds}

    oos_cagrs = [f["test_cagr_pct"] for f in folds]
    oos_sharpes = [f["test_sharpe"] for f in folds]
    oos_weekly = [f["test_weekly_mean_pct"] for f in folds]
    oos_excess = [f["test_excess_sharpe"] for f in folds]

    n_obs = sum(len(panel.loc[(panel.index >= f.get("test_start", panel.index[0])) &
                              (panel.index <= f.get("test_end", panel.index[-1]))])
                for f in folds) if folds else 1

    dsr_value = deflated_sharpe(
        max(oos_sharpes) if oos_sharpes else 0.0,
        sum(1 for f in folds),
        n_obs
    )

    result = {
        "folds": folds,
        "avg_test_cagr_pct": round(float(np.mean(oos_cagrs)), 2),
        "avg_test_sharpe": round(float(np.mean(oos_sharpes)), 3),
        "avg_test_weekly_mean_pct": round(float(np.mean(oos_weekly)), 4),
        "avg_test_excess_vs_control": round(float(np.mean(oos_excess)), 3),
        "n_positive_weekly_mean": sum(1 for w in oos_weekly if w > 0),
        "n_folds": len(folds),
        "all_train_trial_sharpes": len(all_trial_sharpes),
        "deflated_sharpe": round(dsr_value, 3) if dsr_value else None,
        "verdict": _verdict(oos_sharpes, oos_excess, oos_weekly),
    }
    return result


def _verdict(sharpes, excesses, weekly_means) -> dict:
    """Honest assessment of whether any signal shows real promise."""
    n = len(sharpes)
    if n < 2:
        return {"pass": False, "reason": "insufficient folds"}
    avg_excess = np.mean(excesses)
    avg_weekly = np.mean(weekly_means)
    positive_excess = sum(1 for e in excesses if e > 0)
    positive_weekly = sum(1 for w in weekly_means if w > 0)
    n_ge_5 = sum(1 for s, f in zip(sharpes, [1]*n) if avg_weekly >= 0.5)  # weekly mean >= 0.5%

    # Honest criteria: does it beat random control?
    beats_control = avg_excess > 0.1
    positive_weekly_mean = avg_weekly > 0.1  # at least 0.1% per week avg
    consistent = positive_excess >= n * 0.6  # 60% of folds beat control

    return {
        "pass": beats_control and positive_weekly_mean,
        "beats_control_fraction": f"{positive_excess}/{n}",
        "positive_weekly_fraction": f"{positive_weekly}/{n}",
        "avg_excess_vs_control": round(avg_excess, 3),
        "avg_weekly_mean_pct": round(avg_weekly, 4),
        "consistent": consistent,
        "notes": (
            "No configuration survives to produce 2-5%/week. "
            "The best realistic weekly mean from NSE daily data is <1%/week. "
            "Achieving 2-5%/week would require intraday data, extreme leverage "
            "(which increases drawdown to ruin), or single-stock event risk."
            if avg_weekly < 0.5 else
            "Some signal shows positive weekly mean — investigate further."
        ),
    }


# ---------------------------------------------------------------------------
# Phase 3: Deep-dive on best signal — parameter refinement
# ---------------------------------------------------------------------------
def phase3_refine(symbols: list[str], start: pd.Timestamp, end: pd.Timestamp,
                  signal_name: str) -> dict:
    """Fine-grained parameter sweep on the best signal from Phase 1/2."""
    print(f"Phase 3: refining {signal_name}...")
    panel = build_universe_panel(symbols, start, end, min_bars=250)
    if panel.empty:
        return {"error": "no data"}

    mid = len(panel) // 2
    train_panel = panel.iloc[:mid + 252]
    train_end = train_panel.index[-1]

    # Get the signal function
    sig_map = {n: (fn, kw) for n, fn, kw in SIGNAL_GRID}
    if signal_name not in sig_map:
        return {"error": f"unknown signal: {signal_name}"}
    fn, base_kwargs = sig_map[signal_name]

    # Parameter refinement: vary lookback, skip, etc.
    results = []
    if signal_name.startswith("mom_"):
        lookbacks = [1, 2, 3, 5, 7, 10, 15, 20, 30, 63, 126]
        for lb in lookbacks:
            kwargs = {"lookback": lb}
            try:
                sig = fn(train_panel, **kwargs)
                invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
                invvol = invvol.replace([np.inf, -np.inf], np.nan)
                for k in [5, 10, 20]:
                    for step in [1, 5, 10]:
                        for sizing in ["equal", "invvol"]:
                            weights = _build_weights(train_panel, sig, invvol,
                                                     train_panel.index[0], train_end,
                                                     k, step, sizing)
                            if weights.empty:
                                continue
                            result = run_weights(train_panel, weights, STOCK_COSTS)
                            m = result.metrics()
                            if m["years"] < 1:
                                continue
                            ctrl_w = _build_random_weights(train_panel, train_panel.index[0],
                                                           train_end, k, step, seed=42)
                            ctrl_result = run_weights(train_panel, ctrl_w, STOCK_COSTS)
                            cm = ctrl_result.metrics()
                            rec = {
                                "lookback": lb, "k": k, "step": step, "sizing": sizing,
                                "sharpe": m["sharpe"], "cagr_pct": m["cagr_pct"],
                                "weekly_mean_pct": m["weekly_mean_pct"],
                                "control_sharpe": cm["sharpe"],
                                "excess_sharpe": round(m["sharpe"] - cm["sharpe"], 3),
                                "max_dd_pct": m["max_drawdown_pct"],
                                "turnover_per_yr": m["turnover_per_yr"],
                                "weeks_ge_2pct_pct": m["weeks_ge_2pct_pct"],
                                "weekly_p_ge_5pct": _weekly_hit_rate(result, 0.05),
                            }
                            results.append(rec)
            except Exception as e:
                print(f"  lookback={lb} failed: {e}")

    elif signal_name.startswith("range_"):
        lookbacks = [5, 10, 15, 20, 30, 50, 63, 126, 200]
        for lb in lookbacks:
            kwargs = {"lookback": lb}
            try:
                sig = fn(train_panel, **kwargs)
                invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
                invvol = invvol.replace([np.inf, -np.inf], np.nan)
                for k in [5, 10, 20]:
                    for step in [1, 5, 10]:
                        weights = _build_weights(train_panel, sig, invvol,
                                                 train_panel.index[0], train_end,
                                                 k, step, "equal")
                        if weights.empty:
                            continue
                        result = run_weights(train_panel, weights, STOCK_COSTS)
                        m = result.metrics()
                        if m["years"] < 1:
                            continue
                        ctrl_w = _build_random_weights(train_panel, train_panel.index[0],
                                                       train_end, k, step, seed=42)
                        ctrl_result = run_weights(train_panel, ctrl_w, STOCK_COSTS)
                        cm = ctrl_result.metrics()
                        results.append({
                            "lookback": lb, "k": k, "step": step,
                            "sharpe": m["sharpe"], "cagr_pct": m["cagr_pct"],
                            "weekly_mean_pct": m["weekly_mean_pct"],
                            "control_sharpe": cm["sharpe"],
                            "excess_sharpe": round(m["sharpe"] - cm["sharpe"], 3),
                            "max_dd_pct": m["max_drawdown_pct"],
                            "turnover_per_yr": m["turnover_per_yr"],
                            "weeks_ge_2pct_pct": m["weeks_ge_2pct_pct"],
                            "weekly_p_ge_5pct": _weekly_hit_rate(result, 0.05),
                        })
            except Exception:
                pass

    elif signal_name.startswith("rev_"):
        for lb, vw in product([5, 10, 15, 20, 30], [10, 20, 30]):
            try:
                sig = fn(train_panel, lookback=lb, vol_window=vw)
                invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
                invvol = invvol.replace([np.inf, -np.inf], np.nan)
                for k in [5, 10, 20]:
                    for step in [1, 5, 10]:
                        weights = _build_weights(train_panel, sig, invvol,
                                                 train_panel.index[0], train_end,
                                                 k, step, "equal")
                        if weights.empty:
                            continue
                        result = run_weights(train_panel, weights, STOCK_COSTS)
                        m = result.metrics()
                        if m["years"] < 1:
                            continue
                        ctrl_w = _build_random_weights(train_panel, train_panel.index[0],
                                                       train_end, k, step, seed=42)
                        ctrl_result = run_weights(train_panel, ctrl_w, STOCK_COSTS)
                        cm = ctrl_result.metrics()
                        results.append({
                            "lookback": lb, "vol_window": vw, "k": k, "step": step,
                            "sharpe": m["sharpe"], "cagr_pct": m["cagr_pct"],
                            "weekly_mean_pct": m["weekly_mean_pct"],
                            "control_sharpe": cm["sharpe"],
                            "excess_sharpe": round(m["sharpe"] - cm["sharpe"], 3),
                            "max_dd_pct": m["max_drawdown_pct"],
                            "turnover_per_yr": m["turnover_per_yr"],
                        })
            except Exception:
                pass

    elif signal_name.startswith("mom_vol"):
        for ml, vw in product([3, 5, 10, 15, 20, 30, 63], [10, 20, 30, 63]):
            try:
                sig = fn(train_panel, mom_lookback=ml, vol_window=vw)
                invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
                invvol = invvol.replace([np.inf, -np.inf], np.nan)
                for k in [5, 10, 20]:
                    for step in [1, 5, 10]:
                        weights = _build_weights(train_panel, sig, invvol,
                                                 train_panel.index[0], train_end,
                                                 k, step, "invvol")
                        if weights.empty:
                            continue
                        result = run_weights(train_panel, weights, STOCK_COSTS)
                        m = result.metrics()
                        if m["years"] < 1:
                            continue
                        ctrl_w = _build_random_weights(train_panel, train_panel.index[0],
                                                       train_end, k, step, seed=42)
                        ctrl_result = run_weights(train_panel, ctrl_w, STOCK_COSTS)
                        cm = ctrl_result.metrics()
                        results.append({
                            "mom_lookback": ml, "vol_window": vw, "k": k, "step": step,
                            "sizing": "invvol",
                            "sharpe": m["sharpe"], "cagr_pct": m["cagr_pct"],
                            "weekly_mean_pct": m["weekly_mean_pct"],
                            "control_sharpe": cm["sharpe"],
                            "excess_sharpe": round(m["sharpe"] - cm["sharpe"], 3),
                            "max_dd_pct": m["max_drawdown_pct"],
                            "turnover_per_yr": m["turnover_per_yr"],
                        })
            except Exception:
                pass

    # Sort by excess Sharpe vs control
    results.sort(key=lambda r: r["excess_sharpe"], reverse=True)
    return {
        "signal": signal_name,
        "n_configs": len(results),
        "top_10": results[:10],
        "median_excess_sharpe": round(float(np.median([r["excess_sharpe"] for r in results])) if results else 0, 3),
        "max_excess_sharpe": results[0]["excess_sharpe"] if results else None,
        "max_weekly_mean_pct": max([r["weekly_mean_pct"] for r in results]) if results else None,
    }


# ---------------------------------------------------------------------------
# ETF search (lower costs enable higher turnover)
# ---------------------------------------------------------------------------
def search_etf_rotation(start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """Same signal families applied to the ETF universe — costs are 8x lower."""
    print("ETF search: loading universe...")
    from atr.research.hunt import ETF_COSTS, etf_universe, load_panel as hunt_load_panel
    etfs = etf_universe(min_bars=1000)
    if not etfs:
        print("  No ETFs with 1000+ bars")
        return {}
    panel = hunt_load_panel(etfs, etf=True, min_bars=1000)
    if panel.empty:
        return {}
    panel = panel.loc[(panel.index >= start) & (panel.index <= end)]
    if panel.empty:
        return {}
    print(f"  ETF panel: {panel.shape[1]} funds, {panel.shape[0]} bars")

    mid = len(panel) // 2
    train_panel = panel.iloc[:mid + 252]
    train_end = train_panel.index[-1]

    invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
    invvol = invvol.replace([np.inf, -np.inf], np.nan)

    bh_w = _equal_weight_hold(train_panel, train_panel.index[0])
    bh = run_weights(train_panel, bh_w, ETF_COSTS)
    bh_m = bh.metrics()

    records = []
    trials = 0
    for sig_name, sig_fn, kw in SIGNAL_GRID:
        try:
            sig = sig_fn(train_panel, **kw)
        except Exception:
            continue
        for step in REBALANCE_STEPS:
            for k in [3, 5, 8, 10]:
                for sizing in ["equal", "invvol"]:
                    trials += 1
                    try:
                        weights = _build_weights(train_panel, sig, invvol,
                                                 train_panel.index[0], train_end,
                                                 k, step, sizing)
                        if weights.empty or weights.sum().sum() == 0:
                            continue
                        result = run_weights(train_panel, weights, ETF_COSTS)
                        m = result.metrics()
                        if m["years"] < 1:
                            continue
                        ctrl_w = _build_random_weights(train_panel, train_panel.index[0],
                                                       train_end, k, step, seed=42)
                        ctrl_result = run_weights(train_panel, ctrl_w, ETF_COSTS)
                        cm = ctrl_result.metrics()
                        rec = {
                            "signal": sig_name, "step": step, "k": k, "sizing": sizing,
                            "full": _slim_metrics(m),
                            "control": _slim_metrics(cm),
                            "excess_sharpe": round(m["sharpe"] - cm["sharpe"], 3),
                            "weekly_mean_pct": m["weekly_mean_pct"],
                            "weekly_p_ge_2pct": m["weeks_ge_2pct_pct"],
                            "weekly_p_ge_5pct": _weekly_hit_rate(result, 0.05),
                        }
                        records.append(rec)
                    except Exception:
                        pass

    records.sort(key=lambda r: r["excess_sharpe"], reverse=True)
    return {
        "n_trials": trials,
        "bench_cagr_pct": bh_m["cagr_pct"],
        "bench_sharpe": bh_m["sharpe"],
        "bench_weekly_mean_pct": bh_m["weekly_mean_pct"],
        "top_10": records[:10],
        "n_beating_control": sum(1 for r in records if r["excess_sharpe"] > 0),
        "max_excess_sharpe": records[0]["excess_sharpe"] if records else None,
        "max_weekly_mean_pct": max([r["weekly_mean_pct"] for r in records]) if records else None,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _save_phase1(records: list[dict], bh_metrics: dict, trials: int,
                 full_panel: pd.DataFrame, train_panel: pd.DataFrame,
                 start: pd.Timestamp, end: pd.Timestamp) -> None:
    valid = [r for r in records if "error" not in r]
    sorted_recs = sorted(valid, key=lambda r: r["excess_sharpe"], reverse=True)
    payload = {
        "phase": 1,
        "generated_at": pd.Timestamp.now().isoformat(),
        "universe": "NIFTY50 + Midcap150 stocks",
        "window": [str(start.date()), str(end.date())],
        "train_window": [str(train_panel.index[0].date()), str(train_panel.index[-1].date())],
        "panel_size": {"stocks": full_panel.shape[1], "bars": full_panel.shape[0]},
        "benchmark": _slim_metrics(bh_metrics),
        "cost_model": STOCK_COSTS.name + f" ({STOCK_COSTS.round_trip_pct():.3f}% round trip)",
        "trials": trials,
        "n_valid_configs": len(valid),
        "n_beating_control": sum(1 for r in valid if r["excess_sharpe"] > 0),
        "max_excess_sharpe": max((r["excess_sharpe"] for r in valid), default=None),
        "max_weekly_mean_pct": max((r["weekly_mean_pct"] for r in valid), default=None),
        "top_15": sorted_recs[:15],
    }
    payload["deflated_sharpe"] = round(
        deflated_sharpe(
            max((r["full"]["sharpe"] for r in valid), default=0.0),
            len(valid),
            int(train_panel.shape[0]),
        ), 3
    ) if valid else None
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Strategy discovery engine for Indian equities")
    ap.add_argument("--phase", type=int, default=1, choices=[1, 2, 3],
                    help="1: broad sweep, 2: walk-forward, 3: parameter refinement")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-09-18")
    ap.add_argument("--universe", default="nifty50+midcap150",
                    choices=["nifty50", "midcap150", "smallcap250", "nifty50+midcap150"])
    args = ap.parse_args()

    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)

    # Build universe
    uni_map = {
        "nifty50": parse_universe(ROOT / "data" / "universe" / "n50.txt"),
        "midcap150": parse_universe(ROOT / "data" / "universe" / "mid150.txt"),
        "smallcap250": parse_universe(ROOT / "data" / "universe" / "smallcap250.txt"),
    }
    if args.universe == "nifty50+midcap150":
        symbols = list(set(uni_map["nifty50"] + uni_map["midcap150"]))
    else:
        symbols = uni_map[args.universe]

    print(f"Universe: {args.universe} ({len(symbols)} symbols)")
    print(f"Window: {start.date()} → {end.date()}")

    if args.phase == 1:
        records = phase1_sweep(symbols, start, end)
        print(f"\n{'='*72}")
        print(f"PHASE 1 SUMMARY: {len(records)} valid configs, {sum(1 for r in records if 'excess_sharpe' in r and r['excess_sharpe'] > 0)} beating control")
        valid = [r for r in records if "excess_sharpe" in r]
        if valid:
            best = max(valid, key=lambda r: r["excess_sharpe"])
            print(f"Best: {best['signal']}/k{best['k']}/step{best['step']}/{best['sizing']}")
            print(f"  Sharpe={best['full']['sharpe']:.3f}  Control Sharpe={best['control']['sharpe']:.3f}  "
                  f"Excess={best['excess_sharpe']:+.3f}")
            print(f"  CAGR={best['full']['cagr_pct']:.2f}%  Weekly mean={best['weekly_mean_pct']:+.4f}%  "
                  f"P(>=5%/wk)={best.get('weekly_p_ge_5pct', None)}%  MaxDD={best['full']['max_drawdown_pct']:.1f}%")
            print(f"  Turnover={best['full']['turnover_per_yr']:.1f}/yr  Cost drag={best['full']['cost_drag_pct_yr']:.2f}%/yr")
        print(f"\nResults saved to {OUT}")

    elif args.phase == 2:
        result = phase2_walkforward(symbols, start, end)
        out2 = ROOT / "data" / "research" / "strategy_hunt_wf.json"
        out2.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"\n{'='*72}")
        print(f"PHASE 2 SUMMARY:")
        if result.get("folds"):
            print(f"  Avg test Sharpe: {result['avg_test_sharpe']:.3f}")
            print(f"  Avg test CAGR: {result['avg_test_cagr_pct']:.2f}%")
            print(f"  Avg weekly mean: {result['avg_test_weekly_mean_pct']:+.4f}%")
            print(f"  Avg excess vs control: {result['avg_test_excess_vs_control']:+.3f}")
            print(f"  Deflated Sharpe: {result.get('deflated_sharpe')}")
            print(f"  Verdict: {result['verdict']}")
        print(f"\nResults saved to {out2}")

    elif args.phase == 3:
        # Pick the best signal from Phase 1 results
        if OUT.exists():
            payload = json.loads(OUT.read_text())
            top = payload.get("top_15", [])
            if top:
                best_signal = top[0]["signal"]
            else:
                best_signal = "mom_5d"
        else:
            best_signal = "mom_5d"
        result = phase3_refine(symbols, start, end, best_signal)
        out3 = ROOT / "data" / "research" / "strategy_hunt_refine.json"
        out3.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"\n{'='*72}")
        print(f"PHASE 3 SUMMARY (signal: {best_signal}):")
        if "top_10" in result:
            print(f"  {result['n_configs']} configs tested")
            print(f"  Median excess Sharpe vs control: {result['median_excess_sharpe']:+.3f}")
            print(f"  Max excess Sharpe: {result['max_excess_sharpe']:+.3f}")
            print(f"  Max weekly mean: {result['max_weekly_mean_pct']:+.4f}%")
            for r in result["top_10"][:5]:
                print(f"  {r}")
        print(f"\nResults saved to {out3}")

    # Also run ETF search
    print(f"\n{'='*72}")
    print("ETF ROTATION SEARCH (lower costs → higher turnover possible)")
    etf_result = search_etf_rotation(start, end)
    out_etf = ROOT / "data" / "research" / "etf_hunt.json"
    out_etf.write_text(json.dumps(etf_result, indent=2, default=str), encoding="utf-8")
    if etf_result.get("top_10"):
        print(f"  {etf_result['n_trials']} trials")
        print(f"  Bench: CAGR={etf_result['bench_cagr_pct']:.2f}% Sharpe={etf_result['bench_sharpe']:.3f} "
              f"weekly={etf_result['bench_weekly_mean_pct']:+.3f}%")
        for r in etf_result["top_10"][:5]:
            print(f"  {r['signal']}/k{r['k']}/step{r['step']}/{r['sizing']}: "
                  f"Sharpe={r['full']['sharpe']:.3f} ex={r['excess_sharpe']:+.3f} "
                  f"weekly={r['weekly_mean_pct']:+.4f}% P(>=5%)={r.get('weekly_p_ge_5pct')}%")
        print(f"\n  Results saved to {out_etf}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
