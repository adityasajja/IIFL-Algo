"""The evidence vocabulary — one home, so nothing can restate it wrongly.

Why this module exists
----------------------

Three separate parts of the system have to agree on a single question: *is this
record evidence, or is it a measurement of the history the rule was chosen from?*

* the paper-ledger reader (:mod:`atr.research.learning_paper`) grades a settled
  week;
* the trade journal (:mod:`atr.services.journal`) grades a closed episode, from
  the provenance stamp the OMS wrote when it raised the order;
* the dataset builder (:mod:`atr.services.learning`) stamps every row and the
  analysis, the report and the dashboard all read that stamp.

Before this module the answer was spelled out in each of those places, and the
failure mode of a disagreement is silent in exactly one direction: a record
graded forward by one reader and in-sample by another is counted as evidence by
whichever reader is asked. So the vocabulary lives here, in the compute layer,
where both ``research`` and ``services`` can import it and neither has to import
the other.

Two levels, because they answer two questions
---------------------------------------------

``evidence_class`` — **where did this come from?** Four values:

``BACKTEST``
    A simulated run. In-sample by construction: the rule was selected on the
    history it is measured over. A number, not evidence that the rule works.
``IN_SAMPLE``
    A measurement on that same selection history — a backfilled ledger week, or
    a paper fill whose record cannot demonstrate it predates its own outcome.
``PAPER_FORWARD``
    A paper trade recorded before its outcome was known. **Evidence.**
``LIVE_FORWARD``
    The same, with real money.

``evidence_grade`` — **does a finding depend on it?** Two values, ``forward``
and ``in_sample``, derived from the class by :func:`grade_of`. Every consumer
that has to decide whether it may publish a number reads this one, and no
consumer has to know the four-value vocabulary to read it correctly.

Why the conservative default points one way
-------------------------------------------

Anything unrecognised, absent or ambiguous grades **in-sample**. Over-claiming
independence is silent — an in-sample number wearing a forward label reads
exactly like a finding, and nothing downstream can tell. Under-claiming it is
merely conservative: the worst outcome is that the engine says it has proven
less than it has. The two failure modes are not symmetric, so the default is
not neutral.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

SOURCE_BACKTEST = "BACKTEST"
SOURCE_PAPER = "PAPER"
SOURCE_LIVE = "LIVE"
#: In the order the drift analysis compares them.
SOURCES = (SOURCE_BACKTEST, SOURCE_PAPER, SOURCE_LIVE)

# ---------------------------------------------------------------------------
# evidence_class — the precise provenance
# ---------------------------------------------------------------------------

CLASS_BACKTEST = "BACKTEST"
CLASS_IN_SAMPLE = "IN_SAMPLE"
CLASS_PAPER_FORWARD = "PAPER_FORWARD"
CLASS_LIVE_FORWARD = "LIVE_FORWARD"
EVIDENCE_CLASSES = (
    CLASS_BACKTEST,
    CLASS_IN_SAMPLE,
    CLASS_PAPER_FORWARD,
    CLASS_LIVE_FORWARD,
)

#: The classes that are evidence. Everything else is a measurement.
FORWARD_CLASSES = (CLASS_PAPER_FORWARD, CLASS_LIVE_FORWARD)
IN_SAMPLE_CLASSES = (CLASS_BACKTEST, CLASS_IN_SAMPLE)

# ---------------------------------------------------------------------------
# evidence_grade — the direction, and the only split a finding depends on
# ---------------------------------------------------------------------------

GRADE_FORWARD = "forward"
GRADE_IN_SAMPLE = "in_sample"
GRADES = (GRADE_FORWARD, GRADE_IN_SAMPLE)

#: ``source`` -> the class a *forward* record of that source belongs to. A
#: journal entry from a LIVE deployment is the only thing that can produce
#: ``LIVE_FORWARD``; everything else paper-traded is ``PAPER_FORWARD``.
_FORWARD_CLASS_BY_SOURCE = {
    SOURCE_PAPER: CLASS_PAPER_FORWARD,
    SOURCE_LIVE: CLASS_LIVE_FORWARD,
}

#: ``evidence_class`` -> ``evidence_grade``. Total by construction: every class
#: is either evidence or a measurement, and there is no third option.
_GRADE_BY_CLASS = {
    CLASS_BACKTEST: GRADE_IN_SAMPLE,
    CLASS_IN_SAMPLE: GRADE_IN_SAMPLE,
    CLASS_PAPER_FORWARD: GRADE_FORWARD,
    CLASS_LIVE_FORWARD: GRADE_FORWARD,
}


def forward_class(source: str) -> str:
    """The class a forward record of ``source`` belongs to."""
    return _FORWARD_CLASS_BY_SOURCE.get(str(source).upper(), CLASS_PAPER_FORWARD)


def grade_of(evidence_class: str | None) -> str:
    """The grade of a record, from its class.

    **An unrecognised or absent class grades in-sample.** See the module
    docstring for why the default is not neutral.
    """
    return _GRADE_BY_CLASS.get(evidence_class, GRADE_IN_SAMPLE)


def is_forward_class(evidence_class: str | None) -> bool:
    """Whether an ``evidence_class`` is evidence rather than a measurement."""
    return evidence_class in FORWARD_CLASSES


def is_forward_grade(grade: str | None) -> bool:
    """Whether an ``evidence_grade`` is evidence. Exact match, no coercion.

    ``str(grade).lower()`` would look harmless and would silently accept
    ``"Forward"``, ``" forward"`` and ``"PAPER_FORWARD"`` — three spellings the
    column is not supposed to hold, each of which would then be counted as
    evidence here and as in-sample by :func:`grade_of`. The two tests have to
    agree, so neither one guesses.
    """
    return grade == GRADE_FORWARD


def row_grade(row: dict[str, Any]) -> str:
    """The grade of a dataset row, resolved from the most specific field present.

    Rows built by the dataset builder carry ``evidence_grade``,
    ``evidence_class`` and ``is_forward``, stamped together. But a row can also
    arrive from a caller that supplied only one of them, and the three must
    never be able to disagree: the explicit grade wins where it is present, then
    the class is translated through :func:`grade_of`, and only then is the
    boolean read. A row carrying none of the three is graded in-sample, which is
    the same default :func:`grade_of` applies and for the same reason.
    """
    grade = row.get("evidence_grade")
    if grade in GRADES:
        return grade
    evidence_class = row.get("evidence_class")
    if evidence_class in _GRADE_BY_CLASS:
        return _GRADE_BY_CLASS[evidence_class]
    if row.get("is_forward"):
        return GRADE_FORWARD
    return GRADE_IN_SAMPLE


def has_grade(row: dict[str, Any]) -> bool:
    """Whether a row states a grade of its own at all.

    Narrower than :func:`row_grade`, and it exists because the two are different
    findings. ``row_grade`` answers "how must this be treated?" and defaults
    conservatively; this answers "did anyone actually record a verdict?", and a
    row that states nothing is not the same as a row that states *in-sample* —
    even though both must be treated identically. A caveat that said "every row
    here is in-sample" about a book nobody graded would be asserting something
    the data does not contain.
    """
    if row.get("evidence_grade") in GRADES:
        return True
    if row.get("evidence_class") in EVIDENCE_CLASSES:
        return True
    return bool(row.get("is_forward"))


#: Why a journal row carries the grade it does, in the grader's own words. A
#: grade with no stated basis is a label a reader has to take on trust, so the
#: note is resolved once, where the grade is decided, and travels with the row.
#: These live here — with the vocabulary, not the builder — so every consumer
#: that reads a note (reports, quality monitors) quotes the same sentence the
#: grader wrote.
NOTE_FORWARD_STAMPED = (
    "the opening order carries an execution-time provenance stamp, so this trade "
    "was recorded before its outcome was known"
)
NOTE_MARKED_IN_SAMPLE = (
    "the opening order was explicitly marked in-sample when the trade was "
    "journaled, so it is a measurement rather than evidence"
)
NOTE_NO_STAMP = (
    "the opening order carries no execution-time provenance stamp, so this "
    "record cannot demonstrate it was written before its own outcome; graded "
    "in-sample rather than assumed forward"
)
NOTE_BACKTEST_IN_SAMPLE = (
    "a simulated run: the rule was selected on the history it is measured over, "
    "so the number is a measurement rather than evidence that the rule still "
    "works"
)


__all__ = [
    "CLASS_BACKTEST",
    "CLASS_IN_SAMPLE",
    "CLASS_LIVE_FORWARD",
    "CLASS_PAPER_FORWARD",
    "EVIDENCE_CLASSES",
    "FORWARD_CLASSES",
    "GRADE_FORWARD",
    "GRADE_IN_SAMPLE",
    "GRADES",
    "IN_SAMPLE_CLASSES",
    "NOTE_BACKTEST_IN_SAMPLE",
    "NOTE_FORWARD_STAMPED",
    "NOTE_MARKED_IN_SAMPLE",
    "NOTE_NO_STAMP",
    "SOURCES",
    "SOURCE_BACKTEST",
    "SOURCE_LIVE",
    "SOURCE_PAPER",
    "forward_class",
    "grade_of",
    "has_grade",
    "is_forward_class",
    "is_forward_grade",
    "row_grade",
]
