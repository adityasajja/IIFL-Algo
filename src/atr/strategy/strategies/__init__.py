"""Built-in strategies."""

from atr.strategy.strategies.orb import OpeningRangeBreakout
from atr.strategy.strategies.sma_crossover import SmaCrossover

STRATEGIES = {
    SmaCrossover.name: SmaCrossover,
    OpeningRangeBreakout.name: OpeningRangeBreakout,
}

__all__ = ["SmaCrossover", "OpeningRangeBreakout", "STRATEGIES"]
