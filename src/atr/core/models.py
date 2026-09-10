"""Domain models for the trading engine.

Design note: `Bar` and `Tick` are plain frozen dataclasses with ``slots``.
They are the hot path of any backtest (millions of instances), so we avoid
pydantic validation overhead there. Everything else — instruments, orders,
fills, positions — is pydantic, because correctness and serialisation matter
more than raw speed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from atr.core.enums import AssetClass, OptionType, OrderStatus, OrderType, Side, TimeInForce

# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    vwap: float | None = None
    open_interest: float | None = None

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def true_range_forecast(self) -> float:
        return self.high - self.low


@dataclass(frozen=True, slots=True)
class Tick:
    ts: datetime
    last: float
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    volume: float | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return None


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """All bars observed at a single timestamp, keyed by symbol."""

    ts: datetime
    bars: dict[str, Bar]

    def price(self, symbol: str) -> float | None:
        bar = self.bars.get(symbol)
        return bar.close if bar else None


# --------------------------------------------------------------------------
# Instruments
# --------------------------------------------------------------------------


class Instrument(BaseModel):
    """A tradable contract.

    For IBKR, ``conid`` is the permanent contract identifier and is the most
    reliable way to rebuild a contract later. ``symbol`` is the human-facing
    key used inside the engine.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    currency: str = "USD"
    exchange: str = "SMART"
    primary_exchange: str | None = None
    multiplier: float = 1.0
    tick_size: float = 0.01
    min_quantity: float = 1.0
    quantity_step: float = 1.0

    # Derivatives only
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    underlying: str | None = None

    # Broker identifiers
    conid: int | None = None
    local_symbol: str | None = None
    trading_class: str | None = None

    # Margin requirement per unit (F&O). For equities this is Reg-T margin.
    initial_margin_per_unit: float = 0.0
    maintenance_margin_per_unit: float = 0.0

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("option_type")
    @classmethod
    def _option_requires_fields(cls, v, info):
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_derivative(self) -> bool:
        return self.asset_class in (AssetClass.FUTURE, AssetClass.OPTION)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_option(self) -> bool:
        return self.asset_class is AssetClass.OPTION

    @property
    def notional_multiplier(self) -> float:
        """Cash exposure of a single unit. Options/futures use the contract
        multiplier; a 100x equity option means 1 unit == 100 shares."""
        return self.multiplier

    def key(self) -> str:
        return self.symbol

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        if self.is_option:
            return f"{self.symbol} {self.expiry} {self.strike}{self.option_type}"
        return self.symbol


# --------------------------------------------------------------------------
# Orders & fills
# --------------------------------------------------------------------------


class Order(BaseModel):
    model_config = ConfigDict(validate_assignment=False)

    order_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    instrument: Instrument
    side: Side
    quantity: float  # always positive; direction lives in `side`
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    tif: TimeInForce = TimeInForce.DAY
    status: OrderStatus = OrderStatus.PENDING
    parent_signal_id: str | None = None
    tag: str | None = None
    created_at: datetime | None = None
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    broker_order_id: str | None = None
    reject_reason: str | None = None

    #: Broker-specific extras (e.g. IIFL ``product`` / ``orderComplexity``).
    #: Keeping them here stops broker vocabulary leaking into the core model.
    broker_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("quantity")
    @classmethod
    def _positive_qty(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("order quantity must be positive")
        return v

    @property
    def signed_quantity(self) -> float:
        return self.side.sign * self.quantity

    @property
    def remaining_quantity(self) -> float:
        return self.quantity - self.filled_quantity

    @property
    def is_active(self) -> bool:
        return self.status in (
            OrderStatus.PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
        )

    def mark(self, status: OrderStatus, reason: str | None = None) -> None:
        self.status = status
        if reason:
            self.reject_reason = reason

    def apply_fill(self, quantity: float, price: float) -> None:
        if quantity <= 0:
            raise ValueError("fill quantity must be positive")
        if quantity > self.remaining_quantity + 1e-9:
            raise ValueError("fill exceeds remaining quantity")
        total = self.filled_quantity + quantity
        self.avg_fill_price = (
            (self.avg_fill_price * self.filled_quantity) + (price * quantity)
        ) / total
        self.filled_quantity = total
        if abs(self.remaining_quantity) < 1e-9:
            self.status = OrderStatus.FILLED
        else:
            self.status = OrderStatus.PARTIALLY_FILLED


class Fill(BaseModel):
    fill_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    order_id: str
    instrument: Instrument
    side: Side
    quantity: float
    price: float
    ts: datetime
    commission: float = 0.0
    slippage: float = 0.0
    liquidity: str = "TAKER"  # TAKER | MAKER
    broker_fill_id: str | None = None

    @property
    def gross_notional(self) -> float:
        return self.quantity * self.price * self.instrument.multiplier

    @property
    def signed_cash_flow(self) -> float:
        """Cash impact of the fill, including commission.

        A BUY removes cash from the account (negative), a SELL adds it.
        """
        return -self.side.sign * self.gross_notional - self.commission


class Position(BaseModel):
    instrument: Instrument
    quantity: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    last_price: float = 0.0
    opened_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_flat(self) -> bool:
        return abs(self.quantity) < 1e-9

    @property
    def direction(self) -> int:
        return 0 if self.is_flat else (1 if self.quantity > 0 else -1)

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price * self.instrument.multiplier

    @property
    def unrealized_pnl(self) -> float:
        if self.is_flat:
            return 0.0
        return (
            (self.last_price - self.avg_price)
            * self.quantity
            * self.instrument.multiplier
        )

    @property
    def exposure(self) -> float:
        return abs(self.market_value)

    def apply_fill(self, fill: Fill) -> float:
        """Fold a fill into the position, return realised P&L from this fill."""
        signed_new = fill.side.sign * fill.quantity
        realised = 0.0

        if self.is_flat:
            self.avg_price = fill.price
            self.quantity = signed_new
            if self.opened_at is None:
                self.opened_at = fill.ts
        elif self.direction == fill.side.sign:
            # Adding to the position: weighted average cost.
            total_qty = abs(self.quantity) + fill.quantity
            self.avg_price = (
                (self.avg_price * abs(self.quantity)) + (fill.price * fill.quantity)
            ) / total_qty
            self.quantity = self.direction * total_qty
        else:
            # Reducing or flipping.
            close_qty = min(abs(self.quantity), fill.quantity)
            realised = (
                (fill.price - self.avg_price)
                * close_qty
                * self.direction
                * self.instrument.multiplier
            )
            remaining = fill.quantity - close_qty
            new_qty = abs(self.quantity) - close_qty
            if remaining <= 1e-9:
                self.quantity = self.direction * new_qty
                if self.is_flat:
                    self.avg_price = 0.0
            else:
                # Position flipped through to the other side.
                self.quantity = fill.side.sign * remaining
                self.avg_price = fill.price

        self.realized_pnl += realised
        self.updated_at = fill.ts
        return realised

    def mark(self, price: float, ts: datetime | None = None) -> None:
        self.last_price = price
        if ts:
            self.updated_at = ts
