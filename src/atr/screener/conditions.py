"""The condition tree: nested AND/OR logic that explains itself.

A screen is a tree of nodes. Each node is either a **group** (with a ``match`` of
``all`` or ``any`` and a list of children) or a **leaf** (an indicator, an
operator, and a comparison target). Groups nest to any depth, so
``(A AND B AND C) OR (D AND E)`` — the shape the brief asks for — is just a group
of two groups.

The part that matters
---------------------

Evaluating a leaf does not return a bare boolean. It returns an
:class:`Evidence` record carrying the label, the actual value, the operator, the
threshold, and a human sentence. A screen that says "matched" without saying why
is a list you have to take on faith.

Evidence is attached to *both* outcomes. A leaf that failed is still recorded, so
a symbol that did not match can be explained too — which is what you need when a
screen returns nothing and you have to work out which condition was too tight.

Three-valued logic
------------------

An indicator that cannot be computed yields NaN, and NaN makes a leaf ``False``
rather than raising. But the evidence marks it ``unmeasurable: True``, so the
difference between "the value was there and did not pass" and "there was no value
to test" survives into the response. Collapsing those two into one boolean is how
a screener silently reports data gaps as non-matches.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from atr.screener.indicators import (
    INDICATOR_INDEX,
    IndicatorSpec,
    indicator_series,
    last_value,
    resolve_indicator,
)

# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class ConditionError(ValueError):
    """A condition tree is structurally invalid. Carries a code for the API."""

    def __init__(self, message: str, *, code: str = "invalid_condition", path: str = ""):
        self.code = code
        self.path = path
        super().__init__(message)


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------
def _eq(a: float, b: float) -> bool:
    # A screener comparing floats needs a tolerance, or "RSI = 63" never matches.
    return abs(a - b) < 1e-9


def _pct_within(a: float, b: float, pct: float) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / abs(b) * 100 <= pct


#: Comparison operators. Each is `(left, right) -> bool` and is NaN-safe by the
#: caller's guard rather than individually, so the table stays readable.
OPERATORS: dict[str, dict[str, Any]] = {
    ">": {"fn": lambda a, b: a > b, "label": ">", "arity": "binary",
          "description": "greater than"},
    ">=": {"fn": lambda a, b: a >= b, "label": "≥", "arity": "binary",
           "description": "greater than or equal to"},
    "<": {"fn": lambda a, b: a < b, "label": "<", "arity": "binary",
          "description": "less than"},
    "<=": {"fn": lambda a, b: a <= b, "label": "≤", "arity": "binary",
           "description": "less than or equal to"},
    "=": {"fn": _eq, "label": "=", "arity": "binary", "description": "equal to"},
    "!=": {"fn": lambda a, b: not _eq(a, b), "label": "≠", "arity": "binary",
           "description": "not equal to"},
    "between": {"fn": None, "label": "between", "arity": "range",
                "description": "within an inclusive range"},
    "outside": {"fn": None, "label": "outside", "arity": "range",
                "description": "outside an inclusive range"},
    "within_pct": {"fn": _pct_within, "label": "within %", "arity": "pct",
                   "description": "within a percentage of the target"},
    "crosses_above": {"fn": None, "label": "crosses above", "arity": "series",
                      "description": "the series rose above the target on the last bar"},
    "crosses_below": {"fn": None, "label": "crosses below", "arity": "series",
                      "description": "the series fell below the target on the last bar"},
}

#: Operators whose target is another indicator rather than a number.
_SERIES_OPS = ("crosses_above", "crosses_below")


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------
@dataclass
class Evidence:
    """Why one leaf did or did not pass. The audit trail for a screen."""

    #: What was tested, in the user's language.
    label: str
    #: The indicator key and period, for the frontend to echo back.
    indicator: str
    period: int | None
    op: str
    #: The value actually observed, or None when unmeasurable.
    value: float | None
    #: The threshold compared against, when the target is a number.
    target: float | None = None
    #: The target indicator's key, when the target is another indicator.
    target_indicator: str | None = None
    target_period: int | None = None
    #: The second bound, for ``between`` / ``outside``.
    upper: float | None = None
    #: True when the indicator could not be computed at all.
    unmeasurable: bool = False
    #: The leaf's own verdict.
    passed: bool = False
    #: A sentence a person can read. This is the "WHY" in the response.
    reason: str = ""
    unit: str = "number"

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "indicator": self.indicator,
            "period": self.period,
            "op": self.op,
            "value": self.value,
            "target": self.target,
            "target_indicator": self.target_indicator,
            "target_period": self.target_period,
            "upper": self.upper,
            "unmeasurable": self.unmeasurable,
            "passed": self.passed,
            "reason": self.reason,
            "unit": self.unit,
        }


def _fmt(value: float | None, unit: str) -> str:
    """Format a value the way the screen should read it."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    if unit == "integer":
        return f"{value:,.0f}"
    if unit == "multiple":
        return f"{value:,.2f}x"
    if unit == "percent":
        return f"{value:,.2f}%"
    if unit == "currency":
        return f"{value:,.2f}"
    return f"{value:,.2f}"


# ---------------------------------------------------------------------------
# leaf
# ---------------------------------------------------------------------------
@dataclass
class Leaf:
    """One indicator test."""

    indicator: str
    op: str
    #: A number, or None when comparing against another indicator.
    value: float | None = None
    upper: float | None = None
    #: Period for the left-hand indicator.
    period: int | None = None
    #: The right-hand indicator, when ``rhs`` is an indicator.
    rhs_indicator: str | None = None
    rhs_period: int | None = None
    #: Optional user label overriding the generated one.
    label: str | None = None

    @property
    def spec(self) -> IndicatorSpec:
        return resolve_indicator(self.indicator)

    def describe(self) -> str:
        if self.label:
            return self.label
        spec = self.spec
        left = spec.label
        if self.period and spec.takes_period:
            left = f"{left} {self.period}"
        meta = OPERATORS.get(self.op, {})
        shown = meta.get("label", self.op)
        if self.rhs_indicator:
            right = resolve_indicator(self.rhs_indicator).label
            if self.rhs_period:
                right = f"{right} {self.rhs_period}"
            return f"{left} {shown} {right}"
        if self.op in ("between", "outside"):
            return f"{left} {shown} {_fmt(self.value, spec.unit)}–{_fmt(self.upper, spec.unit)}"
        if self.op == "within_pct":
            return f"{left} within {_fmt(self.value, 'number')}% of {_fmt(self.upper, spec.unit)}"
        return f"{left} {shown} {_fmt(self.value, spec.unit)}"

    def evaluate(self, df: pd.DataFrame) -> Evidence:
        """Test the leaf against the last bar of *df*, with its reasoning."""
        spec = self.spec
        meta = OPERATORS.get(self.op)
        if meta is None:
            raise ConditionError(
                f"unknown operator {self.op!r}. Known: {', '.join(sorted(OPERATORS))}",
                code="unknown_operator",
            )
        if not spec.available:
            raise ConditionError(
                f"{spec.label} is not available: it requires {spec.requires}",
                code="indicator_unavailable",
            )

        period = self.period if spec.takes_period else None
        left_series = indicator_series(df, self.indicator, period)
        left = last_value(left_series)
        label = self.describe()

        unmeasurable = not math.isfinite(left)

        # --- right-hand operand ------------------------------------------
        right: float = float("nan")
        target_indicator: str | None = None
        target_period: int | None = None
        if self.rhs_indicator:
            target_indicator = self.rhs_indicator
            target_period = self.rhs_period
            right_spec = resolve_indicator(self.rhs_indicator)
            right_period = self.rhs_period if right_spec.takes_period else None
            right = last_value(indicator_series(df, self.rhs_indicator, right_period))
            if not math.isfinite(right):
                unmeasurable = True
        elif self.value is not None:
            right = float(self.value)

        # --- evaluate ----------------------------------------------------
        passed = False
        if not unmeasurable:
            fn = meta["fn"]
            if self.op in _SERIES_OPS:
                passed = self._evaluate_cross(left_series, right)
            elif self.op == "between":
                upper = float(self.upper if self.upper is not None else right)
                passed = bool(right <= left <= upper)
            elif self.op == "outside":
                upper = float(self.upper if self.upper is not None else right)
                passed = bool(left < right or left > upper)
            elif fn is not None:
                try:
                    passed = bool(fn(left, right))
                except Exception:  # noqa: BLE001 - a failed comparison is not a match
                    passed = False

        return Evidence(
            label=label,
            indicator=self.indicator,
            period=period,
            op=self.op,
            value=left if not unmeasurable else None,
            # ``target`` always carries the right-hand number, including when the
            # right-hand side is another indicator. Leaving it None in that case
            # (the earlier behaviour) meant the compared-against value existed
            # only inside the prose, so a UI could not render "80.25 vs 80.60"
            # as two fields without parsing the sentence it had just built.
            target=float(right) if math.isfinite(right) else None,
            target_indicator=target_indicator,
            target_period=target_period,
            upper=self.upper,
            unmeasurable=unmeasurable,
            passed=passed,
            reason=self._sentence(left, right, passed, unmeasurable, label),
            unit=spec.unit,
        )

    def _evaluate_cross(self, series: pd.Series, right: float) -> bool:
        """Whether the series crossed its target on the most recent bar."""
        if len(series) < 2 or not math.isfinite(right):
            return False
        now = last_value(series)
        prev = last_value(series.iloc[:-1])
        if not (math.isfinite(now) and math.isfinite(prev)):
            return False
        if self.op == "crosses_above":
            return bool(now > right and prev <= right)
        return bool(now < right and prev >= right)

    def _sentence(
        self,
        left: float,
        right: float,
        passed: bool,
        unmeasurable: bool,
        label: str,
    ) -> str:
        unit = self.spec.unit
        if unmeasurable:
            return f"{label} — could not be computed for this symbol"
        shown = _fmt(left, unit)
        if self.rhs_indicator:
            rhs_spec = resolve_indicator(self.rhs_indicator)
            # Spell the period out. "satisfies > EMA 1,165.06" does not tell you
            # whether that is the 20 or the 200, and the whole point of this
            # string is that a reader can audit the claim without opening code.
            rhs_name = rhs_spec.label
            if self.rhs_period and rhs_spec.takes_period:
                rhs_name = f"{rhs_name} {self.rhs_period}"
            against = f"{rhs_name} {_fmt(right, rhs_spec.unit)}"
        elif self.op in ("between", "outside"):
            against = f"{_fmt(right, unit)}–{_fmt(self.upper if self.upper is not None else right, unit)}"
        elif self.op == "within_pct":
            against = f"{_fmt(self.value, 'number')}% of {_fmt(self.upper, unit)}"
        else:
            against = _fmt(right, unit)

        if self.op == "crosses_above":
            return f"{label} — closed at {shown}, crossing above {against}"
        if self.op == "crosses_below":
            return f"{label} — closed at {shown}, crossing below {against}"
        if self.op in ("between", "outside"):
            return f"{label} = {shown}, {'inside' if passed else 'outside'} {against}"
        return f"{label} = {shown}, {'satisfies' if passed else 'fails'} {OPERATORS[self.op]['label']} {against}"


# ---------------------------------------------------------------------------
# group
# ---------------------------------------------------------------------------
@dataclass
class Group:
    """A set of child nodes combined with AND (``all``) or OR (``any``)."""

    match: str = "all"
    children: list["Node"] = field(default_factory=list)
    label: str | None = None
    #: Negate the whole group. ``not`` is far more readable in a UI than
    #: restating every leaf with the opposite operator.
    negate: bool = False

    @property
    def op(self) -> str:
        return "all" if self.match == "all" else "any"

    def describe(self) -> str:
        joiner = " AND " if self.match == "all" else " OR "
        inner = joiner.join(c.describe() for c in self.children)
        text = f"({inner})" if len(self.children) > 1 else inner
        return f"NOT {text}" if self.negate else text

    def evaluate(self, df: pd.DataFrame) -> tuple[bool, list[Evidence]]:
        results: list[tuple[bool, list[Evidence]]] = [
            evaluate_node(child, df) for child in self.children
        ]
        if not results:
            # An empty group is a no-op, not a match-all. Matching everything is
            # how a half-built screen returns the whole exchange.
            return False, []

        verdicts = [v for v, _ in results]
        combined = all(verdicts) if self.match == "all" else any(verdicts)
        if self.negate:
            combined = not combined
        evidence = [e for _, evs in results for e in evs]
        return combined, evidence


Node = Leaf | Group


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
def evaluate_node(node: Node, df: pd.DataFrame) -> tuple[bool, list[Evidence]]:
    """Evaluate any node, returning its verdict and every leaf's evidence."""
    if isinstance(node, Group):
        return node.evaluate(df)
    evidence = node.evaluate(df)
    return evidence.passed, [evidence]


def evaluate_symbol(
    df: pd.DataFrame, root: Node, *, min_bars: int = 60
) -> tuple[bool, list[Evidence]]:
    """Evaluate a whole screen against one symbol's frame.

    ``min_bars`` guards the indicators rather than the tree: SMA 50 over 12 bars
    is a number about nothing, so a frame too short to carry the warmup is
    reported as unmeasurable rather than silently scored.
    """
    if df is None or df.empty:
        return False, []
    if len(df) < min_bars:
        return False, [
            Evidence(
                label=f"history ≥ {min_bars} sessions",
                indicator="bars",
                period=None,
                op=">=",
                value=float(len(df)),
                target=float(min_bars),
                unmeasurable=True,
                passed=False,
                reason=f"only {len(df)} sessions cached — too short to evaluate",
                unit="integer",
            )
        ]
    return evaluate_node(root, df)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def parse_node(raw: Any, *, path: str = "root") -> Node:
    """Build the tree from JSON, validating as it goes.

    Accepts two shapes, because both are natural to write:

    * ``{"match": "all", "conditions": [ ... ]}`` — a group.
    * ``{"indicator": "rsi", "op": "<", "value": 35}`` — a leaf.

    A node carrying both ``conditions`` and ``indicator`` is ambiguous and is
    rejected rather than guessed at.
    """
    if not isinstance(raw, dict):
        raise ConditionError(
            f"condition at {path} must be an object, got {type(raw).__name__}",
            path=path,
        )

    children = raw.get("conditions")
    indicator = raw.get("indicator")

    if children is not None and indicator is not None:
        raise ConditionError(
            f"condition at {path} has both 'conditions' and 'indicator'; "
            "a node is either a group or a leaf",
            code="ambiguous_node",
            path=path,
        )
    if children is not None or raw.get("match") is not None:
        if children is None:
            raise ConditionError(
                f"group at {path} has 'match' but no 'conditions'",
                code="empty_group",
                path=path,
            )
        if not isinstance(children, list):
            raise ConditionError(
                f"'conditions' at {path} must be a list", path=path
            )
        match = str(raw.get("match", "all")).strip().lower()
        if match in ("and", "all"):
            match = "all"
        elif match in ("or", "any"):
            match = "any"
        else:
            raise ConditionError(
                f"group at {path} has match={match!r}; expected 'all' or 'any'",
                code="bad_match",
                path=path,
            )
        return Group(
            match=match,
            children=[
                parse_node(child, path=f"{path}.conditions[{i}]")
                for i, child in enumerate(children)
            ],
            label=raw.get("label"),
            negate=bool(raw.get("negate", False)),
        )

    if indicator is None:
        raise ConditionError(
            f"condition at {path} has neither 'indicator' nor 'conditions'",
            code="empty_node",
            path=path,
        )

    op = str(raw.get("op", "")).strip()
    if not op:
        raise ConditionError(f"condition at {path} is missing 'op'", path=path)
    # Reject an unknown operator here, not at evaluation time. Validating the
    # indicator but not the operator was an asymmetry with real consequences: a
    # typo'd ``~=`` parsed cleanly, saved cleanly, and then returned zero rows at
    # scan time — which reads as "nothing matched today" rather than "you made a
    # typo", and is exactly the silent failure this design is meant to prevent.
    if op not in OPERATORS:
        raise ConditionError(
            f"unknown operator {op!r} at {path}. Known: {', '.join(sorted(OPERATORS))}",
            code="unknown_operator",
            path=path,
        )

    period = raw.get("period")
    if period is not None:
        try:
            period = int(period)
        except (TypeError, ValueError) as exc:
            raise ConditionError(
                f"period at {path} must be an integer, got {period!r}", path=path
            ) from exc
        if period <= 0:
            raise ConditionError(
                f"period at {path} must be positive, got {period}", path=path
            )

    rhs_indicator = raw.get("rhs_indicator")
    rhs_period = raw.get("rhs_period")

    # A bare ``period`` is ambiguous on an indicator-vs-indicator leaf. The
    # natural reading of ``close > ema`` with ``period: 50`` is "EMA 50", but
    # ``close`` takes no period, so applying it to the left operand would
    # silently drop it (and mean "EMA 20", the default) — a screen that quietly
    # tests something other than what it says. Route it to whichever side can
    # actually use it. When *both* sides take a period, the field belongs to the
    # left operand and RHS callers must say ``rhs_period``; leaving the LHS
    # unperiodised in that case is a genuine ambiguity, so it is left alone.
    if period is not None and rhs_indicator and rhs_period is None:
        lhs_takes = resolve_indicator(indicator).takes_period
        if not lhs_takes:
            rhs_period = period
            period = None

    if rhs_period is not None:
        try:
            rhs_period = int(rhs_period)
        except (TypeError, ValueError) as exc:
            raise ConditionError(
                f"rhs_period at {path} must be an integer, got {rhs_period!r}", path=path
            ) from exc

    value = raw.get("value")
    if value is not None and not isinstance(value, (int, float)) or (
        isinstance(value, float) and not math.isfinite(value)
    ):
        raise ConditionError(
            f"'value' at {path} must be a finite number, got {value!r}", path=path
        )
    upper = raw.get("upper")
    if upper is not None:
        if not isinstance(upper, (int, float)) or (
            isinstance(upper, float) and not math.isfinite(upper)
        ):
            raise ConditionError(
                f"'upper' at {path} must be a finite number, got {upper!r}", path=path
            )

    if op in ("between", "outside") and (value is None or upper is None):
        raise ConditionError(
            f"operator {op!r} at {path} needs both 'value' and 'upper'",
            code="missing_bound",
            path=path,
        )
    if op not in _SERIES_OPS and rhs_indicator is None and value is None:
        raise ConditionError(
            f"condition at {path} has no comparison target: give 'value' or 'rhs_indicator'",
            code="missing_target",
            path=path,
        )

    # Resolve names up front so an unknown indicator fails validation rather
    # than silently evaluating to false at scan time. ``resolve_indicator``
    # raises ``KeyError``; translate it, because a raw KeyError would escape as a
    # 500 and tell the caller nothing about which field was wrong.
    try:
        lhs_spec = resolve_indicator(indicator)
    except KeyError as exc:
        raise ConditionError(
            f"unknown indicator {indicator!r} at {path}. Known: {', '.join(sorted(INDICATOR_INDEX))}",
            code="unknown_indicator",
            path=path,
        ) from exc
    if rhs_indicator:
        try:
            spec_for_rhs = resolve_indicator(rhs_indicator)
        except KeyError as exc:
            raise ConditionError(
                f"unknown right-hand indicator {rhs_indicator!r} at {path}. "
                f"Known: {', '.join(sorted(INDICATOR_INDEX))}",
                code="unknown_indicator",
                path=path,
            ) from exc
    else:
        spec_for_rhs = None

    # An indicator that exists but cannot be computed here is rejected at build
    # time with the capability it needs, so the UI can explain rather than
    # return a screen that matches nothing.
    for spec, where in ((lhs_spec, "indicator"), (spec_for_rhs, "rhs_indicator")):
        if spec is not None and not spec.available:
            raise ConditionError(
                f"{where} {spec.key!r} is not available: it requires {spec.requires}",
                code="indicator_unavailable",
                path=path,
            )

    return Leaf(
        indicator=str(indicator).strip().lower(),
        op=op,
        value=None if value is None else float(value),
        upper=None if upper is None else float(upper),
        period=period,
        rhs_indicator=str(rhs_indicator).strip().lower() if rhs_indicator else None,
        rhs_period=rhs_period,
        label=raw.get("label"),
    )


def validate_tree(raw: Any) -> tuple[Node | None, list[str]]:
    """Parse a tree, returning ``(node, warnings)`` and never raising for data.

    Structural problems propagate as :class:`ConditionError`; the caller turns
    that into a 400. Warnings are non-fatal notes about indicators that exist but
    cannot be computed, so the UI can explain a greyed-out row.
    """
    node = parse_node(raw)
    warnings: list[str] = []
    for leaf in _leaves(node):
        spec = resolve_indicator(leaf.indicator)
        if not spec.available:
            warnings.append(f"{spec.label} is not available: requires {spec.requires}")
    return node, warnings


def _leaves(node: Node) -> list[Leaf]:
    if isinstance(node, Group):
        out: list[Leaf] = []
        for child in node.children:
            out.extend(_leaves(child))
        return out
    return [node]


def count_nodes(node: Node) -> tuple[int, int]:
    """``(groups, leaves)`` — used to bound an absurdly large submission."""
    if isinstance(node, Group):
        groups, leaves = 1, 0
        for child in node.children:
            g, l = count_nodes(child)
            groups += g
            leaves += l
        return groups, leaves
    return 0, 1


def iter_leaves(node: Node):
    """Yield every leaf in the tree, left to right."""
    if isinstance(node, Group):
        for child in node.children:
            yield from iter_leaves(child)
    else:
        yield node
