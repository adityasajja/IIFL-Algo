"""Context-Aware Signal Engine for Indian Cash Equities.

Enriches every strategy signal with a structured snapshot of the market,
sector, and stock conditions at the moment the signal fired, then scores
that context deterministically using a versioned, configurable model.

Key invariants
--------------
* Read-only: this package never blocks a signal, modifies strategy
  parameters, changes risk limits, or places orders.
* No fabricated data: when a required measurement is missing, the
  ``INSUFFICIENT_DATA`` classification is emitted rather than guessing.
* Versioned: every context object carries the ``SignalContextModelConfig``
  version that produced it, so a change to scoring weights does not silently
  reinterpret historical records.
* Look-ahead safe: ``SignalContextEngine.enrich_historical`` truncates price
  frames to the entry bar timestamp before computing any indicator.
"""

from atr.signal_context.engine import SignalContextEngine
from atr.signal_context.models import (
    DEFAULT_CONTEXT_MODEL_V1,
    MarketContextSnapshot,
    ScoreCriterion,
    SectorContextSnapshot,
    SignalContext,
    SignalContextClass,
    SignalContextModelConfig,
    StockContextSnapshot,
)

__all__ = [
    "DEFAULT_CONTEXT_MODEL_V1",
    "MarketContextSnapshot",
    "ScoreCriterion",
    "SectorContextSnapshot",
    "SignalContext",
    "SignalContextClass",
    "SignalContextEngine",
    "SignalContextModelConfig",
    "StockContextSnapshot",
]
