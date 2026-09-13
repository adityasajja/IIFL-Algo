"""Semi-automatic trade signal queue.

The algo surfaces candidate trades (with suggested entry, stop-loss, and
target). The user approves each one via the dashboard or Telegram. On
approval, orders are placed via the existing IIFL broker integration.

Flow
----
scan_for_signals()
    → creates TradeSignal objects (status=PENDING)
    → sends Telegram preview
    → stored in _SIGNAL_QUEUE (in-memory, persisted to disk)

user clicks Execute
    → execute_signal(id)  →  place entry + SL + target orders
    → signal moves to ACTIVE
    → Telegram confirmation sent

stop/target hit (monitored externally via positions)
    → signal moved to DONE
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from loguru import logger
from pydantic import BaseModel, Field

from atr.strategy.indicators import atr as _atr, rsi as _rsi, sma

IST = timezone(timedelta(hours=5, minutes=30))
QUEUE_PATH = Path("data/trade_signals/queue.json")
SETTINGS_PATH = Path("data/trade_signals/settings.json")


# ─── Settings ─────────────────────────────────────────────────────────────────

class TradeSignalSettings(BaseModel):
    capital: float = 500_000.0          # ₹5 lakh default
    risk_per_trade_pct: float = 1.0     # 1% of capital per trade
    stop_method: Literal["atr", "pct"] = "atr"
    stop_atr_mult: float = 1.5          # 1.5 × ATR below entry
    stop_pct: float = 2.0               # fixed 2% below entry
    rr_ratio: float = 2.0               # target = entry + 2× risk
    max_active: int = 5                 # max concurrent open signals
    exchange: str = "NSEEQ"
    product: str = "CNC"                # CNC = delivery, MIS = intraday


def load_settings() -> TradeSignalSettings:
    try:
        if SETTINGS_PATH.exists():
            return TradeSignalSettings(**json.loads(SETTINGS_PATH.read_text()))
    except Exception as e:
        logger.warning("trade_signals settings corrupt, resetting: {}", e)
    return TradeSignalSettings()


def save_settings(s: TradeSignalSettings) -> TradeSignalSettings:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(s.model_dump_json(indent=2))
    return s


# ─── Signal model ─────────────────────────────────────────────────────────────

class TradeSignal(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    symbol: str
    action: Literal["BUY", "SELL"]
    setup: str                          # "Breakout", "Oversold Pullback", etc.
    reason: str

    entry_price: float
    stop_loss: float
    target: float
    quantity: int

    risk_amount: float                  # ₹ at risk
    rr_ratio: float

    rsi: float | None = None
    vol_x: float | None = None

    # Quant literature & self-learning fields
    paper_citation: str | None = None
    thesis: str | None = None
    confidence_score: float | None = None
    expected_value: float | None = None
    regime_fit: str | None = None

    status: Literal["PENDING", "ACTIVE", "DONE", "SKIPPED"] = "PENDING"
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=IST))
    expires_at: datetime | None = None
    executed_at: datetime | None = None

    # order IDs after execution
    entry_order_id: str | None = None
    sl_order_id: str | None = None
    target_order_id: str | None = None

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(tz=IST) > self.expires_at


# ─── Signal computation ───────────────────────────────────────────────────────

def _compute_stop_and_target(
    df: pd.DataFrame,
    entry: float,
    action: Literal["BUY", "SELL"],
    settings: TradeSignalSettings,
) -> tuple[float, float]:
    """Return (stop_loss, target) for an entry."""
    if settings.stop_method == "atr" and len(df) >= 20:
        atr_val = float(_atr(df["high"], df["low"], df["close"]).iloc[-1])
        risk_pts = settings.stop_atr_mult * atr_val
    else:
        risk_pts = entry * settings.stop_pct / 100.0

    if action == "BUY":
        stop  = round(entry - risk_pts, 2)
        target = round(entry + risk_pts * settings.rr_ratio, 2)
    else:  # SELL (exit existing long)
        stop  = round(entry + risk_pts, 2)
        target = round(entry - risk_pts * settings.rr_ratio, 2)

    return stop, target


def _compute_quantity(
    entry: float,
    stop_loss: float,
    settings: TradeSignalSettings,
) -> int:
    """Risk-based position sizing."""
    risk_per_share = abs(entry - stop_loss)
    if risk_per_share <= 0:
        return 1
    max_loss = settings.capital * settings.risk_per_trade_pct / 100.0
    qty = int(max_loss / risk_per_share)
    return max(qty, 1)


def build_signal_from_intelligent(
    symbol: str,
    action: Literal["BUY", "SELL"],
    setup: str,
    reason: str,
    entry_price: float,
    df: pd.DataFrame,
    settings: TradeSignalSettings,
    rsi_val: float | None = None,
    vol_x: float | None = None,
    paper_citation: str | None = None,
    thesis: str | None = None,
    confidence_score: float | None = None,
    expected_value: float | None = None,
    regime_fit: str | None = None,
) -> TradeSignal:
    stop, target = _compute_stop_and_target(df, entry_price, action, settings)
    qty = _compute_quantity(entry_price, stop, settings)
    risk_amt = round(qty * abs(entry_price - stop), 2)
    rr = round(abs(target - entry_price) / max(abs(entry_price - stop), 0.01), 2)

    # Expire intraday at 14:30, positional at market close
    now = datetime.now(tz=IST)
    expires = now.replace(hour=14, minute=30, second=0, microsecond=0)
    if expires < now:
        expires = now + timedelta(days=1)

    return TradeSignal(
        symbol=symbol,
        action=action,
        setup=setup,
        reason=reason,
        entry_price=entry_price,
        stop_loss=stop,
        target=target,
        quantity=qty,
        risk_amount=risk_amt,
        rr_ratio=rr,
        rsi=rsi_val,
        vol_x=vol_x,
        paper_citation=paper_citation,
        thesis=thesis,
        confidence_score=confidence_score,
        expected_value=expected_value,
        regime_fit=regime_fit,
        expires_at=expires,
    )


# ─── In-memory queue with disk persistence ────────────────────────────────────

class SignalQueue:
    def __init__(self) -> None:
        self._signals: dict[str, TradeSignal] = {}
        self._load()

    def _load(self) -> None:
        try:
            if QUEUE_PATH.exists():
                raw = json.loads(QUEUE_PATH.read_text())
                for d in raw:
                    try:
                        s = TradeSignal(**d)
                        self._signals[s.id] = s
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("Could not load signal queue: {}", e)

    def _persist(self) -> None:
        try:
            QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = [s.model_dump(mode="json") for s in self._signals.values()]
            QUEUE_PATH.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            logger.warning("Could not persist signal queue: {}", e)

    def add(self, sig: TradeSignal) -> None:
        # Expire old pending signals for the same symbol+action
        for s in list(self._signals.values()):
            if s.symbol == sig.symbol and s.action == sig.action and s.status == "PENDING":
                s.status = "SKIPPED"
        self._signals[sig.id] = sig
        self._persist()

    def get(self, sig_id: str) -> TradeSignal | None:
        return self._signals.get(sig_id)

    def all(self) -> list[TradeSignal]:
        # Auto-expire timed-out pending signals
        for s in self._signals.values():
            if s.status == "PENDING" and s.is_expired():
                s.status = "SKIPPED"
        return sorted(self._signals.values(), key=lambda s: s.created_at, reverse=True)

    def pending(self) -> list[TradeSignal]:
        return [s for s in self.all() if s.status == "PENDING"]

    def active(self) -> list[TradeSignal]:
        return [s for s in self.all() if s.status == "ACTIVE"]

    def skip(self, sig_id: str) -> bool:
        s = self._signals.get(sig_id)
        if not s:
            return False
        s.status = "SKIPPED"
        self._persist()
        return True

    def mark_active(self, sig_id: str, entry_id: str, sl_id: str, tgt_id: str) -> None:
        s = self._signals.get(sig_id)
        if s:
            s.status = "ACTIVE"
            s.executed_at = datetime.now(tz=IST)
            s.entry_order_id = entry_id
            s.sl_order_id = sl_id
            s.target_order_id = tgt_id
            self._persist()

    def mark_done(self, sig_id: str, is_win: bool = True, r_multiple: float = 2.0) -> None:
        s = self._signals.get(sig_id)
        if s:
            s.status = "DONE"
            self._persist()

            # Feed outcome back into Bayesian self-learning bandit
            try:
                from atr.research.self_learning import get_self_learning_engine
                engine = get_self_learning_engine()
                # Derive strategy id
                strat_id = "momentum_jegadeesh_titman"
                if "Avellaneda" in s.setup:
                    strat_id = "stat_arb_avellaneda_lee"
                elif "Breakout" in s.setup:
                    strat_id = "volatility_breakout"
                elif "Multi-Factor" in s.setup:
                    strat_id = "multi_factor_composite"

                engine.record_trade_outcome(strat_id, is_win=is_win, r_multiple=r_multiple)
            except Exception as ex:
                logger.debug("Failed to feed outcome to self-learning engine: {}", ex)

    def active_count(self) -> int:
        return len(self.active())


# ─── Singleton ────────────────────────────────────────────────────────────────

_QUEUE = SignalQueue()


def get_queue() -> SignalQueue:
    return _QUEUE


# ─── Telegram formatting ──────────────────────────────────────────────────────

def format_telegram_preview(sig: TradeSignal) -> tuple[str, str]:
    icon = "🟢" if sig.action == "BUY" else "🔴"
    sym = sig.symbol.replace("-EQ", "")
    header = f"{icon} {sig.action} Signal — {sym}"
    body = (
        f"Setup: {sig.setup}\n"
        f"Reason: {sig.reason}\n\n"
        f"• Entry:  ₹{sig.entry_price:,.2f}\n"
        f"• Stop:   ₹{sig.stop_loss:,.2f}\n"
        f"• Target: ₹{sig.target:,.2f}\n"
        f"• Qty:    {sig.quantity} shares\n"
        f"• Risk:   ₹{sig.risk_amount:,.0f} ({sig.rr_ratio:.1f}:1 R:R)\n"
        f"• RSI:    {sig.rsi:.0f}" if sig.rsi else ""
        + f"\n\nOpen dashboard → Scanner → Trade Signals to execute."
    )
    return header, body


def format_telegram_confirm(sig: TradeSignal) -> tuple[str, str]:
    sym = sig.symbol.replace("-EQ", "")
    header = f"✅ Order Placed — {sym}"
    body = (
        f"Action: {sig.action} {sig.quantity} shares\n"
        f"Entry:  ₹{sig.entry_price:,.2f}\n"
        f"SL:     ₹{sig.stop_loss:,.2f}\n"
        f"Target: ₹{sig.target:,.2f}\n"
        f"Risk:   ₹{sig.risk_amount:,.0f}\n"
        f"Orders tagged: ATR-SEMI"
    )
    return header, body
