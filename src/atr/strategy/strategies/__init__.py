"""Built-in strategies."""

from atr.signals.strategy import SignalEntryStrategy
from atr.strategy.strategies.orb import OpeningRangeBreakout
from atr.strategy.strategies.sma_crossover import SmaCrossover

STRATEGIES = {
    SmaCrossover.name: SmaCrossover,
    OpeningRangeBreakout.name: OpeningRangeBreakout,
    SignalEntryStrategy.name: SignalEntryStrategy,
}

__all__ = [
    "SmaCrossover",
    "OpeningRangeBreakout",
    "SignalEntryStrategy",
    "STRATEGIES",
]
