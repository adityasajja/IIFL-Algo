"""Reading structure out of a prose reason, without inventing any.

The problem
-----------

``signal_reason`` is the only record of *why* a trade happened, and it is prose:

    "within 2.0% of the 63-bar high 1,431.82 on 2.4x average volume"
    "uptrend (SMA20 1,431.82 > SMA50 1,420.55) with RSI 47"
    "SMA 10 (1,431.82) crossed above SMA 30 (1,420.55) with close at 1,433.00"

Prose is the right storage format — ``Strategy.describe_signal`` is documented as
opt-in precisely so that a strategy which cannot explain itself is still legal.
But "does relative volume above 2.0x help?" cannot be answered from prose.

The rule this module obeys
--------------------------

**Extract, never infer. A field that cannot be read is absent, not guessed.**

If a reason does not mention a volume multiple, ``volume_multiple`` is ``None``.
It is not 1.0, it is not the mean, and it is not carried over from a sibling
trade. That is the difference between a dataset that can be used for statistics
and one that quietly manufactures them — the same mistake that made the older
``profit_factor`` in ``research/self_learning.py`` meaningless.

The extraction is therefore conservative and pattern-specific, because the
alternative — a clever general parser — would eventually match a number in a
neighbouring phrase and attribute it to the wrong feature. A number in the wrong
column is worse than a missing number, because a missing number is visible.

Setup normalisation
-------------------

The three entry setups in ``atr.signals.rules`` share one ``signal_reason``
column but produce distinguishable prose. ``normalise_setup`` maps that prose to
one of the three names, so a breakdown by setup type is possible without
changing the strategy contract or the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: The setup names, matching ``atr.signals.rules.eval_entry``'s rule names.
SETUP_TREND_PULLBACK = "trend_pullback"
SETUP_BREAKOUT = "breakout"
SETUP_OVERSOLD = "oversold_uptrend"
SETUP_SMA_CROSS = "sma_crossover"
SETUP_UNKNOWN = "unknown"

#: Sentinel for "this trade's reason did not name its setup".
UNKNOWN = SETUP_UNKNOWN

#: Number pattern that tolerates thousands separators and Indian digit
#: grouping, so "1,431.82" and "1,43,182.00" both read as numbers.
_NUMBER = r"(\d[\d,]*(?:\.\d+)?)"


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ExtractedReason:
    """Whatever could be read from one ``signal_reason`` string.

    Every field is Optional and defaults to None. The ``matched`` set records
    which patterns fired, so a caller can distinguish "the reason did not say"
    from "the parser failed on text that should have matched" — a distinction
    that matters when deciding whether to extend the parser or accept a gap.
    """

    setup: str = UNKNOWN
    rsi: float | None = None
    sma_fast: float | None = None
    sma_slow: float | None = None
    sma_long: float | None = None
    prior_high: float | None = None
    volume_multiple: float | None = None
    proximity_pct: float | None = None
    matched: frozenset[str] = field(default_factory=frozenset)

    @property
    def parsed_anything(self) -> bool:
        return bool(self.matched)

    def as_dict(self) -> dict[str, Any]:
        return {
            "setup": self.setup,
            "rsi": self.rsi,
            "sma_fast": self.sma_fast,
            "sma_slow": self.sma_slow,
            "sma_long": self.sma_long,
            "prior_high": self.prior_high,
            "volume_multiple": self.volume_multiple,
            "proximity_pct": self.proximity_pct,
            "matched": sorted(self.matched),
        }


# ""uptrend (SMA20 1,431.82 > SMA50 1,420.55) with RSI 47""
_TREND_PULLBACK = re.compile(
    rf"uptrend\s*\(\s*SMA(\d+)\s*{_NUMBER}\s*>\s*SMA(\d+)\s*{_NUMBER}\s*\)\s*with\s*RSI\s*{_NUMBER}",
    re.IGNORECASE,
)
# ""within 2.0% of the 63-bar high 1,431.82 on 2.4x average volume""
_BREAKOUT = re.compile(
    rf"within\s*{_NUMBER}\s*%\s*of\s*the\s*(\d+)-bar\s*high\s*{_NUMBER}\s*on\s*{_NUMBER}\s*x\s*average\s*volume",
    re.IGNORECASE,
)
# ""RSI 28 but still above SMA100 1,420.55""
_OVERSOLD = re.compile(
    rf"RSI\s*{_NUMBER}\s*but\s*still\s*above\s*SMA(\d+)\s*{_NUMBER}",
    re.IGNORECASE,
)
# ""SMA 10 (1,431.82) crossed above SMA 30 (1,420.55) with close at 1,433.00""
_SMA_CROSS = re.compile(
    rf"SMA\s*(\d+)\s*\(\s*{_NUMBER}\s*\)\s*crossed\s*(above|below)\s*SMA\s*(\d+)\s*\(\s*{_NUMBER}\s*\)",
    re.IGNORECASE,
)
# Fallback RSI mention, used only when no setup pattern matched but an RSI was
# named. A bare "RSI 44" in an unrecognised sentence is still a real RSI value.
_RSI_ANY = re.compile(rf"RSI\s*{_NUMBER}", re.IGNORECASE)


def extract_reason(reason: str | None) -> ExtractedReason:
    """Read the structured fields a ``signal_reason`` actually contains.

    Returns an all-None :class:`ExtractedReason` for empty input rather than
    raising: a trade with no reason is a normal, legal state — the contract
    explicitly permits a strategy that cannot explain itself — and the dataset
    must be able to hold it and mark the features missing.
    """
    text = (reason or "").strip()
    if not text:
        return ExtractedReason()

    matched: set[str] = set()

    # Group map for every pattern below: the window sizes are group 1 and the
    # prices follow, because the compiled regex interleaves ``(\d+)`` with the
    # shared ``_NUMBER`` capture. Verified against real reasons in
    # ``tests/test_learning_attribution.py`` rather than by eye.
    found = _TREND_PULLBACK.search(text)
    if found:
        matched.update({"setup", "rsi", "sma_fast", "sma_slow"})
        return ExtractedReason(
            setup=SETUP_TREND_PULLBACK,
            sma_fast=_to_float(found.group(2)),  # SMA<fast window> <value>
            sma_slow=_to_float(found.group(4)),  # SMA<slow window> <value>
            rsi=_to_float(found.group(5)),
            matched=frozenset(matched),
        )

    found = _BREAKOUT.search(text)
    if found:
        matched.update({"setup", "proximity_pct", "prior_high", "volume_multiple"})
        return ExtractedReason(
            setup=SETUP_BREAKOUT,
            proximity_pct=_to_float(found.group(1)),  # within <p>% of the
            prior_high=_to_float(found.group(3)),  # <n>-bar high <value>
            volume_multiple=_to_float(found.group(4)),  # on <v>x average volume
            matched=frozenset(matched),
        )

    found = _OVERSOLD.search(text)
    if found:
        matched.update({"setup", "rsi", "sma_long"})
        return ExtractedReason(
            setup=SETUP_OVERSOLD,
            rsi=_to_float(found.group(1)),  # RSI <value>
            sma_long=_to_float(found.group(3)),  # SMA<window> <value>
            matched=frozenset(matched),
        )

    found = _SMA_CROSS.search(text)
    if found:
        # Groups: 1=fast window, 2=fast value, 3=direction, 4=slow window,
        # 5=slow value. Reading group 4 as sma_slow was a real bug caught by
        # the test below — it returned the *window* (30) as the price.
        matched.update({"setup", "sma_fast", "sma_slow", "direction"})
        return ExtractedReason(
            setup=SETUP_SMA_CROSS,
            sma_fast=_to_float(found.group(2)),
            sma_slow=_to_float(found.group(5)),
            matched=frozenset(matched),
        )

    # Nothing recognisable. Salvage the one field whose pattern is unambiguous on
    # its own, and leave everything else absent.
    found = _RSI_ANY.search(text)
    if found:
        matched.add("rsi")
        return ExtractedReason(rsi=_to_float(found.group(1)), matched=frozenset(matched))

    return ExtractedReason()


def normalise_setup(reason: str | None) -> str:
    """The setup name for a reason, or ``"unknown"``.

    Kept separate from :func:`extract_reason` because a caller building an axis
    does not need the numbers, and calling this avoids constructing a dataclass
    per trade for a groupby key.
    """
    return extract_reason(reason).setup


# ---------------------------------------------------------------------------
# nify the trend of one symbol's own price path
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceContext:
    """Point-in-time features for one symbol at one timestamp.

    Every field describes the bar *at or before* the timestamp and nothing after
    it. The ``as_of_ts`` records which bar was used, so a reader can verify the
    join rather than trusting it.
    """

    as_of_ts: Any = None
    close: float | None = None
    rsi: float | None = None
    atr_pct: float | None = None
    volume_multiple: float | None = None
    gap_pct: float | None = None
    sma_fast: float | None = None
    sma_slow: float | None = None
    #: Close above the slow average — the cheapest honest trend filter.
    above_slow_sma: bool | None = None
    #: Bars available up to and including ``as_of_ts``.
    bars_available: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of_ts": self.as_of_ts,
            "close": self.close,
            "rsi": self.rsi,
            "atr_pct": self.atr_pct,
            "volume_multiple": self.volume_multiple,
            "gap_pct": self.gap_pct,
            "sma_fast": self.sma_fast,
            "sma_slow": self.sma_slow,
            "above_slow_sma": self.above_slow_sma,
            "bars_available": self.bars_available,
        }


__all__ = [
    "ExtractedReason",
    "PriceContext",
    "SETUP_BREAKOUT",
    "SETUP_OVERSOLD",
    "SETUP_SMA_CROSS",
    "SETUP_TREND_PULLBACK",
    "SETUP_UNKNOWN",
    "UNKNOWN",
    "extract_reason",
    "normalise_setup",
]
