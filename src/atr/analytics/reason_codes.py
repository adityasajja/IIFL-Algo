"""The attributable-classification vocabulary, declared once.

What these are, and what they are not
-------------------------------------

A reason code is a **descriptive classification of a closed trade**. It says
which part of the chain behaved in a way that is worth naming, so that a reader —
or the learning engine, slicing forward trades — can ask *"how do trades where
execution was poor compare with trades where it was good?"*.

It is **not** a prediction and **not** a recommendation. ``GOOD_ENTRY`` does not
mean the next entry will be good; it means *the price this one got was at or
better than the price the decision asked for*. ``OVERSIZED`` does not mean "size
down"; it means the position ended up larger than the sizing rule's own cap
permitted, which is a statement about a recorded number and its recorded cap.

The vocabulary is a closed set. Anything not in :data:`ALL_REASON_CODES` is a
bug, and the tests enumerate this tuple so the set cannot grow silently into
free-text advice.

Two families, deliberately separated
------------------------------------

*Decision codes* classify what the strategy chose — that the signal was positive,
that the context was or was not supportive, that the risk taken was larger or
smaller than intended, why the trade ended.
*Execution codes* classify what happened when the order met the market — entry
and exit slippage, how much of the move that was available was actually
captured.

The split is the whole point of the feature's "clearly distinguish strategy
decision from execution outcome" requirement: a profitable trade with poor
execution and a well-executed weak signal must not read the same, and they only
stay distinguishable if their codes come from different families and are never
merged into a single score.

The audit
---------

``ADVICE_WORDS`` exists so a test can assert no code contains one. A vocabulary
that drifts from "this is what happened" toward "this is what to do" is how an
advisory layer becomes a decision layer without anyone editing a permission.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Decision family — what the strategy chose
# ---------------------------------------------------------------------------

#: The signal that raised the trade was itself positive on its own terms.
SIGNAL_POSITIVE = "SIGNAL_POSITIVE"
#: The recorded market/sector context supported the trade's direction.
CONTEXT_POSITIVE = "CONTEXT_POSITIVE"
#: The recorded context did not support the trade's direction.
CONTEXT_NEGATIVE = "CONTEXT_NEGATIVE"

#: The fill was at or better than the price the decision asked for.
GOOD_ENTRY = "GOOD_ENTRY"
#: The fill was worse than the price the decision asked for.
POOR_ENTRY = "POOR_ENTRY"
#: The exit captured the move that was available, or better.
GOOD_EXIT = "GOOD_EXIT"

#: The trade ended before the thesis had its move — a stop or a time-out that
#: arrived while the position was still (or about to be) favourable.
EARLY_EXIT = "EARLY_EXIT"
#: The position gave back a large share of the ground it had made before closing.
LATE_EXIT = "LATE_EXIT"

#: Position size exceeded the sizing rule's own cap.
OVERSIZED = "OVERSIZED"
#: Position size came in below what the sizing rule intended.
UNDERSIZED = "UNDERSIZED"

#: The realised risk (stop distance) was materially wider than planned.
HIGH_RISK = "HIGH_RISK"
#: The realised risk was materially tighter than planned.
LOW_RISK = "LOW_RISK"

# ---------------------------------------------------------------------------
# Execution family — what happened when the order met the market
# ---------------------------------------------------------------------------

#: Slippage on the measured legs was materially adverse.
HIGH_SLIPPAGE = "HIGH_SLIPPAGE"
#: Slippage was negligible or favourable.
LOW_SLIPPAGE = "LOW_SLIPPAGE"

#: The position saw a large favourable excursion — it went somewhere.
FAVORABLE_MFE = "FAVORABLE_MFE"
#: The position saw a large adverse excursion before it closed.
HIGH_MAE = "HIGH_MAE"

# ---------------------------------------------------------------------------
# Exit-cause family — how the episode terminated
# ---------------------------------------------------------------------------

#: The episode ended at its protective stop.
STOP_DRIVEN = "STOP_DRIVEN"
#: The episode ended at its target.
TARGET_DRIVEN = "TARGET_DRIVEN"
#: The episode ended because its time was up.
TIME_EXIT = "TIME_EXIT"

#: Reason codes are withheld because the inputs needed to justify them are
#: absent. Emitted in place of a guess, never alongside one.
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

# ---------------------------------------------------------------------------
# The closed set
# ---------------------------------------------------------------------------

#: Every code this module may emit. A closed vocabulary on purpose: an open one
#: drifts, and a drifted vocabulary cannot be counted, sliced or tested.
ALL_REASON_CODES: tuple[str, ...] = (
    SIGNAL_POSITIVE,
    CONTEXT_POSITIVE,
    CONTEXT_NEGATIVE,
    GOOD_ENTRY,
    POOR_ENTRY,
    GOOD_EXIT,
    EARLY_EXIT,
    LATE_EXIT,
    OVERSIZED,
    UNDERSIZED,
    HIGH_RISK,
    LOW_RISK,
    HIGH_SLIPPAGE,
    LOW_SLIPPAGE,
    FAVORABLE_MFE,
    HIGH_MAE,
    STOP_DRIVEN,
    TARGET_DRIVEN,
    TIME_EXIT,
    INSUFFICIENT_DATA,
)

#: Which family each code belongs to. Returned with every code so a consumer can
#: keep decision quality and execution quality apart without hard-coding the
#: split in two places.
DECISION_CODES: frozenset[str] = frozenset(
    {
        SIGNAL_POSITIVE,
        CONTEXT_POSITIVE,
        CONTEXT_NEGATIVE,
        GOOD_ENTRY,
        POOR_ENTRY,
        GOOD_EXIT,
        EARLY_EXIT,
        LATE_EXIT,
        OVERSIZED,
        UNDERSIZED,
        HIGH_RISK,
        LOW_RISK,
    }
)
EXECUTION_CODES: frozenset[str] = frozenset(
    {HIGH_SLIPPAGE, LOW_SLIPPAGE, FAVORABLE_MFE, HIGH_MAE}
)
EXIT_CAUSE_CODES: frozenset[str] = frozenset(
    {STOP_DRIVEN, TARGET_DRIVEN, TIME_EXIT}
)


def family_of(code: str) -> str:
    """``decision`` | ``execution`` | ``exit_cause`` | ``unknown``.

    ``unknown`` rather than raising: a stored row may carry a code this version
    no longer emits, and a reader of history must still be able to render it.
    """
    if code in DECISION_CODES:
        return "decision"
    if code in EXECUTION_CODES:
        return "execution"
    if code in EXIT_CAUSE_CODES:
        return "exit_cause"
    return "unknown"


#: Exit reasons the platform records that map onto an exit-cause code. The
#: mapping is a lookup rather than a substring match: ``"stop_loss"`` contains
#: both "stop" and "loss", and a rule that scanned for keywords would classify
#: ``"trailing_stop_gain"`` as a stop *and* a target.
EXIT_REASON_CODES: dict[str, str] = {
    "stop_loss": STOP_DRIVEN,
    "stoploss": STOP_DRIVEN,
    "stop": STOP_DRIVEN,
    "hard_stop": STOP_DRIVEN,
    "trailing_stop": STOP_DRIVEN,
    "target": TARGET_DRIVEN,
    "take_profit": TARGET_DRIVEN,
    "profit_target": TARGET_DRIVEN,
    "time_stop": TIME_EXIT,
    "time_exit": TIME_EXIT,
    "max_hold": TIME_EXIT,
    "eod": TIME_EXIT,
    "square_off": TIME_EXIT,
    "session_close": TIME_EXIT,
}

#: Words that would turn a description into a recommendation. No code may
#: contain one; ``tests/test_attribution.py`` enforces it.
ADVICE_WORDS: tuple[str, ...] = (
    "BUY",
    "SELL",
    "RECOMMEND",
    "SHOULD",
    "CONSIDER",
    "INCREASE",
    "REDUCE",
    "PROPOSE",
    "OPTIMIZE",
    "IMPROVE",
)


__all__ = [
    "ADVICE_WORDS",
    "ALL_REASON_CODES",
    "CONTEXT_NEGATIVE",
    "CONTEXT_POSITIVE",
    "DECISION_CODES",
    "EXIT_CAUSE_CODES",
    "EXIT_REASON_CODES",
    "EXECUTION_CODES",
    "INSUFFICIENT_DATA",
    "family_of",
]
