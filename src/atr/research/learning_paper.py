"""The forward paper-trading ledger, read as a learning-dataset source.

Why this module exists
----------------------

Phase 1 of the learning specification names **paper-trading results** as a
source, and the drift analysis is *entirely* a comparison between what a rule did
in testing and what it did afterwards. Without a paper source there is no
"afterwards" to compare against, and the drift verdict can only ever report that
it has nothing to compare.

The database cannot supply it. ``trade_journal`` is filled from the paper
*deployments* in ``services/runner.py``, and those have produced no fills; the
only record of a rule actually running forward in this project is the ledger
written by ``scripts/track_momentum_paper.py``:

* ``data/paper_momentum/picks.jsonl`` — one record per week, written **before**
  the outcome was known, carrying each pick's entry price and the backtest's
  expectation.
* ``data/paper_momentum/settlements.jsonl`` — one record per settled week, with
  each pick's realised return.

The rule this module exists to enforce
--------------------------------------

**An in-sample week is never counted as forward evidence.**

The ledger contains two kinds of week, and they are not the same thing:

* a **forward** week, written by ``record()`` at the time, with the expectation
  logged before the outcome existed. This is evidence.
* a **backfilled** week, written by ``backfill()`` to exercise the settlement
  path, and labelled by its own producer as *"backfill (in-sample — harness
  validation only)"*. The rule was selected on that history. This is **not**
  evidence about whether the rule still works, and reading it as such is the
  exact self-deception the rest of this project is built to prevent.

Every row therefore carries an ``evidence_class``, and the ledger reports the
forward and in-sample counts separately. A week whose provenance is ambiguous is
graded in-sample, because the failure mode of over-claiming independence is
silent and the failure mode of under-claiming it is merely conservative.

What the ledger cannot supply, and does not pretend to
------------------------------------------------------

* **No rupee P&L.** The ledger records an equal-weight return. There is no
  position size anywhere in it, so ``quantity``, ``gross_pnl``, ``net_pnl`` and
  ``commission`` are all ``None`` — and marked missing. Multiplying a return by
  an assumed capital would manufacture a number that looks exactly like a
  measurement.
* **The return is gross.** Entry and exit are close prices; no commission and no
  slippage is applied. A net backtest must not be compared against it without
  saying so, and the reason is recorded per row.
* **No MFE/MAE, no slippage, no exit reason from the market.** The exit is a
  fixed seven-calendar-day hold, which is a property of the harness rather than
  of the market, so it is recorded as ``fixed_hold_7d`` and not as a strategy
  decision.
* **No entry setup** in the vocabulary ``atr.signals.rules`` uses. The entry
  condition is a cross-sectional momentum rank, which is none of
  ``trend_pullback``, ``breakout``, ``oversold_uptrend`` or ``sma_crossover``.
  It is left ``None`` rather than filed under ``unknown``, because an
  ``unknown`` bucket gets compared to the rest of the sample as though "we could
  not name it" were a market condition.

Timestamps
----------

The ledger stores dates, not instants: a week's close. Every row is stamped at
15:30 on that date — the NSE close — so ``time_of_day`` reports ``close``
rather than the ``open`` a midnight stamp would produce. That is a convention
for placing a weekly close on the clock, not a measurement of an execution time,
and it is the same convention for entry and exit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from atr.research.learning_enrich import time_of_day
from atr.research.learning_evidence import (  # noqa: F401 — re-exported
    CLASS_IN_SAMPLE,
    CLASS_PAPER_FORWARD,
    GRADE_FORWARD,
    GRADE_IN_SAMPLE,
    grade_of,
    is_forward_grade,
)

#: The directory under the data root that holds the ledger.
LEDGER_DIR = "paper_momentum"
PICKS_FILE = "picks.jsonl"
SETTLEMENTS_FILE = "settlements.jsonl"

#: The strategy the ledger measures: rank the liquid universe by 26-week
#: momentum and hold the top ten, equal weight, for one week.
STRATEGY_KEY = "weekly_momentum_top10"

#: The two ``evidence_class`` values this source can produce, re-exported so a
#: caller reading the ledger does not have to import the vocabulary module to
#: name what it found.
#:
#: ``PAPER_FORWARD`` — recorded before the outcome was known. Evidence.
#: ``IN_SAMPLE`` — measured on history the rule was selected on. A measurement,
#: but not evidence that the rule still works.
#:
#: The other two classes (``BACKTEST``, ``LIVE_FORWARD``) belong to other
#: sources; the whole vocabulary is assembled in
#: :mod:`atr.research.learning_evidence`, which is also where the ``forward`` /
#: ``in_sample`` grade these map to is defined.

#: The exit the harness applies, and the only reason it can report.
EXIT_REASON = "fixed_hold_7d"

#: NSE close, used to place a ledger *date* on the clock.
CLOSE_HOUR = 15
CLOSE_MINUTE = 30

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def day_of_week(when: Any) -> str | None:
    """The weekday name for a timestamp.

    Local rather than imported from the dataset builder: ``research`` may not
    import ``services`` (``tests/test_architecture.py``), and the dependency
    pointing the other way is the whole reason the statistics modules are
    importable without a database.
    """
    if when is None:
        return None
    try:
        return _DAY_NAMES[when.weekday()]
    except (AttributeError, IndexError):
        return None

#: Substring that marks a week as backfilled, matched case-insensitively against
#: the picks record's own ``source`` field and against its expectation source.
_BACKFILL_MARKER = "backfill"

# ---------------------------------------------------------------------------
# reasons — quoted by the dataset registry so the wording lives in one place
# ---------------------------------------------------------------------------

REASON_NO_MONEY = (
    "the paper ledger records an equal-weight return, not a rupee P&L; no "
    "position size was recorded, so no currency amount can be produced"
)
REASON_NO_COSTS = (
    "the ledger's return is gross: entry and exit are close prices with no "
    "commission and no slippage applied, so it is not comparable to a net "
    "backtest without saying so"
)
REASON_NO_EXCURSION = (
    "the ledger records only the weekly entry and exit, so no intra-trade "
    "excursion was ever observed"
)
REASON_NO_SETUP = (
    "the ledger's entry condition is a cross-sectional momentum rank, which is "
    "not one of the setups in atr.signals.rules; it is left unnamed rather than "
    "filed under 'unknown'"
)
REASON_NO_SIGNAL_FEATURES = (
    "the ledger records no signal reason carrying an RSI, SMA level, prior high "
    "or volume multiple; those are computed from the price cache where history "
    "allows and otherwise left absent"
)


class LedgerError(Exception):
    """The ledger could not be read at all."""


@dataclass(frozen=True)
class LedgerPick:
    symbol: str
    entry_price: float | None
    return_fraction: float | None
    #: True when the pick was listed in the picks record but had no measurable
    #: exit — a missing symbol, or a cache that did not reach the exit date.
    #: Carried so the row is *excluded* rather than settled at zero.
    unsettled: bool = False


@dataclass(frozen=True)
class LedgerWeek:
    """One recorded week: what was picked, and what it did."""

    week: str
    #: ``forward`` or ``in_sample`` — the direction. See
    #: :attr:`evidence_class` for the precise provenance.
    grade: str
    filter: str | None
    entry_date: date
    exit_date: date | None
    picks: tuple[LedgerPick, ...]
    expected_mean_weekly_pct: float | None
    #: Why the week is graded the way it is, quoted in the dataset.
    provenance: str

    @property
    def forward(self) -> bool:
        return is_forward_grade(self.grade)

    @property
    def evidence_class(self) -> str:
        """The class, derived from the grade so the two cannot disagree.

        Derived rather than stored: a frozen dataclass with both a ``grade`` and
        an ``evidence_class`` field can be constructed with the two contradicting
        each other, and whichever a reader happened to look at would decide
        whether the week counted as evidence.
        """
        return CLASS_PAPER_FORWARD if self.forward else CLASS_IN_SAMPLE

    @property
    def settled_picks(self) -> tuple[LedgerPick, ...]:
        return tuple(pick for pick in self.picks if not pick.unsettled)

    @property
    def unsettled_picks(self) -> tuple[LedgerPick, ...]:
        """Picks that were listed but had no measurable exit.

        Carried so they are visible in the counts without producing a row: a
        pick that could not be measured is not a pick that returned nothing.
        """
        return tuple(pick for pick in self.picks if pick.unsettled)


@dataclass
class PaperLedger:
    """The whole ledger, with its own account of what it can and cannot support."""

    weeks: list[LedgerWeek] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: True when the ledger files exist at all. Distinguishes "no paper record
    #: has been written yet" from "a record exists and holds no forward week",
    #: which are different findings.
    present: bool = False

    @property
    def forward_weeks(self) -> list[LedgerWeek]:
        return [week for week in self.weeks if week.forward]

    @property
    def in_sample_weeks(self) -> list[LedgerWeek]:
        return [week for week in self.weeks if not week.forward]

    def counts(self) -> dict[str, int]:
        forward = sum(len(week.settled_picks) for week in self.forward_weeks)
        in_sample = sum(len(week.settled_picks) for week in self.in_sample_weeks)
        return {
            "weeks": len(self.weeks),
            "forward_weeks": len(self.forward_weeks),
            "in_sample_weeks": len(self.in_sample_weeks),
            "trades": forward + in_sample,
            "forward_trades": forward,
            "in_sample_trades": in_sample,
        }

    def summary(self) -> dict[str, Any]:
        counts = self.counts()
        if not self.present:
            note = "no paper ledger has been written yet"
        elif not self.weeks:
            note = "the paper ledger exists but holds no settled week"
        elif counts["forward_trades"] == 0:
            note = (
                f"{counts['in_sample_trades']} settled paper trades exist and "
                "none of them is forward: every settled week is an in-sample "
                "backfill of the harness, so the ledger supports no claim about "
                "whether the rule still works"
            )
        else:
            note = (
                f"{counts['forward_trades']} forward paper trades over "
                f"{counts['forward_weeks']} weeks"
            )
        return {
            "present": self.present,
            "path": LEDGER_DIR,
            "strategy": STRATEGY_KEY,
            **counts,
            "note": note,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse an append-only JSONL file, skipping lines that are not records.

    A half-written final line is a normal consequence of an append-only file
    being read while it is written. It is skipped with a warning rather than
    allowed to discard the whole history — and rather than being silently
    dropped, because a ledger quietly losing a week is a ledger whose counts
    cannot be trusted.
    """
    warnings: list[str] = []
    if not path.exists():
        return [], warnings
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            warnings.append(f"{path.name} line {number} is not valid JSON; skipped")
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            warnings.append(f"{path.name} line {number} is not an object; skipped")
    return records, warnings


def _close_stamp(day: date) -> datetime:
    """A ledger date placed on the clock at the NSE close."""
    return datetime.combine(day, time(CLOSE_HOUR, CLOSE_MINUTE))


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return None


def _grade_of(record: dict[str, Any]) -> tuple[str, str]:
    """``(grade, provenance)`` for one picks record.

    Conservative by design: anything other than an unambiguous forward record is
    graded in-sample. The two markers the producer writes are the record's own
    ``source`` and its ``backtest_expectation.source``; either naming a backfill
    is enough, and neither being present at all is *not* enough to claim
    forward — a record with no provenance is a record whose provenance is
    unknown.

    The grade returned is the direction (``forward`` / ``in_sample``), not the
    class. :attr:`LedgerWeek.evidence_class` derives the class from it, so the
    two can never disagree.
    """
    marker = str(record.get("source") or "").strip()
    expectation = record.get("backtest_expectation")
    expectation_source = ""
    if isinstance(expectation, dict):
        expectation_source = str(expectation.get("source") or "").strip()

    if not marker and expectation_source and _BACKFILL_MARKER not in expectation_source.lower():
        # Written by ``record()``: no ``source`` key, and an expectation whose
        # source names the picks file it was read from.
        return (
            GRADE_FORWARD,
            "recorded forward, with the expectation logged before the outcome",
        )

    if not marker and not expectation_source:
        return (
            GRADE_IN_SAMPLE,
            "the record carries no provenance, so it is graded in-sample rather "
            "than assumed forward",
        )

    return GRADE_IN_SAMPLE, marker or expectation_source


def load_ledger(root: Path | str | None = None) -> PaperLedger:
    """Read the ledger from ``<root>/paper_momentum``.

    ``root`` is the data directory — the same one the dataset builder is given
    for its sector table and price cache, so a test pointing at its own
    directory gets its own ledger and never the operator's.
    """
    base = Path(root) if root is not None else Path("data")
    directory = base / LEDGER_DIR
    picks_path = directory / PICKS_FILE
    settle_path = directory / SETTLEMENTS_FILE

    ledger = PaperLedger(present=picks_path.exists() or settle_path.exists())
    if not ledger.present:
        return ledger

    picks_records, picks_warnings = read_jsonl(picks_path)
    settle_records, settle_warnings = read_jsonl(settle_path)
    ledger.warnings.extend(picks_warnings)
    ledger.warnings.extend(settle_warnings)

    picks_by_week: dict[str, dict[str, Any]] = {}
    for record in picks_records:
        week = str(record.get("week") or "").strip()
        if not week:
            ledger.warnings.append("a picks record carries no week; skipped")
            continue
        if week in picks_by_week:
            # First write wins: the ledger is append-only, so a second record for
            # the same week is a correction, and honouring it would let the
            # record be revised after the outcome — which is the one property
            # the ledger exists to have.
            ledger.warnings.append(
                f"{week} appears more than once in {PICKS_FILE}; the first record is kept"
            )
            continue
        picks_by_week[week] = record

    for record in settle_records:
        week = str(record.get("week") or "").strip()
        if not week:
            ledger.warnings.append("a settlement record carries no week; skipped")
            continue
        source = picks_by_week.get(week)
        if source is None:
            # A settlement with no picks record has no entry price, so no row can
            # be built from it. Reported rather than guessed at.
            ledger.warnings.append(
                f"{week} is settled but has no picks record, so it has no entry "
                "price and produces no rows"
            )
            continue

        entry_date = _parse_date(week)
        if entry_date is None:
            ledger.warnings.append(f"{week} is not a usable date; skipped")
            continue
        exit_date = _parse_date(record.get("exit_window_end"))

        grade, provenance = _grade_of(source)
        returns = record.get("returns")
        returns = returns if isinstance(returns, dict) else {}

        entry_prices: dict[str, float | None] = {}
        for pick in source.get("picks") or []:
            if isinstance(pick, dict) and pick.get("symbol"):
                try:
                    price = float(pick["entry"])
                except (KeyError, TypeError, ValueError):
                    price = None
                entry_prices[str(pick["symbol"])] = price

        settled_symbols = set()
        picks: list[LedgerPick] = []
        for symbol, raw_return in returns.items():
            name = str(symbol)
            try:
                value = float(raw_return)
            except (TypeError, ValueError):
                ledger.warnings.append(f"{week}: {name} has a non-numeric return; skipped")
                continue
            settled_symbols.add(name)
            picks.append(
                LedgerPick(
                    symbol=name,
                    entry_price=entry_prices.get(name),
                    return_fraction=value,
                )
            )

        # A pick that was listed but never settled is carried as unsettled, so it
        # is visible in the counts and produces no row. Writing it as a 0.00%
        # outcome would be a missing value wearing a measurement's clothes.
        for name in entry_prices:
            if name not in settled_symbols:
                picks.append(
                    LedgerPick(symbol=name, entry_price=entry_prices[name],
                               return_fraction=None, unsettled=True)
                )

        if not picks:
            ledger.warnings.append(f"{week} settled with no usable return; skipped")
            continue

        expectation = source.get("backtest_expectation")
        expected = None
        if isinstance(expectation, dict):
            try:
                raw = expectation.get("mean_weekly_pct")
                expected = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                expected = None

        ledger.weeks.append(
            LedgerWeek(
                week=week,
                grade=grade,
                filter=str(source.get("filter") or "") or None,
                entry_date=entry_date,
                exit_date=exit_date,
                picks=tuple(picks),
                expected_mean_weekly_pct=expected,
                provenance=provenance,
            )
        )

    ledger.weeks.sort(key=lambda week: week.week)
    return ledger


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


def build_rows(ledger: PaperLedger, *, strategy_key: str = STRATEGY_KEY) -> list[dict[str, Any]]:
    """Dataset rows for every settled pick.

    Returns *partial* rows: the entry-time features (RSI, ATR, relative volume,
    regime, sector) are added by the dataset builder's enrichment pass, which
    owns the price cache. Everything this function returns comes from the ledger
    itself, so it can be tested without a cache at all.

    An unsettled pick produces no row. A pick whose entry price is missing
    produces no row either — without an entry price there is no return to
    compute and no entry-time bar to enrich against, and a row built anyway
    would carry a ``return_pct`` with nothing behind it.
    """
    rows: list[dict[str, Any]] = []
    for week in ledger.weeks:
        for pick in week.settled_picks:
            if pick.entry_price is None or pick.entry_price <= 0:
                continue
            if pick.return_fraction is None:
                continue
            rows.append(_row(week, pick, strategy_key=strategy_key))
    return rows


def _row(week: LedgerWeek, pick: LedgerPick, *, strategy_key: str) -> dict[str, Any]:
    entry_price = float(pick.entry_price)
    fraction = float(pick.return_fraction)
    exit_price = entry_price * (1.0 + fraction)
    entry_ts = _close_stamp(week.entry_date)
    exit_ts = _close_stamp(week.exit_date) if week.exit_date else None

    missing = [
        "quantity",
        "gross_pnl",
        "net_pnl",
        "commission",
        "costs",
        "mfe",
        "mae",
        "slippage_bps",
        "setup",
        "signal_volume_multiple",
        "prior_high",
        "breakout_proximity_pct",
        "sma_fast",
        "sma_slow",
        "sma_long",
    ]

    return {
        "source": "PAPER",
        "source_ref": f"{LEDGER_DIR}/{week.week}",
        "trade_ref": f"paper#{week.week}#{pick.symbol}",
        # The weekly ledger aggregates picks, not signals: there is no raising
        # order and no recorded context to retain. ``None`` here is the honest
        # value, and data-quality monitoring reports it as unlinked rather
        # than the builder inventing a link.
        "signal_id": None,
        "opening_order_id": None,
        "context_score": None,
        "context_model_version": None,
        "context_class": None,
        "strategy_key": strategy_key,
        "strategy_id": None,
        "strategy_version": None,
        "engine_key": None,
        "symbol": pick.symbol,
        "sector": None,
        "direction": "LONG",
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "duration_days": (
            (week.exit_date - week.entry_date).days if week.exit_date else None
        ),
        # Both are functions of the entry stamp, and the builder's enrichment
        # pass fills neither — it owns the price cache, not the calendar. A row
        # that left them blank would silently remove two of the axes the
        # specification asks for, with a coverage of zero rather than an error.
        "day_of_week": day_of_week(entry_ts),
        "time_of_day": time_of_day(entry_ts),
        "quantity": None,
        "entry_price": entry_price,
        # Algebra on a recorded return, not an estimate: the ledger measured
        # exit/entry - 1, so exit is recoverable exactly.
        "exit_price": exit_price,
        "gross_pnl": None,
        "commission": None,
        "net_pnl": None,
        "return_pct": fraction * 100.0,
        "exit_reason": EXIT_REASON,
        "mfe": None,
        "mae": None,
        "slippage_bps": None,
        # The rule that produced the pick, as free text. No parser will read a
        # volume multiple or an RSI out of it, which is the correct outcome: the
        # ledger never recorded one.
        "signal_reason": (
            f"weekly momentum {week.filter}" if week.filter else "weekly momentum"
        ),
        "setup": None,
        "rsi": None,
        "sma_fast": None,
        "sma_slow": None,
        "sma_long": None,
        "prior_high": None,
        "signal_volume_multiple": None,
        "breakout_proximity_pct": None,
        #: The precise provenance: ``PAPER_FORWARD`` for a record written before
        #: its outcome, ``IN_SAMPLE`` for a measurement on the history the rule
        #: was selected on. Read by the drift comparison, which needs to tell
        #: paper from live.
        "evidence_class": week.evidence_class,
        #: The direction, derived from the class by the week so the two cannot
        #: disagree. This is the column a finding is published on.
        "evidence_grade": week.grade,
        #: Why the week carries the grade it does, in the ledger's own words.
        "evidence_note": week.provenance,
        #: The same fact as a boolean, so no consumer has to restate the
        #: membership test and get it wrong.
        "is_forward": week.forward,
        "portfolio_exposure_at_entry": None,
        "strategy_allocation": None,
        "sector_exposure_pct": None,
        "position_concentration": None,
        "risk_per_trade": None,
        "position_size": None,
        "position_value": None,
        "stop_distance": None,
        "atr_based_risk": None,
        "_missing": missing,
    }


def reasons() -> dict[str, str]:
    """The paper-specific missing-feature reasons, for the dataset registry."""
    return {
        "net_pnl": REASON_NO_MONEY,
        "quantity": REASON_NO_MONEY,
        "gross_pnl": REASON_NO_MONEY,
        "commission": REASON_NO_MONEY,
        "costs": REASON_NO_COSTS,
        "mfe": REASON_NO_EXCURSION,
        "mae": REASON_NO_EXCURSION,
        "slippage_bps": REASON_NO_EXCURSION,
        "setup": REASON_NO_SETUP,
        "signal_volume_multiple": REASON_NO_SIGNAL_FEATURES,
        "prior_high": REASON_NO_SIGNAL_FEATURES,
        "breakout_proximity_pct": REASON_NO_SIGNAL_FEATURES,
    }


__all__ = [
    "CLOSE_HOUR",
    "CLOSE_MINUTE",
    "EXIT_REASON",
    "CLASS_PAPER_FORWARD",
    "CLASS_IN_SAMPLE",
    "GRADE_FORWARD",
    "GRADE_IN_SAMPLE",
    "LEDGER_DIR",
    "LedgerError",
    "LedgerPick",
    "LedgerWeek",
    "PICKS_FILE",
    "PaperLedger",
    "REASON_NO_COSTS",
    "REASON_NO_EXCURSION",
    "REASON_NO_MONEY",
    "REASON_NO_SETUP",
    "REASON_NO_SIGNAL_FEATURES",
    "SETTLEMENTS_FILE",
    "STRATEGY_KEY",
    "build_rows",
    "day_of_week",
    "load_ledger",
    "read_jsonl",
    "reasons",
]
