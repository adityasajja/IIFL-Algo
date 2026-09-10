"""Alert rule/event models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

RuleKind = Literal[
    "price_above",      # last >= threshold
    "price_below",      # last <= threshold  (falling-stock / stop-loss alerts)
    "day_drop_pct",     # day change <= -threshold
    "day_gain_pct",     # day change >= threshold
    "rsi_below",        # oversold: RSI <= threshold
    "rsi_above",        # overbought: RSI >= threshold
    "sma_cross_up",     # SMA20 crossed above SMA50 within 5 bars
    "sma_cross_down",   # SMA20 crossed below SMA50 within 5 bars
]


class AlertRule(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    name: str = ""
    symbol: str  # e.g. RELIANCE-EQ
    exchange: str = "NSEEQ"
    kind: RuleKind
    threshold: float = 0.0  # price / pct / rsi level (unused for cross rules)
    cooldown_min: int = 60  # min silence between two firings of this rule
    armed: bool = True
    last_fired_at: datetime | None = None

    @property
    def display(self) -> str:
        label = {
            "price_above": f"{self.symbol} ≥ {self.threshold}",
            "price_below": f"{self.symbol} ≤ {self.threshold}",
            "day_drop_pct": f"{self.symbol} down {self.threshold}% today",
            "day_gain_pct": f"{self.symbol} up {self.threshold}% today",
            "rsi_below": f"{self.symbol} RSI ≤ {self.threshold}",
            "rsi_above": f"{self.symbol} RSI ≥ {self.threshold}",
            "sma_cross_up": f"{self.symbol} golden cross",
            "sma_cross_down": f"{self.symbol} death cross",
        }[self.kind]
        return self.name or label


class AlertEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    rule_id: str
    rule: str  # human-readable snapshot at fire time
    ts: datetime = Field(default_factory=datetime.now)
    message: str
    channel: str = "log"  # log | telegram | sms
    ok: bool = True
