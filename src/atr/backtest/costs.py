"""Transaction cost models.

Underestimating costs is the single most common reason a backtest looks
profitable and live trading doesn't. Both models are explicit and pluggable.
"""

from __future__ import annotations

from dataclasses import dataclass

from atr.core.enums import Side
from atr.core.models import Instrument


@dataclass
class CommissionModel:
    """IBKR-style tiered/fixed commission approximation.

    ``per_share`` for equities, ``per_contract`` for F&O. Whichever is
    relevant for the instrument's asset class is used; the other is ignored.
    """

    per_share: float = 0.005
    per_contract: float = 0.85
    min_per_order: float = 1.0
    max_pct_of_trade: float = 0.01  # IBKR caps commissions as % of trade value
    pct_of_notional: float = 0.0  # exchange/clearing fees, in decimal

    def compute(self, quantity: float, price: float, instrument: Instrument) -> float:
        notional = abs(quantity * price * instrument.multiplier)
        if instrument.is_derivative:
            base = self.per_contract * quantity
        else:
            base = self.per_share * quantity
        pct_fee = self.pct_of_notional * notional
        commission = max(base + pct_fee, self.min_per_order)
        # Regulatory-style cap
        return min(commission, self.max_pct_of_trade * notional) if notional > 0 else commission


@dataclass
class SlippageModel:
    """Adverse price movement between signal and fill.

    Applied in the direction that hurts: buys fill higher, sells fill lower.
    Either a fixed basis-point cost or a number of ticks.
    """

    bps: float = 5.0
    ticks: float = 0.0
    random_seed: int | None = None

    def __post_init__(self) -> None:
        import random

        self._rng = random.Random(self.random_seed) if self.random_seed is not None else None

    def apply(self, price: float, side: Side, instrument: Instrument) -> float:
        factor = self.bps / 10_000.0
        if self._rng is not None:
            factor *= self._rng.uniform(0.5, 1.5)
        tick_cost = self.ticks * instrument.tick_size
        adjustment = price * factor + tick_cost
        return price + (adjustment if side is Side.BUY else -adjustment)
