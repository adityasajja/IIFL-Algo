"""Round-trip trade reconstruction from a raw fill stream (FIFO)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from atr.core.enums import Side
from atr.core.models import Fill


@dataclass(slots=True)
class RoundTrip:
    symbol: str
    side: Side
    quantity: float
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime | None = None
    exit_price: float = 0.0
    gross_pnl: float = 0.0
    commission: float = 0.0

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.commission

    @property
    def duration(self) -> float | None:
        if self.exit_ts is None:
            return None
        return (self.exit_ts - self.entry_ts).total_seconds() / 86400.0

    @property
    def return_pct(self) -> float:
        notional = abs(self.entry_price * self.quantity)
        return (self.net_pnl / notional * 100) if notional else 0.0


@dataclass
class TradeLog:
    """Accumulates fills into closed round trips.

    A fill that increases exposure opens/extends a lot; a fill that reduces or
    flips closes lots FIFO. This mirrors how brokers compute realised P&L for
    Indian F&O (FIFO, per contract).
    """

    trades: list[RoundTrip] = field(default_factory=list)
    _open: dict[str, list[tuple[float, float, datetime, Side]]] = field(default_factory=dict)

    def add(self, fill: Fill) -> list[RoundTrip]:
        symbol = fill.instrument.symbol
        mult = fill.instrument.multiplier
        remaining = fill.quantity
        price = fill.price
        closed: list[RoundTrip] = []

        lots = self._open.setdefault(symbol, [])
        while remaining > 1e-9 and lots:
            lot_qty, lot_price, lot_ts, lot_side = lots[0]
            if lot_side is fill.side:
                break  # same direction: extends, handled below
            matched = min(lot_qty, remaining)
            pnl = (price - lot_price) * matched * mult * lot_side.sign
            trade = RoundTrip(
                symbol=symbol,
                side=lot_side,
                quantity=matched,
                entry_ts=lot_ts,
                entry_price=lot_price,
                exit_ts=fill.ts,
                exit_price=price,
                gross_pnl=pnl,
                commission=fill.commission * (matched / fill.quantity) if fill.quantity else 0.0,
            )
            closed.append(trade)
            remaining -= matched
            lot_qty -= matched
            if lot_qty <= 1e-9:
                lots.pop(0)
            else:
                lots[0] = (lot_qty, lot_price, lot_ts, lot_side)

        if remaining > 1e-9:
            lots.append((remaining, price, fill.ts, fill.side))

        self.trades.extend(closed)
        return closed

    def open_lots(self, symbol: str) -> list[tuple[float, float, datetime, Side]]:
        return self._open.get(symbol, [])

    def frame(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame(
                columns=[
                    "symbol", "side", "quantity", "entry_ts", "entry_price",
                    "exit_ts", "exit_price", "gross_pnl", "commission", "net_pnl",
                    "return_pct", "duration_days",
                ]
            )
        rows = []
        for t in self.trades:
            rows.append(
                {
                    "symbol": t.symbol,
                    "side": t.side.name,
                    "quantity": t.quantity,
                    "entry_ts": t.entry_ts,
                    "entry_price": t.entry_price,
                    "exit_ts": t.exit_ts,
                    "exit_price": t.exit_price,
                    "gross_pnl": t.gross_pnl,
                    "commission": t.commission,
                    "net_pnl": t.net_pnl,
                    "return_pct": t.return_pct,
                    "duration_days": t.duration,
                }
            )
        return pd.DataFrame(rows)
