"""Portfolio accounting — the single source of truth for money.

Deliberately modelled so that equity == cash + market value at all times. If
that identity ever breaks, there is a bug in the fill or margin logic, and the
engine asserts on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from atr.core.models import Fill, Instrument, Position

EPS = 1e-6


@dataclass
class Portfolio:
    initial_cash: float = 1_000_000.0
    #: Fraction of notional that must be posted as margin for cash products.
    #: Derivatives use the per-unit margin on the Instrument instead.
    equity_margin_ratio: float = 1.0
    allow_short: bool = True

    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict, init=False)
    realized_pnl: float = field(default=0.0, init=False)
    commission_paid: float = field(default=0.0, init=False)
    slippage_cost: float = field(default=0.0, init=False)
    _curve: list[tuple[datetime, float, float]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.cash = self.initial_cash

    # ------------------------------------------------------------------
    def position(self, symbol: str) -> Position:
        return self.positions.get(symbol, Position(instrument=Instrument(symbol=symbol)))

    def ensure_position(self, instrument: Instrument) -> Position:
        pos = self.positions.get(instrument.symbol)
        if pos is None:
            pos = Position(instrument=instrument)
            self.positions[instrument.symbol] = pos
        return pos

    # ------------------------------------------------------------------
    def mark(self, ts: datetime, prices: dict[str, float]) -> None:
        for symbol, price in prices.items():
            pos = self.positions.get(symbol)
            if pos:
                pos.mark(price, ts)
        self._curve.append((ts, self.equity, self.cash))

    @property
    def market_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    @property
    def equity(self) -> float:
        return self.cash + self.market_value

    @property
    def gross_exposure(self) -> float:
        return sum(p.exposure for p in self.positions.values())

    @property
    def net_exposure(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    # ------------------------------------------------------------------
    def margin_required(self, position: Position) -> float:
        inst = position.instrument
        qty = abs(position.quantity)
        if qty < EPS:
            return 0.0
        if inst.is_derivative and inst.initial_margin_per_unit > 0:
            return qty * inst.initial_margin_per_unit
        return abs(position.market_value) * self.equity_margin_ratio

    @property
    def margin_used(self) -> float:
        return sum(self.margin_required(p) for p in self.positions.values())

    @property
    def buying_power(self) -> float:
        return self.equity - self.margin_used

    def margin_for_order(self, instrument: Instrument, quantity: float, price: float) -> float:
        if instrument.is_derivative and instrument.initial_margin_per_unit > 0:
            return abs(quantity) * instrument.initial_margin_per_unit
        return abs(quantity * price * instrument.multiplier) * self.equity_margin_ratio

    # ------------------------------------------------------------------
    def apply_fill(self, fill: Fill) -> float:
        """Post a fill to cash and position; return realised P&L."""
        position = self.ensure_position(fill.instrument)
        realised = position.apply_fill(fill)

        self.cash += fill.signed_cash_flow
        self.commission_paid += fill.commission
        self.slippage_cost += abs(fill.slippage) * fill.quantity
        self.realized_pnl += realised

        if position.is_flat:
            position.last_price = 0.0
        return realised

    # ------------------------------------------------------------------
    def equity_curve(self) -> pd.Series:
        if not self._curve:
            return pd.Series(dtype=float)
        index = [c[0] for c in self._curve]
        values = [c[1] for c in self._curve]
        return pd.Series(values, index=pd.DatetimeIndex(index), name="equity")

    def cash_curve(self) -> pd.Series:
        if not self._curve:
            return pd.Series(dtype=float)
        index = [c[0] for c in self._curve]
        values = [c[2] for c in self._curve]
        return pd.Series(values, index=pd.DatetimeIndex(index), name="cash")

    def check_invariant(self) -> None:
        """equity must equal initial cash + realised + unrealised - commission.

        Slippage is already baked into fill prices, so it is not subtracted
        again here. A break here means the fill or margin path is wrong.
        """
        expected = (
            self.initial_cash + self.realized_pnl + self.unrealized_pnl - self.commission_paid
        )
        actual = self.equity
        if abs(expected - actual) > 1.0:
            raise AssertionError(
                f"portfolio invariant broken: expected {expected:.2f}, got {actual:.2f}"
            )
