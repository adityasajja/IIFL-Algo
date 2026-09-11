"""Rule evaluation.

Deliberately pure: every function takes a daily OHLCV frame plus thresholds and
returns signals. No network, no broker, no clock — so the rules can be tested
directly and reused by both the live scanner and the backtest strategy.

Each rule reports *why* it fired and the numbers behind it, because a signal
you cannot audit is indistinguishable from a hunch.
"""

from __future__ import annotations

import math

import pandas as pd

from atr.signals.models import EntryRules, ExitRules, Signal
from atr.strategy.indicators import rsi, sma

#: Order exits are reported in. A stop-loss outranks a take-profit.
_EXIT_PRIORITY = ["stop_loss", "trailing_stop", "trend_break", "take_profit", "rsi_overbought"]

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def append_live_bar(
    frame: pd.DataFrame, price: float, volume: float | None = None
) -> pd.DataFrame:
    """Append today's live price as a forming bar.

    The daily cache lags by at least a session, so without this every rule
    would be evaluated against yesterday's close and the system would always be
    a day late.
    """
    if not math.isfinite(price) or price <= 0:
        return frame
    bar = {
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": float(volume) if volume is not None else float(frame["volume"].iloc[-1] or 0),
    }
    return pd.concat([frame, pd.DataFrame([bar])], ignore_index=True)


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _last(series: pd.Series):
    return series.iloc[-1] if len(series) else float("nan")


def eval_exit(
    symbol: str,
    frame: pd.DataFrame,
    avg_price: float,
    rules: ExitRules,
    *,
    quantity: float = 0.0,
) -> list[Signal]:
    """Apply the risk rules to one holding. Returns every rule that fired."""
    if frame is None or frame.empty or not _finite(avg_price) or avg_price <= 0:
        return []

    price = float(frame["close"].iloc[-1])
    pnl_pct = (price / avg_price - 1.0) * 100.0
    out: list[Signal] = []

    def add(rule: str, reason: str, **detail):
        out.append(
            Signal(
                symbol=symbol,
                action="SELL",
                rule=rule,
                reason=reason,
                price=price,
                detail={
                    "avg_price": round(avg_price, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "quantity": quantity,
                    "value": round(quantity * price, 2),
                    **detail,
                },
            )
        )

    # --- stop loss (needs no history at all) --------------------------
    if rules.stop_loss_pct and pnl_pct <= -abs(rules.stop_loss_pct):
        add(
            "stop_loss",
            f"down {pnl_pct:.1f}% against an average of {avg_price:,.2f}",
            threshold=-abs(rules.stop_loss_pct),
        )

    # --- take profit ---------------------------------------------------
    if rules.take_profit_pct and pnl_pct >= abs(rules.take_profit_pct):
        add(
            "take_profit",
            f"up {pnl_pct:.1f}% against an average of {avg_price:,.2f}",
            threshold=abs(rules.take_profit_pct),
        )

    # --- trailing stop -------------------------------------------------
    if rules.trailing_stop_pct:
        lookback = min(len(frame), 120)
        peak = float(frame["high"].tail(lookback).max())
        if _finite(peak) and peak > 0:
            off_peak = (price / peak - 1.0) * 100.0
            if off_peak <= -abs(rules.trailing_stop_pct):
                add(
                    "trailing_stop",
                    f"{off_peak:.1f}% below its {lookback}-bar high of {peak:,.2f}",
                    peak=round(peak, 2),
                    off_peak_pct=round(off_peak, 2),
                    threshold=-abs(rules.trailing_stop_pct),
                )

    # --- trend break ---------------------------------------------------
    confirm = max(int(rules.trend_confirm_bars), 1)
    if rules.trend_sma and len(frame) >= rules.trend_sma + confirm:
        line = sma(frame["close"], rules.trend_sma)
        closes = frame["close"].iloc[-confirm:]
        lines = line.iloc[-confirm:]
        if bool((closes < lines).all()):
            last_line = float(lines.iloc[-1])
            add(
                "trend_break",
                f"{confirm} closes below SMA{rules.trend_sma} "
                f"{last_line:,.2f} (now {price:,.2f})",
                sma=round(last_line, 2),
                confirm_bars=confirm,
            )

    # --- overbought ----------------------------------------------------
    if rules.rsi_overbought is not None and len(frame) >= 30:
        value = _last(rsi(frame["close"]))
        if _finite(value) and float(value) >= rules.rsi_overbought:
            add(
                "rsi_overbought",
                f"RSI {float(value):.0f} at or above {rules.rsi_overbought:.0f}",
                rsi=round(float(value), 1),
            )

    return out


def eval_entry(symbol: str, frame: pd.DataFrame, rules: EntryRules) -> list[Signal]:
    """Apply the candidate entry setups to one symbol."""
    if frame is None or frame.empty:
        return []
    price = float(frame["close"].iloc[-1])
    if not _finite(price) or price <= 0:
        return []

    out: list[Signal] = []

    def add(rule: str, reason: str, **detail):
        out.append(
            Signal(
                symbol=symbol,
                action="BUY",
                rule=rule,
                reason=reason,
                price=price,
                detail=detail,
                validated=False,
            )
        )

    close = frame["close"]

    # --- 1. uptrend, bought on a pullback ------------------------------
    fast_n, slow_n = rules.trend_fast_sma, rules.trend_slow_sma
    if len(frame) >= max(slow_n, 30):
        fast, slow = _last(sma(close, fast_n)), _last(sma(close, slow_n))
        value = _last(rsi(close))
        if _finite(fast) and _finite(slow) and _finite(value):
            uptrend = price > float(slow) and float(fast) > float(slow)
            pulled_back = rules.pullback_rsi_low <= float(value) <= rules.pullback_rsi_high
            if uptrend and pulled_back:
                add(
                    "trend_pullback",
                    f"uptrend (SMA{fast_n} {float(fast):,.2f} > SMA{slow_n} "
                    f"{float(slow):,.2f}) with RSI {float(value):.0f}",
                    rsi=round(float(value), 1),
                    sma_fast=round(float(fast), 2),
                    sma_slow=round(float(slow), 2),
                )

    # --- 2. breakout to new highs on volume ----------------------------
    lookback = rules.breakout_lookback
    if len(frame) >= max(lookback, rules.volume_lookback) + 1:
        prior_high = float(frame["high"].iloc[-(lookback + 1) : -1].max())
        avg_volume = float(frame["volume"].iloc[-(rules.volume_lookback + 1) : -1].mean())
        volume = float(frame["volume"].iloc[-1])
        if _finite(prior_high) and prior_high > 0 and _finite(avg_volume) and avg_volume > 0:
            near_high = price >= prior_high * (1 - rules.breakout_proximity_pct / 100.0)
            heavy = volume >= avg_volume * rules.volume_multiple
            if near_high and heavy:
                add(
                    "breakout",
                    f"within {rules.breakout_proximity_pct:.1f}% of the {lookback}-bar high "
                    f"{prior_high:,.2f} on {volume / avg_volume:.1f}x average volume",
                    prior_high=round(prior_high, 2),
                    volume_multiple=round(volume / avg_volume, 2),
                )

    # --- 3. oversold within a longer-term uptrend ----------------------
    if len(frame) >= rules.long_sma:
        long_ma = _last(sma(close, rules.long_sma))
        value = _last(rsi(close))
        if (
            _finite(long_ma)
            and _finite(value)
            and float(value) <= rules.oversold_rsi
            and price > float(long_ma)
        ):
            add(
                "oversold_uptrend",
                f"RSI {float(value):.0f} but still above SMA{rules.long_sma} "
                f"{float(long_ma):,.2f}",
                rsi=round(float(value), 1),
                sma_long=round(float(long_ma), 2),
            )

    return out


def primary_exit(signals: list[Signal]) -> Signal | None:
    """The most severe exit among several fired rules for one symbol."""
    if not signals:
        return None
    rank = {name: i for i, name in enumerate(_EXIT_PRIORITY)}
    return min(signals, key=lambda s: rank.get(s.rule, len(rank)))
