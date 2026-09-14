"""Built-in strategies."""

from atr.signals.cross_sectional import CrossSectionalMomentum
from atr.signals.strategy import SignalEntryStrategy
from atr.strategy.strategies.alpha_candidates import (
    ShortHorizonReversal,
    TrendFilteredExposure,
    VolatilityManaged,
)
from atr.strategy.strategies.episodic_pivot import (
    EpisodicPivot9M,
    EpisodicPivotDay1,
    EpisodicPivotDelayed,
)
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
    # Pradeep Bonde's Episodic Pivot family (see episodic_pivot.py for what
    # survives translation from a news-driven discretionary model to daily bars).
    EpisodicPivotDay1.name: EpisodicPivotDay1,
    EpisodicPivotDelayed.name: EpisodicPivotDelayed,
    EpisodicPivot9M.name: EpisodicPivot9M,
    # Pre-registered candidates for the alpha hunt — see alpha_candidates.py for
    # the prior behind each and the condition that falsifies it.
    ShortHorizonReversal.name: ShortHorizonReversal,
    VolatilityManaged.name: VolatilityManaged,
    TrendFilteredExposure.name: TrendFilteredExposure,
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
    "EpisodicPivotDay1",
    "EpisodicPivotDelayed",
    "EpisodicPivot9M",
    "ShortHorizonReversal",
    "VolatilityManaged",
    "TrendFilteredExposure",
    "STRATEGIES",
]
