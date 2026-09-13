"""Built-in strategies."""

from atr.signals.cross_sectional import CrossSectionalMomentum
from atr.signals.strategy import SignalEntryStrategy
from atr.strategy.strategies.orb import OpeningRangeBreakout
from atr.strategy.strategies.paper_alpha import (
    AvellanedaLeeMeanReversion,
    IimaNseMomentum,
    JegadeeshTitmanMomentum,
    MultiFactorComposite,
    Nism52wHighProximity,
    SehgalLowVolAnomaly,
    VolatilityBreakout,
)
from atr.strategy.strategies.sma_crossover import SmaCrossover

STRATEGIES = {
    SmaCrossover.name: SmaCrossover,
    OpeningRangeBreakout.name: OpeningRangeBreakout,
    SignalEntryStrategy.name: SignalEntryStrategy,
    CrossSectionalMomentum.name: CrossSectionalMomentum,
    # Academic paper models. Registered so the walk-forward harness can score
    # them; their live-signal confidence numbers are assumptions until a
    # validation run says otherwise.
    JegadeeshTitmanMomentum.name: JegadeeshTitmanMomentum,
    AvellanedaLeeMeanReversion.name: AvellanedaLeeMeanReversion,
    VolatilityBreakout.name: VolatilityBreakout,
    MultiFactorComposite.name: MultiFactorComposite,
    IimaNseMomentum.name: IimaNseMomentum,
    Nism52wHighProximity.name: Nism52wHighProximity,
    SehgalLowVolAnomaly.name: SehgalLowVolAnomaly,
}

__all__ = [
    "SmaCrossover",
    "OpeningRangeBreakout",
    "SignalEntryStrategy",
    "CrossSectionalMomentum",
    "JegadeeshTitmanMomentum",
    "AvellanedaLeeMeanReversion",
    "VolatilityBreakout",
    "MultiFactorComposite",
    "IimaNseMomentum",
    "Nism52wHighProximity",
    "SehgalLowVolAnomaly",
    "STRATEGIES",
]
