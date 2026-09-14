"""Pre-trade and intraday risk controls.

These run identically in backtest and live. The point is that a strategy bug
costs you a halted simulation, not a blown account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time

from atr.core.enums import Side
from atr.core.models import Order


@dataclass
class RiskLimits:
    max_gross_exposure: float | None = None
    max_position_notional: float = float("inf")
    max_position_per_symbol: float | None = None  # in units
    max_daily_loss: float | None = None
    max_daily_trades: int | None = None
    max_open_positions: int | None = None
    max_order_notional: float | None = None
    allow_short: bool = True
    allowed_symbols: set[str] | None = None
    trading_window: tuple[time, time] | None = None
    kill_switch: bool = False


@dataclass
class RiskVerdict:
    allowed: bool
    reason: str | None = None

    @classmethod
    def ok(cls) -> RiskVerdict:
        return cls(True)

    @classmethod
    def reject(cls, reason: str) -> RiskVerdict:
        return cls(False, reason)


@dataclass
class RiskEngine:
    limits: RiskLimits = field(default_factory=RiskLimits)

    day_start_equity: float | None = field(default=None, init=False)
    orders_today: int = field(default=0, init=False)
    halted: bool = field(default=False, init=False)
    halt_reason: str | None = field(default=None, init=False)

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self.day_start_equity = None
        self.orders_today = 0
        self.halted = False
        self.halt_reason = None

    def reset_daily(self, equity_at_rollover: float | None = None) -> None:
        """Start a new session. Pass the equity at the rollover for a usable baseline.

        ``max_daily_loss`` is measured from the start of the day, so the caller
        has to say what the day started at. Without an argument the baseline
        re-seeds on the next :meth:`check` — which the engine runs *after*
        marking the bar, so on daily bars the day's loss is already in the
        equity and the limit compares a number to itself: it can never fire.
        Passing the previous close (which is what the engine has at the rollover
        point) makes a full session's loss visible.

        Leaving ``day_start_equity`` pinned across days is the other failure
        mode: the limit silently becomes "cumulative drawdown since inception",
        trips once, and reports a single-day loss that never happened.
        """
        self.orders_today = 0
        self.day_start_equity = equity_at_rollover

    def trip(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    # ------------------------------------------------------------------
    def check(self, portfolio, now: datetime) -> RiskVerdict:
        """Portfolio-level check, run once per bar."""
        if self.limits.kill_switch:
            return RiskVerdict.reject("kill switch engaged")
        if self.halted:
            return RiskVerdict.reject(self.halt_reason or "halted")

        if self.day_start_equity is None:
            self.day_start_equity = portfolio.equity

        if self.limits.max_daily_loss is not None:
            loss = self.day_start_equity - portfolio.equity
            if loss >= self.limits.max_daily_loss:
                self.trip(f"daily loss {loss:,.2f} >= limit {self.limits.max_daily_loss:,.2f}")
                return RiskVerdict.reject(self.halt_reason)

        limit = self.limits.max_gross_exposure
        if limit is not None and portfolio.gross_exposure > limit:
            return RiskVerdict.reject(
                f"gross exposure {portfolio.gross_exposure:,.0f} > {limit:,.0f}"
            )

        if self.limits.max_open_positions is not None:
            open_count = sum(1 for p in portfolio.positions.values() if not p.is_flat)
            if open_count > self.limits.max_open_positions:
                return RiskVerdict.reject(f"too many open positions ({open_count})")

        if self.limits.trading_window is not None:
            start, end = self.limits.trading_window
            if not (start <= now.time() <= end):
                return RiskVerdict.reject(f"outside trading window {start}-{end}")

        return RiskVerdict.ok()

    # ------------------------------------------------------------------
    def check_order(self, order: Order, portfolio) -> RiskVerdict:
        """Per-order check, run before submission."""
        if self.limits.kill_switch:
            return RiskVerdict.reject("kill switch engaged")
        if self.halted:
            return RiskVerdict.reject(self.halt_reason or "halted")

        symbol = order.instrument.symbol
        if self.limits.allowed_symbols is not None and symbol not in self.limits.allowed_symbols:
            return RiskVerdict.reject(f"{symbol} not in allowed universe")

        if order.side is Side.SELL and not self.limits.allow_short:
            held = portfolio.position(symbol).quantity
            if order.quantity > held + 1e-9:
                return RiskVerdict.reject(f"shorting disabled, holding {held}")

        price = order.limit_price or portfolio.position(symbol).last_price or 0.0
        notional = order.quantity * price * order.instrument.multiplier

        if self.limits.max_order_notional and notional > self.limits.max_order_notional:
            return RiskVerdict.reject(f"order notional {notional:,.0f} exceeds limit")

        if self.limits.max_position_per_symbol:
            projected = abs(portfolio.position(symbol).quantity + order.signed_quantity)
            if projected > self.limits.max_position_per_symbol:
                return RiskVerdict.reject(
                    f"projected position {projected} > {self.limits.max_position_per_symbol}"
                )

        if self.limits.max_position_notional and notional > self.limits.max_position_notional:
            return RiskVerdict.reject(f"position notional {notional:,.0f} exceeds limit")

        if self.limits.max_daily_trades and self.orders_today >= self.limits.max_daily_trades:
            return RiskVerdict.reject(f"daily trade limit reached ({self.orders_today})")

        self.orders_today += 1
        return RiskVerdict.ok()
