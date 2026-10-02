"""Is this backtest believable? Plain checks on the inputs, reported as warnings.

None of these stop a run — they tell the reader which results to distrust and why:

* a one-day jump this large in a daily close is almost always a split or bonus the price
  history was not adjusted for, which shows up as a fake crash (or a fake rally) the
  strategy then "trades";
* a hole of weeks in the middle of a series means missing data, not a quiet market;
* a universe built from today's names cannot contain the companies that were delisted or
  collapsed, so every result is flattered by survivorship — unfixable without delisted
  history, so it is disclosed, not hidden;
* shorting cash equity for more than a day is not possible in India, so a short-capable run
  costed as delivery describes something nobody can do.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

#: A close-to-close move beyond this is treated as a probable unadjusted split/bonus.
MAX_DAILY_JUMP = 0.35
#: Calendar days. Exchange holidays and long weekends never reach this.
MAX_GAP_DAYS = 12


def _examples(items: list[str], n: int = 4) -> str:
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


def jump_findings(frames: dict[str, pd.DataFrame]) -> list[str]:
    out: list[str] = []
    for symbol, frame in frames.items():
        if frame is None or len(frame) < 2 or "close" not in frame:
            continue
        move = frame["close"].astype(float).pct_change().abs()
        bad = move[move > MAX_DAILY_JUMP]
        if len(bad):
            when = bad.idxmax()
            out.append(f"{symbol} {pd.Timestamp(when).date()} ({bad.max():.0%})")
    return out


def gap_findings(frames: dict[str, pd.DataFrame]) -> list[str]:
    out: list[str] = []
    for symbol, frame in frames.items():
        if frame is None or len(frame) < 2:
            continue
        index = pd.DatetimeIndex(frame.index)
        gaps = pd.Series(index[1:] - index[:-1], index=index[1:])
        big = gaps[gaps > pd.Timedelta(days=MAX_GAP_DAYS)]
        if len(big):
            out.append(f"{symbol} {big.index[0].date()} ({big.iloc[0].days} days)")
    return out


def validity_warnings(frames: dict[str, pd.DataFrame], *, allow_short: bool, cost_model: str) -> list[str]:
    notes: list[str] = []
    jumps = jump_findings(frames)
    if jumps:
        notes.append(
            f"{len(jumps)} symbol(s) have a one-day close move over {MAX_DAILY_JUMP:.0%} — likely an "
            f"unadjusted split or bonus, which fakes a crash or rally: {_examples(jumps)}. "
            "Check the price history before trusting trades in these names."
        )
    gaps = gap_findings(frames)
    if gaps:
        notes.append(
            f"{len(gaps)} symbol(s) have a gap of over {MAX_GAP_DAYS} days inside their history "
            f"(missing data, not a quiet market): {_examples(gaps)}."
        )
    notes.append(
        "Survivorship: the universe is built from names that exist today. Companies delisted or "
        "collapsed during this period are not in it, so results are flattered, most for long-only "
        "strategies over long windows."
    )
    if allow_short and cost_model == "india_delivery":
        notes.append(
            "Shorting is on but costs are delivery rates. In India a cash-equity short must be "
            "squared off the same day (intraday rates, different STT), so overnight shorts here "
            "cannot be done in practice."
        )
    return notes


__all__ = ["validity_warnings", "jump_findings", "gap_findings", "MAX_DAILY_JUMP", "MAX_GAP_DAYS"]
