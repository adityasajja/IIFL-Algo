"""Backtested time-to-target and hit-rate stats per intelligent-alert setup.

A live signal card can show the entry, stop and target — the numbers computed
right now — but not "how long does this usually take, and how often does it
actually get there before the stop does". That needs history: replaying each
rule's exact trigger condition (the same conditions ``evaluate_stock_signals``
checks live) across cached daily bars, then walking forward from every
historical trigger with the same stop/target math ``build_signal_from_intelligent``
uses today, to see which came first and how many sessions it took.

Results are aggregated per *setup* (e.g. "RSI Oversold Dip"), across every
symbol scanned, because a handful of trades for one stock is not a sample —
one rule replayed over hundreds of stocks' history is.
"""

from __future__ import annotations

import glob
import os
import threading
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
from loguru import logger

from atr.alerts.intelligent import IntelligentAlertConfig
from atr.strategy.indicators import crossover, rsi, sma
from atr.trade_signals import TradeSignalSettings, _compute_stop_and_target


@dataclass
class SetupStat:
    #: Trades that resolved (hit either target or stop) within the window —
    #: the denominator for `hit_rate_pct`. Excludes trades still undecided
    #: when history ran out, which would understate how decisive the rule is.
    sample: int = 0
    hit_rate_pct: float | None = None
    median_days_to_target: float | None = None
    median_days_to_stop: float | None = None


def _first_cross_below(series: pd.Series, threshold: float) -> pd.Series:
    """True only on the day a series first drops to/under `threshold`, not every day it stays there."""
    below = series <= threshold
    return below & ~below.shift(1, fill_value=False)


def _first_cross_above(series: pd.Series, threshold: float) -> pd.Series:
    above = series >= threshold
    return above & ~above.shift(1, fill_value=False)


def _walk_forward(
    df: pd.DataFrame, i: int, action: str, stop: float, target: float, max_hold_days: int
) -> tuple[str, int | None]:
    """From the bar after a trigger, which is touched first: stop or target?

    Whichever is checked first when both land on the same bar is the
    conservative read — the same convention any bar-level (not intrabar)
    backtest has to make, since a daily bar alone cannot say which came first.
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    end = min(i + max_hold_days, len(df) - 1)
    for j in range(i + 1, end + 1):
        if action == "BUY":
            if lows[j] <= stop:
                return "stop", j - i
            if highs[j] >= target:
                return "target", j - i
        else:  # SELL here means "exit an existing long" — see _compute_stop_and_target
            if highs[j] >= stop:
                return "stop", j - i
            if lows[j] <= target:
                return "target", j - i
    return "none", None


def _triggers_for_symbol(
    df: pd.DataFrame, cfg: IntelligentAlertConfig
) -> dict[str, list[tuple[int, str]]]:
    """metric -> [(bar index, action)], replaying each enabled rule over the whole frame."""
    c = df["close"]
    s20 = sma(c, 20)
    s50 = sma(c, 50)
    rsi_vals = rsi(c)
    out: dict[str, list[tuple[int, str]]] = {}

    if cfg.buy_golden_cross:
        idx = np.flatnonzero(crossover(s20, s50).to_numpy())
        out["Golden Cross (SMA 20/50)"] = [(int(i), "BUY") for i in idx]

    if cfg.buy_rsi_oversold:
        idx = np.flatnonzero(_first_cross_below(rsi_vals, cfg.buy_rsi_threshold).to_numpy())
        out["RSI Oversold Dip"] = [(int(i), "BUY") for i in idx]

    if cfg.buy_breakout_vol and "volume" in df.columns:
        recent_high = df["high"].rolling(20).max()
        vol_avg = df["volume"].rolling(20).mean()
        vol_x = df["volume"] / vol_avg.replace(0, np.nan)
        trig = (c > recent_high) & (vol_x >= 1.5)
        idx = np.flatnonzero(trig.fillna(False).to_numpy())
        out["Breakout + Vol Spike"] = [(int(i), "BUY") for i in idx]

    if cfg.sell_sma_breakdown:
        below = c < s20
        trig = below & ~below.shift(1, fill_value=False) & s20.notna()
        idx = np.flatnonzero(trig.to_numpy())
        out["SMA20 Breakdown"] = [(int(i), "SELL") for i in idx]

    if cfg.sell_rsi_overbought:
        idx = np.flatnonzero(_first_cross_above(rsi_vals, cfg.sell_rsi_threshold).to_numpy())
        out["RSI Overbought"] = [(int(i), "SELL") for i in idx]

    if cfg.sell_trailing_stop_enabled:
        recent_high = df["high"].rolling(20).max()
        drop_pct = (c / recent_high - 1) * 100
        below = drop_pct <= -abs(cfg.sell_trailing_stop_pct)
        trig = below & ~below.shift(1, fill_value=False)
        idx = np.flatnonzero(trig.fillna(False).to_numpy())
        out[f"Trailing Drop > {cfg.sell_trailing_stop_pct}%"] = [(int(i), "SELL") for i in idx]

    return out


def compute_setup_stats(
    frames: dict[str, pd.DataFrame],
    cfg: IntelligentAlertConfig,
    settings: TradeSignalSettings | None = None,
    max_hold_days: int = 40,
    min_bars: int = 60,
) -> dict[str, SetupStat]:
    """Replay every enabled rule across `frames`' history and score it.

    `min_bars` skips a trigger too early in a symbol's history to have a
    stable SMA50/ATR behind it; `max_hold_days` is the window a signal is
    given to resolve before it's counted as neither — the same cap keeps one
    thin, still-open trade from dragging the whole sample out.
    """
    settings = settings or TradeSignalSettings()
    targets: dict[str, int] = {}
    stops: dict[str, int] = {}
    days_to_target: dict[str, list[int]] = {}
    days_to_stop: dict[str, list[int]] = {}

    for symbol, raw in frames.items():
        if raw is None or len(raw) < min_bars + max_hold_days:
            continue
        df = raw.sort_values("ts").reset_index(drop=True) if "ts" in raw.columns else raw.reset_index(drop=True)
        try:
            triggers = _triggers_for_symbol(df, cfg)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not sink the whole stat
            logger.debug("setup-stat replay failed for {}: {}", symbol, exc)
            continue

        for metric, points in triggers.items():
            for i, action in points:
                if i < min_bars or i >= len(df) - 1:
                    continue
                entry = float(df["close"].iloc[i])
                if entry <= 0:
                    continue
                stop, target = _compute_stop_and_target(df.iloc[: i + 1], entry, action, settings)
                outcome, days = _walk_forward(df, i, action, stop, target, max_hold_days)
                if outcome == "target":
                    targets[metric] = targets.get(metric, 0) + 1
                    days_to_target.setdefault(metric, []).append(days)
                elif outcome == "stop":
                    stops[metric] = stops.get(metric, 0) + 1
                    days_to_stop.setdefault(metric, []).append(days)

    out: dict[str, SetupStat] = {}
    for metric in set(targets) | set(stops):
        hit = targets.get(metric, 0)
        missed = stops.get(metric, 0)
        decided = hit + missed
        tgt_days = days_to_target.get(metric, [])
        stop_days = days_to_stop.get(metric, [])
        out[metric] = SetupStat(
            sample=decided,
            hit_rate_pct=round(hit / decided * 100, 1) if decided else None,
            median_days_to_target=round(float(np.median(tgt_days)), 1) if tgt_days else None,
            median_days_to_stop=round(float(np.median(stop_days)), 1) if stop_days else None,
        )
    return out


# ─── Cache: this is a ~60s replay over hundreds of symbols' history, so it is
# computed once in the background and refreshed on a TTL — never on a request.
_STATS_CACHE: dict[str, SetupStat] = {}
_STATS_AT = 0.0
_STATS_TTL_S = 12 * 3600.0
_STATS_LOCK = threading.Lock()
_STATS_BUILDING = False
_SAMPLE_SIZE = 300


def get_setup_stats() -> dict[str, SetupStat]:
    """The cached stats. Never blocks: a stale-but-present cache is refreshed
    in the background; a cold one (server just started) returns empty until
    the warm-up thread fills it in."""
    now = time.monotonic()
    if _STATS_CACHE and now - _STATS_AT >= _STATS_TTL_S:
        warm_setup_stats(background=True)
    return _STATS_CACHE


def warm_setup_stats(*, background: bool = True) -> None:
    """(Re)compute the cache. `background=False` blocks — only for startup
    warm-up or an explicit admin refresh, never a request handler."""
    global _STATS_BUILDING
    with _STATS_LOCK:
        if _STATS_BUILDING:
            return
        _STATS_BUILDING = True

    def work() -> None:
        global _STATS_CACHE, _STATS_AT, _STATS_BUILDING
        try:
            from atr.alerts.intelligent import load_intelligent_config
            from atr.data.history import load_cached

            import hashlib
            import pickle

            t0 = time.monotonic()
            files = sorted(glob.glob("data/iifl_daily/NSEEQ/*.parquet"))[:_SAMPLE_SIZE]
            symbols = [os.path.splitext(os.path.basename(f))[0] for f in files]
            cfg = load_intelligent_config()
            # A ~30s replay whose inputs are the sampled files, the rules and this code:
            # the same inputs give the same stats, so a restart reuses the stored ones.
            h = hashlib.sha256(repr(cfg).encode())
            for f in [*files, __file__]:
                try:
                    st = os.stat(f)
                    h.update(f"{os.path.basename(f)}:{st.st_mtime_ns}:{st.st_size}".encode())
                except OSError:
                    pass
            key = h.hexdigest()
            store = os.path.join("data", "panel", "setup_stats.pkl")
            stats = None
            try:
                with open(store, "rb") as fh:
                    stored_key, stored = pickle.load(fh)
                if stored_key == key:
                    stats = stored
            except Exception:  # noqa: BLE001 - missing or from other code: recompute
                pass
            frames: dict = {}
            if stats is None:
                frames = load_cached("NSEEQ", symbols=symbols)
                stats = compute_setup_stats(frames, cfg)
                try:
                    os.makedirs(os.path.dirname(store), exist_ok=True)
                    with open(store + ".tmp", "wb") as fh:
                        pickle.dump((key, stats), fh, protocol=pickle.HIGHEST_PROTOCOL)
                    os.replace(store + ".tmp", store)
                except Exception:  # noqa: BLE001 - an optimisation only
                    pass
            _STATS_CACHE = stats
            _STATS_AT = time.monotonic()
            logger.info(
                "setup stats refreshed: {} setups from {} symbols in {:.1f}s",
                len(stats), len(frames) or len(symbols), time.monotonic() - t0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("setup-stat warm-up failed: {}", exc)
        finally:
            with _STATS_LOCK:
                _STATS_BUILDING = False

    if background:
        threading.Thread(target=work, daemon=True, name="atr-setup-stats-warm").start()
    else:
        work()
