"""Post-trade analytics — attribution, excursions and execution quality.

Why this package exists
-----------------------

Everything upstream answers *"what should we do"*. This answers *"what did we
actually do, and which part of it worked"* — and it answers it from the record
the platform already wrote, not from a re-run.

The shape, and the two rules that hold it together
--------------------------------------------------

``models.py``  the field vocabulary. Frozen dataclasses; the serialised shape is
               what backtest, paper and live all write, so one schema covers all
               three venues.
``excursions.py``  MAE/MFE and the execution-quality measures. Pure, and
               look-ahead-free *by construction*: every input is truncated at the
               observation point before any arithmetic runs, so a future bar
               cannot reach the calculation even by accident.
``attribution.py``  the TRADE tree and its deterministic reason codes. Pure —
               it takes a fully-assembled input and returns a classification.
``reason_codes.py``  the code vocabulary, declared once.

Two rules, each of which exists because breaking it produces a number that looks
exactly like a correct one:

1. **A measure may only use information that existed when it claims to.** MAE and
   MFE are *path* measures and — unlike a forecast — legitimately read the price
   series after entry, because that is what "the worst the trade saw" means. What
   must not happen is an *entry-quality* measure reading the future: a
   "favourable entry" judged by where price went afterwards is look-ahead wearing
   the name of execution analysis. So the two families are separated here, in
   different modules, with the truncation rule stated at each.

2. **Absent is not zero.** Every field is ``float | None`` and a missing input
   yields ``None``, never ``0.0``. The distinction matters most for cost and
   slippage: a leg whose reference price was never recorded had *no measurable*
   slippage, which is a different statement from *zero* slippage, and one that a
   reader can act on.

Deliberately absent
-------------------

No prediction, no proposed parameter, no strategy ranking. The reason codes are
descriptive classifications of a trade that has already closed; they carry no
claim about the next one. ``tests/test_attribution.py`` asserts the vocabulary
contains no advice-shaped word, for the same reason the learning tier does.
"""

from atr.analytics.attribution import TradeAttribution, attribute_trade, build_input
from atr.analytics.excursions import (
    ExecutionQuality,
    ExcursionSet,
    compute_excursions,
    execution_quality,
)
from atr.analytics.models import (
    AttributionInput,
    AttributionLeg,
    TradeDetails,
)
from atr.analytics.reason_codes import (
    ALL_REASON_CODES,
    EARLY_EXIT,
    GOOD_ENTRY,
    GOOD_EXIT,
    HIGH_MAE,
    HIGH_SLIPPAGE,
    LATE_EXIT,
    OVERSIZED,
    STOP_DRIVEN,
    TARGET_DRIVEN,
    TIME_EXIT,
    UNDERSIZED,
)

__all__ = [
    "ALL_REASON_CODES",
    "EARLY_EXIT",
    "GOOD_ENTRY",
    "GOOD_EXIT",
    "HIGH_MAE",
    "HIGH_SLIPPAGE",
    "LATE_EXIT",
    "OVERSIZED",
    "STOP_DRIVEN",
    "TARGET_DRIVEN",
    "TIME_EXIT",
    "UNDERSIZED",
    "AttributionInput",
    "AttributionLeg",
    "ExecutionQuality",
    "ExcursionSet",
    "TradeAttribution",
    "TradeDetails",
    "attribute_trade",
    "build_input",
    "compute_excursions",
    "execution_quality",
]
