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

from atr.signals.models import EntryRules, ExitRules, SessionContext, Signal
from atr.strategy.indicators import rsi, sma

#: Order exits are reported in. A stop-loss outranks a take-profit.
_EXIT_PRIORITY = ["stop_loss", "trailing_stop", "trend_break", "take_profit", "rsi_overbought", "week_end"]

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


# --------------------------------------------------------------------------
# Indicator access
#
# A rolling indicator costs ~0.35ms in pandas almost regardless of how much
# data it covers, so recomputing four of them per bar per symbol dominated a
# walk-forward run (~3ms per symbol-bar, and a 200-combination sweep would have
# taken hours). If the caller has precomputed a column — as the backtest
# strategy does in prepare() — these read it instead.
#
# The fallback computes the identical quantity, so the live scanner and the
# backtest cannot disagree about what a rule means.
# --------------------------------------------------------------------------

_PRE_SMA = "_sma{}"
_PRE_RSI = "_rsi"
_PRE_PRIOR_HIGH = "_priorhigh{}"
_PRE_PRIOR_VOLUME = "_priorvol{}"


def _sma_series(frame: pd.DataFrame, window: int) -> pd.Series:
    key = _PRE_SMA.format(window)
    return frame[key] if key in frame.columns else sma(frame["close"], window)


def _rsi_series(frame: pd.DataFrame) -> pd.Series:
    return frame[_PRE_RSI] if _PRE_RSI in frame.columns else rsi(frame["close"])


def _rsi_n(frame: pd.DataFrame, window: int) -> pd.Series:
    """RSI over ``window`` bars; the precomputed column when it exists."""
    key = f"_rsi{window}"
    if key in frame.columns:
        return frame[key]
    return _rsi_series(frame) if window == 14 else rsi(frame["close"], window)


def _prior_high(frame: pd.DataFrame, lookback: int) -> pd.Series:
    """Highest high over the ``lookback`` bars *before* the current one."""
    key = _PRE_PRIOR_HIGH.format(lookback)
    if key in frame.columns:
        return frame[key]
    return frame["high"].shift(1).rolling(lookback).max()


def _prior_volume(frame: pd.DataFrame, window: int) -> pd.Series:
    """Average volume over the ``window`` bars *before* the current one."""
    key = _PRE_PRIOR_VOLUME.format(window)
    if key in frame.columns:
        return frame[key]
    return frame["volume"].shift(1).rolling(window).mean()


def precompute_indicators(frame: pd.DataFrame, entries: EntryRules, exits: ExitRules) -> None:
    """Add the columns the rules look for, in place.

    Called once by the backtest strategy's ``prepare()``. The live scanner skips
    it and computes on demand — one frame per symbol, so the cost is irrelevant
    there.
    """
    windows = {
        entries.trend_fast_sma,
        entries.trend_slow_sma,
        entries.long_sma,
        exits.trend_sma,
    }
    for window in windows:
        if window and window > 0:
            frame[_PRE_SMA.format(window)] = sma(frame["close"], window)
    frame[_PRE_RSI] = rsi(frame["close"])
    for window in {entries.triple_rsi_period, exits.rsi_period} - {14}:
        frame[f"_rsi{window}"] = rsi(frame["close"], window)
    if entries.triple_rsi_trend_sma:
        frame[_PRE_SMA.format(entries.triple_rsi_trend_sma)] = sma(
            frame["close"], entries.triple_rsi_trend_sma
        )
    frame[_PRE_PRIOR_HIGH.format(entries.breakout_lookback)] = _prior_high(
        frame, entries.breakout_lookback
    )
    frame[_PRE_PRIOR_VOLUME.format(entries.volume_lookback)] = _prior_volume(
        frame, entries.volume_lookback
    )


def eval_exit(
    symbol: str,
    frame: pd.DataFrame,
    avg_price: float,
    rules: ExitRules,
    *,
    quantity: float = 0.0,
    context: SessionContext | None = None,
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
        line = _sma_series(frame, rules.trend_sma)
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

    # --- end of the week -----------------------------------------------
    if rules.exit_at_week_end and context is not None and context.week_end_close:
        add("week_end", f"last session of the week: selling at {price:,.2f}")

    # --- overbought ----------------------------------------------------
    if (
        rules.rsi_overbought is not None
        and (not rules.close_only or context is None or context.near_close)
        and len(frame) >= 30
    ):
        value = _last(_rsi_n(frame, rules.rsi_period))
        if _finite(value) and float(value) >= rules.rsi_overbought:
            add(
                "rsi_overbought",
                f"RSI {float(value):.0f} at or above {rules.rsi_overbought:.0f}",
                rsi=round(float(value), 1),
            )

    return out


def eval_entry(
    symbol: str,
    frame: pd.DataFrame,
    rules: EntryRules,
    context: SessionContext | None = None,
) -> list[Signal]:
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

    def _wants(rule: str) -> bool:
        """A strategy that names its setup computes only that rule, not all and then filters."""
        return not rules.setup or rules.setup == rule

    # --- 1. uptrend, bought on a pullback ------------------------------
    fast_n, slow_n = rules.trend_fast_sma, rules.trend_slow_sma
    if _wants("trend_pullback") and len(frame) >= max(slow_n, 30):
        fast, slow = _last(_sma_series(frame, fast_n)), _last(_sma_series(frame, slow_n))
        value = _last(_rsi_series(frame))
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
    if _wants("breakout") and len(frame) >= max(lookback, rules.volume_lookback) + 1:
        prior_high = float(_prior_high(frame, lookback).iloc[-1])
        avg_volume = float(_prior_volume(frame, rules.volume_lookback).iloc[-1])
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
    if _wants("oversold_uptrend") and len(frame) >= rules.long_sma:
        long_ma = _last(_sma_series(frame, rules.long_sma))
        value = _last(_rsi_series(frame))
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

    # --- 4. Triple RSI: oversold, falling three days, still in an uptrend ----
    trend_n = rules.triple_rsi_trend_sma
    if (
        _wants("triple_rsi")
        and (not rules.close_only or context is None or context.near_close)
        and len(frame) >= max(trend_n, 10) + 1
    ):
        line = _last(_sma_series(frame, trend_n)) if trend_n else None
        r = _rsi_n(frame, rules.triple_rsi_period)
        now, d1, d2, d3 = (float(r.iloc[-1 - i]) for i in range(4))
        falling = now < d1 < d2 < d3
        in_trend = trend_n == 0 or (_finite(line) and price > float(line))
        if (
            falling
            and now < rules.triple_rsi_below
            and d3 < rules.triple_rsi_prior_below
            and in_trend
        ):
            add(
                "triple_rsi",
                f"RSI{rules.triple_rsi_period} {now:.0f}, down 3 days in a row, "
                f"above SMA{trend_n}",
                rsi=round(now, 1),
            )

    # --- 5. gap down at the open -------------------------------------------
    if rules.setup == "gap_down":
        gap = _gap_down(frame, rules, context)
        if gap is not None:
            add(
                "gap_down",
                f"opened {abs(gap['gap_pct']):.1f}% below the last close "
                f"{gap['prior_close']:,.2f}",
                **gap,
            )

    if rules.setup:
        out = [s for s in out if s.rule == rules.setup]
    return out


def _gap_down(
    frame: pd.DataFrame, rules: EntryRules, context: SessionContext | None
) -> dict[str, float] | None:
    """The gap's numbers when this bar qualifies, else None.

    With no context (a backtest) the bar's own open is used and the filters that need
    the live session (weekday, market) cannot be met, so a strategy that sets them does
    not fire there. Silent firing without the market filter would test a different plan.
    """
    if len(frame) < 3:
        return None
    prior_close = float(frame["close"].iloc[-2])
    if not _finite(prior_close) or prior_close <= 0:
        return None

    if context is None:
        if rules.gap_weekday is not None or rules.gap_market_min_pct is not None:
            return None
        open_price = float(frame["open"].iloc[-1])
        market = None
    else:
        if rules.gap_weekday is not None and context.today.weekday() != rules.gap_weekday:
            return None
        if context.open_price is None or context.minutes_since_open is None:
            return None  # the open was not seen: guessing it would be trading a different gap
        if context.minutes_since_open > rules.gap_entry_minutes:
            return None
        if rules.gap_market_min_pct is not None and (
            context.market_week_pct is None or context.market_week_pct <= rules.gap_market_min_pct
        ):
            return None
        if "ts" in frame.columns and context.prior_session is not None:
            last = frame["ts"].iloc[-2]
            if pd.isna(last) or pd.Timestamp(last).date() != context.prior_session:
                return None  # the history is stale: its last close is not the last session's
        open_price = float(context.open_price)
        market = context.market_week_pct

    if not _finite(open_price) or open_price <= 0:
        return None
    gap_pct = 100.0 * (open_price / prior_close - 1.0)
    if gap_pct > -rules.gap_down_pct:
        return None
    detail = {"gap_pct": round(gap_pct, 2), "open": round(open_price, 2), "prior_close": round(prior_close, 2)}
    if market is not None:
        detail["market_week_pct"] = round(float(market), 2)
    return detail


def primary_exit(signals: list[Signal]) -> Signal | None:
    """The most severe exit among several fired rules for one symbol."""
    if not signals:
        return None
    rank = {name: i for i, name in enumerate(_EXIT_PRIORITY)}
    return min(signals, key=lambda s: rank.get(s.rule, len(rank)))
