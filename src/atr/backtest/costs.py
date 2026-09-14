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

    For NSE cash equity this model is **badly wrong** and should not be used:
    it charges Rs 0.005/share with no ad-valorem component, while the real bill
    is dominated by Securities Transaction Tax at 0.1% of turnover on *both*
    sides. See :class:`IndianDeliveryCosts`.
    """

    per_share: float = 0.005
    per_contract: float = 0.85
    min_per_order: float = 1.0
    max_pct_of_trade: float = 0.01  # IBKR caps commissions as % of trade value
    pct_of_notional: float = 0.0  # exchange/clearing fees, in decimal

    def compute(
        self,
        quantity: float,
        price: float,
        instrument: Instrument,
        side: Side | None = None,
    ) -> float:
        """``side`` is ignored here; models with asymmetric fees need it."""
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
class IndianDeliveryCosts(CommissionModel):
    """Realistic NSE cash-equity **delivery** costs, 2026 rates.

    Why this exists: every strategy measured in this repo before it was costed
    with the IBKR-style default, which charges Rs 0.005/share and no statutory
    levy at all. A delivery round trip in India actually costs roughly **0.25% of
    turnover**, and almost all of that is ad valorem:

    ====================  =========  ===================================
    Charge                Rate       Notes
    ====================  =========  ===================================
    STT                   0.1%       **each side** — the dominant cost
    Stamp duty            0.015%     buy only
    NSE transaction       0.00297%   each side
    SEBI turnover fee     Rs 10/cr   each side
    GST                   18%        on brokerage + exchange + SEBI
    Brokerage             Rs 20/ord  or 0.03%, whichever is lower
    DP charges            Rs 13.5    per sell, per scrip
    ====================  =========  ===================================

    The consequence for strategy selection is not a rounding error. A rule that
    trades 1,900 times pays ~19% of the account in frictions over a 1,000-session
    out-of-sample window; a rule that rebalances 20 times pays ~0.2%. That
    difference is larger than most of the edges anyone was chasing, which is the
    single most useful thing a cost model can tell you.

    Not modelled: short-term vs long-term capital gains. Converting a long-term
    holding into many short-term ones costs a further 7.5 points of tax on gains
    (20% STCG vs 12.5% LTCG), so the real bar is higher still.
    """

    brokerage_per_order: float = 20.0
    brokerage_pct: float = 0.0003
    #: Securities Transaction Tax. 0.1% on both legs for delivery.
    stt_pct: float = 0.001
    #: Stamp duty, charged on the buy leg only.
    stamp_pct_buy: float = 0.00015
    exchange_pct: float = 0.0000297
    sebi_pct: float = 0.000001
    gst_pct: float = 0.18
    dp_per_sell: float = 13.5

    def compute(
        self,
        quantity: float,
        price: float,
        instrument: Instrument,
        side: Side | None = None,
    ) -> float:
        notional = abs(quantity * price * instrument.multiplier)
        if notional <= 0:
            return 0.0

        brokerage = min(self.brokerage_per_order, self.brokerage_pct * notional)
        exchange = self.exchange_pct * notional
        sebi = self.sebi_pct * notional
        gst = self.gst_pct * (brokerage + exchange + sebi)

        total = brokerage + exchange + sebi + gst
        total += self.stt_pct * notional
        if side is Side.SELL:
            total += self.dp_per_sell
        else:
            # Unknown side is treated as a buy: stamp duty is the smaller
            # asymmetry, and over-charging slightly is the safe direction.
            total += self.stamp_pct_buy * notional
        return total


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
