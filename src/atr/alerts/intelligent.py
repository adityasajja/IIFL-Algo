"""Intelligent Buy & Sell Signal Engine and Automated Periodic Monitor.

Continuously evaluates watched stocks and portfolio holdings against quantitative
trend, momentum, and risk indicators (SMA crossovers, RSI thresholds, trailing stops,
take-profit, and volume breakouts) and dispatches instant actionable Telegram alerts.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from loguru import logger
from pydantic import BaseModel, Field

from atr.alerts.channels import channels_from_settings
from atr.alerts.models import AlertEvent
from atr.config.settings import get_settings
from atr.data.history import CACHE_ROOT, load_cached
from atr.strategy.indicators import crossover, rsi, sma

IST = timezone(timedelta(hours=5, minutes=30))
CONFIG_PATH = Path("data/alerts/intelligent.json")


class IntelligentAlertConfig(BaseModel):
    enabled: bool = True
    universe: Literal["holdings", "watchlist", "both"] = "both"
    interval_min: int = 3  # 1, 3, 5, 15
    cooldown_min: int = 45  # Cooldown between repeated signals for the same stock

    # --- Sell / Exit Rules ---
    sell_sma_breakdown: bool = True  # Price crosses below SMA20
    sell_rsi_overbought: bool = True
    sell_rsi_threshold: float = 75.0
    sell_take_profit_enabled: bool = True
    sell_take_profit_pct: float = 8.0  # +8% gain target
    sell_stop_loss_enabled: bool = True
    sell_stop_loss_pct: float = 4.0  # -4% loss cut
    sell_trailing_stop_enabled: bool = True
    sell_trailing_stop_pct: float = 3.0  # -3% drop from 20-day high

    # Confluence needed, in weighted points, before the "soft" signals below
    # (trend breakdown, RSI overbought, trailing stop) combine into a single
    # exit call. Stop-loss and take-profit are hard exits and bypass this —
    # a breached risk limit or hit target doesn't need a second opinion.
    sell_composite_min_score: float = 2.0

    # --- Capital rotation: a winner that stopped moving, vs. one moving now ---
    # See `atr.capital_rotation`. Off the damage-based exits above: nothing is
    # wrong with the position, the capital just has a better use.
    sell_stall_enabled: bool = False
    sell_stall_days: int = 10          # sessions of tight range before it counts as stalled
    sell_stall_atr_mult: float = 1.5   # window's range vs ATR(14) — lower = tighter/more certain
    sell_stall_min_gain_pct: float = 5.0  # must be up at least this much to call it "a winner gone quiet"

    # --- Buy / Entry Rules ---
    buy_golden_cross: bool = True  # SMA20 crosses above SMA50 within 3 bars
    buy_rsi_oversold: bool = True
    buy_rsi_threshold: float = 32.0
    buy_breakout_vol: bool = True  # Near 20-day high with > 1.5x avg volume
    buy_dip_sma20: bool = True  # Pullback bounce near 20-day SMA in strong uptrend
    buy_box_oscillation: bool = False  # Bounce off the floor of a tight trading range
    buy_box_lookback: int = 20      # bars defining the range
    buy_box_width_pct: float = 8.0  # range must be at most this wide (% of floor) to count as a "box"
    buy_box_floor_pct: float = 2.0  # must be within this % of the floor to count as "at support"


def load_intelligent_config() -> IntelligentAlertConfig:
    if CONFIG_PATH.exists():
        try:
            return IntelligentAlertConfig(**json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning("Could not read intelligent config, resetting: {}", e)
    return IntelligentAlertConfig()


def save_intelligent_config(cfg: IntelligentAlertConfig) -> IntelligentAlertConfig:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
    return cfg


def is_market_hours(now: datetime | None = None) -> bool:
    now = (now or datetime.now(tz=IST)).astimezone(IST)
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= mins <= (15 * 60 + 30)


class IntelligentSignal(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    reason: str
    price: float
    day_chg_pct: float
    rsi: float | None = None
    metric: str = ""
    ts: datetime = Field(default_factory=datetime.now)


def evaluate_stock_signals(
    symbol: str,
    df: pd.DataFrame,
    cfg: IntelligentAlertConfig,
    holding_info: dict[str, Any] | None = None,
) -> list[IntelligentSignal]:
    """Evaluates a single stock against buy and sell quantitative rules."""
    signals: list[IntelligentSignal] = []
    if df is None or len(df) < 30:
        return signals

    df = df.sort_values("ts").reset_index(drop=True)
    c = df["close"]
    last = float(c.iloc[-1])
    prev = float(c.iloc[-2]) if len(c) > 1 else last
    day_chg = round(((last / prev) - 1) * 100, 2)

    s20 = sma(c, 20)
    s50 = sma(c, 50)
    rsi_vals = rsi(c)
    last_rsi = round(float(rsi_vals.iloc[-1]), 1) if not rsi_vals.empty else 50.0

    last_s20 = float(s20.iloc[-1]) if not s20.empty else last
    prev_s20 = float(s20.iloc[-2]) if len(s20) > 1 else last_s20

    # -----------------
    # 1. SELL SIGNALS — one intelligent exit call per symbol
    # -----------------
    # Hard exits (breached risk limit or hit target) are deterministic P&L
    # facts and bypass scoring entirely — they fire alone, at max confidence.
    # Everything else (trend breakdown, RSI overbought, trailing stop) is a
    # "soft" warning sign that only means something in confluence, so those
    # are weighted and combined into a single composite call instead of
    # stacking separate alerts for what is really one deteriorating position.
    hard_exit: IntelligentSignal | None = None
    if holding_info:
        avg_price = float(holding_info.get("avg_price") or holding_info.get("buy_price") or 0)
        if avg_price > 0:
            pnl_pct = round(((last / avg_price) - 1) * 100, 2)
            if cfg.sell_take_profit_enabled and pnl_pct >= cfg.sell_take_profit_pct:
                hard_exit = IntelligentSignal(
                    symbol=symbol,
                    action="SELL",
                    reason=f"Target profit hit: Position at {pnl_pct:+.1f}% P&L (Target {cfg.sell_take_profit_pct}%). Lock gains!",
                    price=last,
                    day_chg_pct=day_chg,
                    rsi=last_rsi,
                    metric=f"Target +{cfg.sell_take_profit_pct}% Reached",
                )
            elif cfg.sell_stop_loss_enabled and pnl_pct <= -abs(cfg.sell_stop_loss_pct):
                hard_exit = IntelligentSignal(
                    symbol=symbol,
                    action="SELL",
                    reason=f"Max loss cut: Position down {pnl_pct:.1f}% P&L (Limit -{cfg.sell_stop_loss_pct}%). Protect capital.",
                    price=last,
                    day_chg_pct=day_chg,
                    rsi=last_rsi,
                    metric=f"Stop Loss -{cfg.sell_stop_loss_pct}%",
                )

    if hard_exit is not None:
        signals.append(hard_exit)
    else:
        score = 0.0
        reasons: list[str] = []
        metrics: list[str] = []

        # Trend breakdown: price drops below SMA20 when previously above
        if cfg.sell_sma_breakdown and last < last_s20 and prev >= prev_s20:
            score += 1.0
            metrics.append("SMA20 Breakdown")
            reasons.append(f"trend broke down (price ₹{last:,.2f} crossed below 20-SMA ₹{last_s20:,.2f})")

        # RSI overbought reversal risk
        if cfg.sell_rsi_overbought and last_rsi >= cfg.sell_rsi_threshold:
            score += 1.0
            metrics.append("RSI Overbought")
            reasons.append(f"momentum overbought (RSI {last_rsi:.1f} ≥ {cfg.sell_rsi_threshold})")

        # Trailing stop from recent 20-day peak
        recent_high = float(df["high"].tail(20).max())
        drop_from_high = ((last / recent_high) - 1) * 100
        if cfg.sell_trailing_stop_enabled and drop_from_high <= -abs(cfg.sell_trailing_stop_pct):
            score += 1.5
            metrics.append(f"Trailing Drop > {cfg.sell_trailing_stop_pct}%")
            reasons.append(f"down {drop_from_high:.1f}% from its 20-day high (₹{recent_high:,.2f})")

        if score >= cfg.sell_composite_min_score:
            signals.append(
                IntelligentSignal(
                    symbol=symbol,
                    action="SELL",
                    reason=(
                        f"Composite exit (confluence score {score:.1f}/{cfg.sell_composite_min_score:.1f}): "
                        + "; ".join(reasons) + "."
                    ),
                    price=last,
                    day_chg_pct=day_chg,
                    rsi=last_rsi,
                    metric="Composite Exit: " + ", ".join(metrics),
                )
            )

    # -----------------
    # 2. BUY SIGNALS
    # -----------------
    # A. Golden Cross: SMA20 crosses above SMA50 within the last 3 bars
    if cfg.buy_golden_cross:
        try:
            if bool(crossover(s20, s50).tail(3).any()):
                signals.append(
                    IntelligentSignal(
                        symbol=symbol,
                        action="BUY",
                        reason=f"Golden Cross: 20-SMA crossed above 50-SMA near ₹{last:,.2f} with RSI {last_rsi:.0f}.",
                        price=last,
                        day_chg_pct=day_chg,
                        rsi=last_rsi,
                        metric="Golden Cross (SMA 20/50)",
                    )
                )
        except Exception:
            pass

    # B. Oversold Bounce
    if cfg.buy_rsi_oversold and last_rsi <= cfg.buy_rsi_threshold:
        signals.append(
            IntelligentSignal(
                symbol=symbol,
                action="BUY",
                reason=f"Oversold territory: RSI at {last_rsi:.1f} (≤ {cfg.buy_rsi_threshold}). Potential mean-reversion opportunity.",
                price=last,
                day_chg_pct=day_chg,
                rsi=last_rsi,
                metric="RSI Oversold Dip",
            )
        )

    # C. Breakout with Volume Surge
    if cfg.buy_breakout_vol and "volume" in df.columns and len(df) >= 21:
        recent_20_high = float(df["high"].iloc[-21:-1].max())
        vol_avg = float(df["volume"].tail(20).mean())
        cur_vol = float(df["volume"].iloc[-1])
        vol_x = (cur_vol / vol_avg) if vol_avg > 0 else 1.0
        if last > recent_20_high and vol_x >= 1.5:
            signals.append(
                IntelligentSignal(
                    symbol=symbol,
                    action="BUY",
                    reason=f"Volume Breakout: Price broke 20-day high (₹{recent_20_high:,.2f}) on {vol_x:.1f}x average volume.",
                    price=last,
                    day_chg_pct=day_chg,
                    rsi=last_rsi,
                    metric="Breakout + Vol Spike",
                )
            )

    # D. Box Oscillation: price is sitting near the floor of a tight,
    # sideways range — a mean-reversion bounce rather than a trend trade.
    if cfg.buy_box_oscillation and len(df) >= cfg.buy_box_lookback:
        window = df.tail(cfg.buy_box_lookback)
        floor = float(window["low"].min())
        ceiling = float(window["high"].max())
        if floor > 0:
            box_width_pct = ((ceiling / floor) - 1) * 100
            dist_from_floor_pct = ((last / floor) - 1) * 100
            if box_width_pct <= cfg.buy_box_width_pct and 0 <= dist_from_floor_pct <= cfg.buy_box_floor_pct:
                signals.append(
                    IntelligentSignal(
                        symbol=symbol,
                        action="BUY",
                        reason=(
                            f"Box oscillation: range-bound {box_width_pct:.1f}% over "
                            f"{cfg.buy_box_lookback} bars (₹{floor:,.2f}–₹{ceiling:,.2f}), "
                            f"now {dist_from_floor_pct:.1f}% above the floor."
                        ),
                        price=last,
                        day_chg_pct=day_chg,
                        rsi=last_rsi,
                        metric="Box Floor Bounce",
                    )
                )

    return signals


class IntelligentMonitorManager:
    """Singleton background orchestrator for periodic automated stock evaluation."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_run: datetime | None = None
        self._recent_signals: list[dict[str, Any]] = []
        self._cooldown_tracker: dict[str, datetime] = {}  # "SYMBOL:ACTION:METRIC" -> last_alerted_at

    def get_status(self) -> dict[str, Any]:
        cfg = load_intelligent_config()
        return {
            "enabled": cfg.enabled,
            "running": self._running,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "market_open": is_market_hours(),
            "interval_min": cfg.interval_min,
            "universe": cfg.universe,
            "recent_signals": self._recent_signals[-30:],
        }

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Started Intelligent Alerts Periodic Monitor background task.")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Stopped Intelligent Alerts Periodic Monitor background task.")

    async def run_evaluation_cycle(self, force: bool = False) -> list[IntelligentSignal]:
        """Runs an evaluation cycle across target stocks and fires Telegram alerts."""
        cfg = load_intelligent_config()
        if not cfg.enabled and not force:
            return []

        from atr.api.legacy.alerts import _alert_store

        store = _alert_store()
        channels = channels_from_settings(get_settings())

        # Collect symbols to evaluate
        symbols_to_check: set[str] = set()
        holdings_dict: dict[str, dict[str, Any]] = {}

        # 1. Fetch Holdings if needed
        #
        # This used to read `h.get("symbol")` / `"avg_price"` / `"BuyAvgRate"` /
        # `"TotalQty"` off the raw IIFL holdings response — none of which exist
        # in that schema (it's `nseTradingSymbol` / `averageTradedPrice` /
        # `totalQuantity`), so `holdings_dict` was silently always empty. That
        # meant the P&L-based take-profit/stop-loss rule below never fired, and
        # every SELL this cycle generates for a held symbol always looked
        # identical to one for a symbol never bought. `_held_symbols()` is the
        # same lookup already fixed and verified against a live response.
        if cfg.universe in ("holdings", "both"):
            try:
                from atr.api.legacy.signals import _held_symbols

                for sym, info in _held_symbols().items():
                    symbols_to_check.add(sym)
                    holdings_dict[sym] = {
                        "avg_price": float(info.get("avg_price") or 0),
                        "qty": float(info.get("qty") or 0),
                    }
            except Exception as e:
                logger.debug("Could not fetch holdings for intelligent alert: {}", e)

        # 2. Add Watchlist symbols if needed
        if cfg.universe in ("watchlist", "both"):
            # Include standard universe + custom rules symbols
            for r in store.rules():
                symbols_to_check.add(r.symbol)
            # Default popular tickers
            default_watch = [
                "RELIANCE-EQ", "HDFCBANK-EQ", "INFY-EQ", "TCS-EQ", "SBIN-EQ",
                "ICICIBANK-EQ", "LT-EQ", "BHARTIARTL-EQ", "NIFTYBEES-EQ", "TATAMOTORS-EQ"
            ]
            symbols_to_check.update(default_watch)

        # Load daily frames from cache
        all_frames = load_cached("NSEEQ")
        generated_signals: list[IntelligentSignal] = []
        now = datetime.now()

        for sym in symbols_to_check:
            df = all_frames.get(sym)
            if df is None:
                # Try finding parquet directly
                for p in Path(CACHE_ROOT).glob(f"*/{sym}.parquet"):
                    try:
                        df = pd.read_parquet(p)
                        break
                    except Exception:
                        pass
            if df is None or len(df) < 20:
                continue

            signals = evaluate_stock_signals(sym, df, cfg, holdings_dict.get(sym))
            for sig in signals:
                tracker_key = f"{sig.symbol}:{sig.action}:{sig.metric}"
                last_sent = self._cooldown_tracker.get(tracker_key)
                if last_sent and (now - last_sent) < timedelta(minutes=cfg.cooldown_min):
                    continue  # Cooldown active

                self._cooldown_tracker[tracker_key] = now
                generated_signals.append(sig)

                # Format Telegram Notification
                icon = "🟢 [BUY SIGNAL]" if sig.action == "BUY" else "🔴 [SELL SIGNAL]"
                header = f"ATR Intelligent Trigger: {icon} {sig.symbol.replace('-EQ', '')}"
                detail = (
                    f"{sig.reason}\n"
                    f"• Price: ₹{sig.price:,.2f} ({sig.day_chg_pct:+.2f}%)\n"
                    f"• RSI: {sig.rsi if sig.rsi else '—'}\n"
                    f"• Strategy Metric: {sig.metric}\n"
                    f"• Time: {now.strftime('%I:%M %p IST')}"
                )

                # Send down channels
                sent_on = "log"
                for ch in channels:
                    try:
                        if ch.send(header, detail):
                            sent_on = ch.name
                            break
                    except Exception as err:
                        logger.warning("Channel send failed: {}", err)

                # Log event in AlertStore
                evt = AlertEvent(
                    rule_id=f"intel-{sig.action.lower()}",
                    rule=f"{icon} {sig.symbol.replace('-EQ', '')} · {sig.metric}",
                    message=detail,
                    channel=sent_on,
                    ok=True,
                )
                store.log(evt)
                self._recent_signals.append({
                    "symbol": sig.symbol,
                    "action": sig.action,
                    "reason": sig.reason,
                    "price": sig.price,
                    "day_chg_pct": sig.day_chg_pct,
                    "rsi": sig.rsi,
                    "metric": sig.metric,
                    "ts": now.isoformat(),
                    "channel": sent_on,
                })

                # Also queue as a semi-automatic trade signal if it's actionable.
                #
                # A SELL only means something if there is something to sell. The
                # rules above run over the whole `symbols_to_check` set — holdings
                # *plus* a hardcoded watchlist (RELIANCE-EQ, TCS-EQ, ...) when
                # `cfg.universe` includes "watchlist" — so "trend broke down" /
                # "RSI overbought" / "trailing stop" fire for a symbol never
                # bought just as readily as for one actually held. Queuing that
                # as an executable trade signal put a real, approvable SELL
                # order (with a fabricated stop/target/quantity, as if opening a
                # fresh short) in front of the user for stock sitting in nobody's
                # portfolio. The Telegram/log alert above stays either way — a
                # "this broke down" notice is still useful watchlist intel — only
                # the *actionable, executable* queue entry is gated on holding it.
                if sig.action == "SELL" and sym not in holdings_dict:
                    continue
                try:
                    from atr.trade_signals import (
                        build_signal_from_intelligent,
                        get_queue,
                    )
                    from atr.trade_signals import (
                        load_settings as load_ts_settings,
                    )
                    ts_settings = load_ts_settings()
                    tq = get_queue()
                    if tq.active_count() < ts_settings.max_active:
                        already_pending = [s for s in tq.pending() if s.symbol == sig.symbol and s.action == sig.action]
                        if not already_pending:
                            trade_sig = build_signal_from_intelligent(
                                symbol=sig.symbol,
                                action=sig.action,
                                setup=sig.metric,
                                reason=sig.reason,
                                entry_price=sig.price,
                                df=df,
                                settings=ts_settings,
                                rsi_val=sig.rsi,
                            )
                            tq.add(trade_sig)
                except Exception as ex:
                    logger.debug("Could not queue trade signal from intelligent monitor: {}", ex)

        # 3. Capital rotation: a held winner that has gone quiet, paired with
        # whatever is showing real momentum right now. Separate from the
        # per-symbol loop above because naming a replacement requires looking
        # across the *whole* scanned universe, not just the one symbol being
        # evaluated — see `atr.capital_rotation`.
        if cfg.sell_stall_enabled and holdings_dict:
            try:
                from atr.capital_rotation import best_momentum_candidate, read_stall
                from atr.trade_signals import (
                    build_signal_from_intelligent,
                    get_queue,
                )
                from atr.trade_signals import load_settings as load_ts_settings

                ts_settings = load_ts_settings()
                tq = get_queue()
                for sym, info in holdings_dict.items():
                    df = all_frames.get(sym)
                    if df is None:
                        continue
                    avg_price = float(info.get("avg_price") or 0)
                    stall = read_stall(
                        df, avg_price,
                        window_days=cfg.sell_stall_days,
                        tight_atr_mult=cfg.sell_stall_atr_mult,
                        min_gain_pct=cfg.sell_stall_min_gain_pct,
                    )
                    if stall is None:
                        continue

                    tracker_key = f"{sym}:ROTATE"
                    last_sent = self._cooldown_tracker.get(tracker_key)
                    if last_sent and (now - last_sent) < timedelta(minutes=cfg.cooldown_min):
                        continue

                    candidate = best_momentum_candidate(all_frames, exclude=set(holdings_dict) | {sym})
                    if candidate is None:
                        continue  # nothing better to rotate into right now

                    self._cooldown_tracker[tracker_key] = now
                    reason_a = (
                        f"Stalled {stall.days_stalled} sessions (range only {stall.range_over_atr:.1f}x "
                        f"ATR) since peaking at ₹{stall.high_in_window:,.2f} — up {stall.gain_from_avg_pct:+.1f}% "
                        f"from your average, but not moving. {candidate.symbol.replace('-EQ', '')} is showing "
                        f"real momentum now ({candidate.roc_pct:+.1f}% in 20d, RSI {candidate.rsi:.0f}) — "
                        f"consider rotating this capital there."
                    )
                    header = f"ATR Intelligent Trigger: 🔄 [ROTATE] {sym.replace('-EQ', '')} → {candidate.symbol.replace('-EQ', '')}"
                    for ch in channels:
                        try:
                            if ch.send(header, reason_a):
                                break
                        except Exception as err:
                            logger.warning("Channel send failed: {}", err)
                    store.log(AlertEvent(
                        rule_id="intel-rotate",
                        rule=f"🔄 [ROTATE] {sym.replace('-EQ', '')} → {candidate.symbol.replace('-EQ', '')}",
                        message=reason_a,
                        channel="log",
                        ok=True,
                    ))

                    if tq.active_count() + 1 >= ts_settings.max_active:
                        continue
                    already = [s for s in tq.pending() if s.symbol == sym and s.setup == "Capital Rotation"]
                    if already:
                        continue

                    sell_sig = build_signal_from_intelligent(
                        symbol=sym, action="SELL", setup="Capital Rotation",
                        reason=reason_a, entry_price=stall.last_price, df=df, settings=ts_settings,
                    )
                    # A rotation liquidates the actual position — sizing it off
                    # `_compute_quantity`'s risk-based guess (right for opening
                    # a *new* trade) would sell an arbitrary amount instead of
                    # what is actually held.
                    held_qty = info.get("qty")
                    if held_qty:
                        sell_sig.quantity = int(held_qty)
                        sell_sig.risk_amount = round(
                            sell_sig.quantity * abs(sell_sig.entry_price - sell_sig.stop_loss), 2
                        )
                    tq.add(sell_sig)

                    buy_reason = (
                        f"Rotation candidate for {sym.replace('-EQ', '')}: "
                        f"{candidate.roc_pct:+.1f}% over 20 sessions, RSI {candidate.rsi:.0f}, "
                        f"trading above both its 20- and 50-day averages."
                    )
                    buy_sig = build_signal_from_intelligent(
                        symbol=candidate.symbol, action="BUY", setup="Rotation Candidate",
                        reason=buy_reason, entry_price=candidate.price,
                        df=all_frames[candidate.symbol], settings=ts_settings, rsi_val=candidate.rsi,
                    )
                    tq.add(buy_sig)
            except Exception as exc:
                logger.debug("Capital rotation check failed: {}", exc)

        self._last_run = now
        return generated_signals

    async def _loop(self) -> None:
        while self._running:
            try:
                cfg = load_intelligent_config()
                if cfg.enabled and is_market_hours():
                    logger.debug("Executing scheduled intelligent alert evaluation cycle...")
                    await self.run_evaluation_cycle(force=False)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in intelligent alert loop: {}", e)

            # Wait for user configured interval
            cfg = load_intelligent_config()
            interval_sec = max(60, cfg.interval_min * 60)
            await asyncio.sleep(interval_sec)


# Global singleton instance
_monitor_instance = IntelligentMonitorManager()


def get_intelligent_monitor() -> IntelligentMonitorManager:
    return _monitor_instance
