"""Champion vs challenger: can the two arms be read yet — never which one wins.

A champion (current strategy version) and a challenger (new version) trade the
same market side by side in PAPER. This module compares what each arm has
*measured*, and it stops there on purpose: with thin samples any "winner" is
noise wearing a label, and this system does not print labels it cannot defend.

What is reused, not rebuilt
----------------------------
* :mod:`atr.research.learning_stats` — ``MIN_SAMPLE`` floors every arm; every
  mean, win rate and interval comes from :func:`summarise`.
* :mod:`atr.research.learning_readiness` — ``ANALYSIS_READY_N`` is the
  comparison floor, ``summarise_forward`` builds each arm's figures, and only
  closed forward rows enter (in-sample rows and open trades are excluded, not
  zeroed).
* The verdicts below are *readiness* verdicts, not findings. They say whether
  the comparison can be read, using the same gates the readiness view uses.

Honesty rules specific to this surface
--------------------------------------
* **No winner, ever.** Deltas are reported as challenger-minus-champion
  descriptions with both sample sizes beside them. There is no significance
  test on the difference — adding one would be a new statistic, and a tested
  difference this early would be read as a promotion order.
* **No promotion path.** Nothing here changes a deployment, a version or a
  risk limit, and there is deliberately no function that could: promotion is a
  human decision taken elsewhere, and this module must not grow the vocabulary
  for it.
* **Parity is reported, not assumed.** "Same conditions" is a claim about two
  deployment rows (universe, capital, mode, shared venue). The comparison
  states what matches and what does not rather than trusting the setup.
"""

from __future__ import annotations

from typing import Any

from atr.research import learning_stats as stats
from atr.research.learning_readiness import (
    ANALYSIS_READY_N,
    evidence_status,
    next_gate,
    summarise_forward,
)

#: The comparison can be read only when the *thinner* arm clears the floor.
#: One arm at 200 trades and the other at 3 is not a comparison, it is a book
#: next to an anecdote.
VERDICT_INSUFFICIENT = "INSUFFICIENT EVIDENCE"
VERDICT_EARLY = "EARLY EVIDENCE"
VERDICT_READY = "COMPARISON READY"

#: Cap on reported definition differences. A version diff is a screen, not a
#: merge tool; past this many changes the answer is "substantially rewritten",
#: stated once, rather than fifty rows nobody reads.
MAX_DIFF_ENTRIES = 50


def comparison_verdict(n_champion: int, n_challenger: int) -> tuple[str, list[str]]:
    """Readiness of the comparison, from the thinner arm's forward count."""
    thinner = min(int(n_champion), int(n_challenger))
    thicker = max(int(n_champion), int(n_challenger))
    if thinner < stats.MIN_SAMPLE:
        return (
            VERDICT_INSUFFICIENT,
            [
                f"the thinner arm holds {thinner} forward trades, below the "
                f"{stats.MIN_SAMPLE}-trade minimum; no side-by-side reading is supported"
            ],
        )
    if thinner < ANALYSIS_READY_N:
        return (
            VERDICT_EARLY,
            [
                f"both arms clear the {stats.MIN_SAMPLE}-trade minimum "
                f"({int(n_champion)} vs {int(n_challenger)}), but the thinner arm "
                f"is below the {ANALYSIS_READY_N}-trade comparison floor; "
                "treat every delta as provisional"
            ],
        )
    return (
        VERDICT_READY,
        [
            f"both arms clear the {ANALYSIS_READY_N}-trade comparison floor "
            f"({int(n_champion)} vs {thinner} at minimum, {thicker} at maximum); "
            "deltas remain descriptive — this verdict licenses reading, not promoting"
        ],
    )


def compare_arms(
    champion_rows: list[dict[str, Any]],
    challenger_rows: list[dict[str, Any]],
    *,
    metric: str = "return_pct",
) -> dict[str, Any]:
    """Side-by-side arm summaries with descriptive deltas.

    ``rows`` are closed, forward, deduplicated rows per arm — the caller
    selects them; this function never filters by grade, so it cannot
    accidentally admit an in-sample row. Deltas are challenger-minus-champion
    on means, each beside the two sample sizes that produced it.
    """
    champion = summarise_forward(champion_rows, metric=metric)
    challenger = summarise_forward(challenger_rows, metric=metric)
    verdict, reasons = comparison_verdict(champion["n"], challenger["n"])

    def _delta(key: str) -> float | None:
        base = (champion["stats"] or {}).get(key)
        other = (challenger["stats"] or {}).get(key)
        if base is None or other is None:
            return None
        try:
            return round(float(other) - float(base), 4)
        except (TypeError, ValueError):
            return None

    return {
        "metric": metric,
        "champion": {
            **champion,
            "evidence_status": evidence_status(champion["n"]),
            "next_gate": {
                "required": next_gate(champion["n"]),
                "have": champion["n"],
                "status": evidence_status(champion["n"]),
            },
        },
        "challenger": {
            **challenger,
            "evidence_status": evidence_status(challenger["n"]),
            "next_gate": {
                "required": next_gate(challenger["n"]),
                "have": challenger["n"],
                "status": evidence_status(challenger["n"]),
            },
        },
        "deltas": {
            "mean": _delta("mean"),
            "median": _delta("median"),
            "win_rate": _delta("win_rate"),
            "profit_factor": _delta("profit_factor"),
        },
        "verdict": verdict,
        "verdict_reasons": reasons,
    }


def definition_diff(
    champion_definition: Any, challenger_definition: Any
) -> dict[str, Any]:
    """The exact parameter differences between two strategy definitions.

    A recursive walk over parsed definitions (nested dicts of rules and
    parameters). Lists are compared element-wise by index; anything else that
    differs is reported whole. Values are carried as parsed — this function
    formats nothing, so a screen cannot misrender a number it never touched.
    """
    entries: list[dict[str, Any]] = []
    truncated = False

    def _walk(path: str, old: Any, new: Any) -> None:
        nonlocal truncated
        if truncated:
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                _walk(f"{path}.{key}" if path else str(key), old.get(key), new.get(key))
            return
        if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
            for index, (old_item, new_item) in enumerate(zip(old, new, strict=True)):
                _walk(f"{path}[{index}]", old_item, new_item)
            return
        if old != new:
            if len(entries) >= MAX_DIFF_ENTRIES:
                truncated = True
                return
            entries.append(
                {"parameter": path or "<root>", "champion": old, "challenger": new}
            )

    _walk("", champion_definition, challenger_definition)
    if truncated:
        entries.append(
            {
                "parameter": "<further differences>",
                "champion": f"more than {MAX_DIFF_ENTRIES} changed paths",
                "challenger": "see the full definitions",
            }
        )
    return {"changes": entries, "identical": not entries}


def version_timeline(
    versions: list[dict[str, Any]],
    *,
    champion_version: int | None,
    challenger_version: int | None,
    forward_counts: dict[Any, int] | None = None,
) -> list[dict[str, Any]]:
    """Every version in order, with its role and its forward count.

    Roles are assigned by the caller — this function never infers which
    version *should* be champion. A version with no forward row reports zero,
    not absence: "V3 has no evidence yet" is the finding.
    """
    counts = forward_counts or {}
    ordered = sorted(versions, key=lambda v: int(v.get("version") or 0))
    timeline = []
    for row in ordered:
        version = int(row.get("version") or 0)
        if version == champion_version:
            role = "CHAMPION"
        elif version == challenger_version:
            role = "CHALLENGER"
        else:
            role = None
        timeline.append(
            {
                "version": version,
                "role": role,
                "forward_observations": int(counts.get(version, 0)),
                "created_at": row.get("created_at"),
            }
        )
    return timeline


__all__ = [
    "MAX_DIFF_ENTRIES",
    "VERDICT_EARLY",
    "VERDICT_INSUFFICIENT",
    "VERDICT_READY",
    "compare_arms",
    "comparison_verdict",
    "definition_diff",
    "version_timeline",
]
