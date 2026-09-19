"""What history says about each setup, measured on the local daily cache.

These numbers come from ``scripts/study_buy_and_market.py`` and ``scripts/study_quiet.py``
(2015-2026, every stock with enough history, an early and a recent half tested separately).
They are written here as data so an alert can quote its own evidence, and so the wording
never drifts from what was measured.

Limits that always apply, and are shown to the user: the stocks are only ones that still
trade (survivorship), returns are before costs, and an average edge says nothing about any
single stock. Nothing in this module says what *will* happen.
"""

from __future__ import annotations

# Extra average return over the next ~20 sessions, against all stocks in the same period,
# as (early half, recent half). Only setups that were positive in both halves are used.
BUY_SETUPS: dict[str, dict[str, str]] = {
    "strong_rs": {
        "label": "Beating the market",
        "evidence": "Stocks up 25%+ more than the market over 20 days did about 0.9% to 1.6% better than average over the next month.",
        "risk": "",
    },
    "oversold": {
        "label": "Oversold bounce",
        "evidence": "Stocks down 20%+ in 20 days bounced about 0.9% to 3.9% better than average over the next month.",
        "risk": "Riskier: many keep falling first, and the average hides that.",
    },
    "resting_leader": {
        "label": "Leader taking a breather",
        "evidence": "A strong stock pausing 3-12% below its high did about 0.2% to 1.4% better than average. This is the weakest of the three.",
        "risk": "",
    },
}

# What the share of stocks above their 50-day average has been followed by (next 20 sessions,
# equal-weight market, early/recent halves). Weak breadth alone has not been a sell signal.
BREADTH_CONTEXT: list[tuple[float, float, str]] = [
    (0, 25, "Very weak breadth. In the past this was followed by above-average months (about +2% to +4%), so it has not been a reason to sell."),
    (25, 35, "Weak breadth. Historically the next month was roughly average."),
    (35, 65, "Middle zone. Historically the least rewarding zone for the next month."),
    (65, 101, "Strong participation. The next month has usually been above average."),
]

QUIET_NOTE = (
    "After going quiet, a stock's next-week move has been about 10-16% smaller than usual. "
    "That is a mild tendency, not a reason to sell: it says nothing about direction."
)

PROTECT_NOTE = "This is risk control, not a forecast: a stop limits how much of a gain can be given back."

LAGGING_NOTE = "Weak stocks have often bounced, so this is worth a look, not a reason to sell."

DISCLAIMER = (
    "Based on 2015-2026 history of stocks that still trade, before costs. "
    "An average says nothing about any single stock."
)


def breadth_context(breadth_pct: float) -> str:
    for lo, hi, text in BREADTH_CONTEXT:
        if lo <= breadth_pct < hi:
            return text
    return BREADTH_CONTEXT[-1][2]
