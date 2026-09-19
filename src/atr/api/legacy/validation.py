"""Stored validation and evidence reports."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter


logger = logging.getLogger("atr.api")

router = APIRouter()


_PAPER_VALIDATION_PATH = Path("data/self_learning/paper_validation.json")


@router.get("/validation")
def validation_report() -> dict[str, Any]:
    """The last out-of-sample run over the academic paper strategies.

    Serves ``scripts/validate_paper_strategies.py`` output so the dashboard can
    show measured numbers instead of the constants that used to be hardcoded in
    ``atr.research.papers``. Includes the random-selection control: a strategy
    that does not clear it has demonstrated nothing.
    """
    if not _PAPER_VALIDATION_PATH.exists():
        return {
            "available": False,
            "hint": "run: .venv/Scripts/python.exe scripts/validate_paper_strategies.py",
        }
    try:
        payload = json.loads(_PAPER_VALIDATION_PATH.read_text(encoding="utf8"))
    except (OSError, ValueError) as exc:
        return {"available": False, "error": str(exc)}

    payload["available"] = True
    return payload


_EPISODIC_VALIDATION_PATH = Path("data/self_learning/episodic_pivot_validation.json")
_EPISODIC_PREMISE_PATH = Path("data/self_learning/episodic_pivot_premise.json")


@router.get("/validation/episodic-pivot")
def episodic_pivot_report() -> dict[str, Any]:
    """Pradeep Bonde's Episodic Pivot, measured rather than quoted.

    Serves two scripts together because either alone invites the wrong
    conclusion. ``episodic_pivot_premise.json`` measures whether the playbook's
    raw material — routine 20–40% gaps and 100–300% repricings — exists on the
    universe at all; ``episodic_pivot_validation.json`` scores the rules out of
    sample. A "no trades" verdict means something entirely different depending
    on which of the two came up empty, so the panel shows both.
    """
    if not _EPISODIC_VALIDATION_PATH.exists():
        return {
            "available": False,
            "hint": (
                "run: .venv/Scripts/python.exe scripts/research_episodic_pivot.py "
                "&& .venv/Scripts/python.exe scripts/validate_episodic_pivot.py"
            ),
        }
    try:
        payload = json.loads(_EPISODIC_VALIDATION_PATH.read_text(encoding="utf8"))
    except (OSError, ValueError) as exc:
        return {"available": False, "error": str(exc)}

    if _EPISODIC_PREMISE_PATH.exists():
        try:
            payload["premise"] = json.loads(
                _EPISODIC_PREMISE_PATH.read_text(encoding="utf8")
            )
        except (OSError, ValueError):
            pass

    payload["available"] = True
    return payload


_ALPHA_HUNT_PATHS = {
    "nifty50": Path("data/self_learning/alpha_hunt_nifty50.json"),
    "midcap150": Path("data/self_learning/alpha_hunt_midcap150.json"),
    "smallcap250": Path("data/self_learning/alpha_hunt_smallcap250.json"),
}


@router.get("/validation/alpha-hunt")
def alpha_hunt_report() -> dict[str, Any]:
    """Pre-registered candidate edges, each against a null that removes only its signal.

    One JSON per universe because the runs are long enough to want running in
    parallel; merged here so the dashboard reads one payload. The
    ``pre_registered`` block is carried through deliberately — a candidate list
    fixed *after* seeing results is not a test, and the reader should be able to
    see that the priors and grids were written down first.
    """
    present = {u: p for u, p in _ALPHA_HUNT_PATHS.items() if p.exists()}
    if not present:
        return {
            "available": False,
            "hint": "run: .venv/Scripts/python.exe scripts/research_alpha_hunt.py",
        }

    merged: dict[str, Any] = {"available": True, "universes": {}, "pre_registered": {},
                              "config": {}, "generated_at": None}
    for _universe, path in present.items():
        try:
            payload = json.loads(path.read_text(encoding="utf8"))
        except (OSError, ValueError) as exc:
            merged.setdefault("errors", {})[str(path)] = str(exc)
            continue
        merged["universes"].update(payload.get("universes", {}))
        merged["pre_registered"].update(payload.get("pre_registered", {}))
        merged["config"].update(payload.get("config", {}))
        generated = payload.get("generated_at")
        if generated and (merged["generated_at"] is None or generated > merged["generated_at"]):
            merged["generated_at"] = generated
    return merged


#: Findings from the factor research programme, written by
#: ``scripts/research_honest_verdicts.py``. Each entry is an ``Evidence`` record
#: rendered with its credibility verdict attached.
_EVIDENCE_PATH = Path("data/signals/evidence.json")


@router.get("/evidence")
def evidence_report() -> dict[str, Any]:
    """Measured findings, each with the credibility verdict that reads it.

    This endpoint exists because a backtest number on its own is not evidence.
    The results served here were each individually correct and collectively
    misleading when first produced: a +480% index that was really +124% once
    universe selection was removed, and a "factor" that was beta in disguise.

    Nothing here should be presented to a user as an opportunity without the
    verdict travelling with it, so the payload keeps them in one record.
    """
    if not _EVIDENCE_PATH.exists():
        return {
            "available": False,
            "hint": (
                "run: .venv/Scripts/python.exe scripts/research_honest_verdicts.py"
            ),
        }
    try:
        payload = json.loads(_EVIDENCE_PATH.read_text(encoding="utf8"))
    except (OSError, ValueError) as exc:
        return {"available": False, "error": str(exc)}

    findings = payload.get("findings", [])
    payload["available"] = True
    payload["credible_count"] = sum(
        1 for f in findings if f.get("verdict", {}).get("credible")
    )
    payload["total"] = len(findings)
    return payload
