"""Readiness gates that decide, not just describe.

:mod:`atr.research.learning_readiness` deliberately stops at research labels —
it will never pause, cap, or refuse anything. Something has to act on those
labels, or "reliable in real decisions" is a slogan. This module is that
something, and it is deliberately narrow:

* PAPER deployments are always allowed. Evidence must come from somewhere;
  refusing first runs would make learning impossible by construction.
* LIVE deployments require fifty *effective* forward trades
  (``ANALYSIS_READY_N``), counted with :func:`effective_n` so one crowded
  minute cannot clear the gate. A refusal states the current effective
  sample, the cluster count, and the next gate.
* Dry-run is the default. :func:`evaluate_gate` decides without touching
  anything; only an explicit enforcement call acts.

Effective sample, not raw rows: fourteen fills in one minute banked zero
findings under Phase 2 for exactly this reason, and the gate must not be
more gullible than the findings loop.
"""

from __future__ import annotations

from typing import Any

from atr.research.learning_evidence import (
    CLASS_LIVE_FORWARD,
    CLASS_PAPER_FORWARD,
)
from atr.research.learning_readiness import ANALYSIS_READY_N, next_gate
from atr.research.learning_stats import MIN_SAMPLE, effective_n


def _forward_rows(
    strategy_id: str, version: int | None, *, user_id: str | None, db: Any
) -> list[dict[str, Any]]:
    """Closed forward journal rows for one strategy version."""
    from atr.appdb.schema import trade_journal

    from sqlalchemy import select

    with db.session() as session:
        stmt = select(trade_journal).order_by(trade_journal.c.entry_ts)
        rows = [dict(r) for r in session.execute(stmt).mappings()]
    out = []
    for row in rows:
        if user_id is not None and row.get("user_id") != user_id:
            continue
        if str(row.get("strategy_id") or "") != strategy_id:
            continue
        if version is not None and row.get("strategy_version") != version:
            try:
                if row.get("strategy_version") is not None and int(row.get("strategy_version")) != int(version):
                    continue
            except (TypeError, ValueError):
                continue
        if str(row.get("evidence_class") or "").upper() not in (
            CLASS_PAPER_FORWARD,
            CLASS_LIVE_FORWARD,
        ) and str(row.get("evidence_grade") or "").lower() != "forward":
            continue
        out.append(row)
    return out


def evaluate_gate(
    strategy_id: str,
    version: int | None = None,
    *,
    user_id: str | None = None,
    db: Any = None,
) -> dict[str, Any]:
    """Decide, without touching anything, whether a strategy may escalate.

    Returns ``action`` ``"allow"`` or ``"hold"`` with the numbers behind it.
    No rows is not a failure — a strategy that never traded has nothing to
    judge, and judging it would block every first run.
    """
    if db is None:
        from atr.appdb.engine import get_app_db

        db = get_app_db()
    rows = _forward_rows(strategy_id, version, user_id=user_id, db=db)
    stamps = [row.get("entry_ts") for row in rows]
    n_eff, clusters = effective_n(stamps)
    if not rows:
        return {
            "strategy_id": strategy_id,
            "version": version,
            "action": "allow",
            "state": "UNEVALUATED",
            "n": 0,
            "n_effective": 0.0,
            "clusters": 0,
            "reason": "no forward trades yet; there is nothing to judge",
        }
    if n_eff >= ANALYSIS_READY_N:
        return {
            "strategy_id": strategy_id,
            "version": version,
            "action": "allow",
            "state": "ANALYSIS READY",
            "n": len(rows),
            "n_effective": round(n_eff, 2),
            "clusters": clusters,
            "reason": (
                f"{n_eff:.1f} effective forward trades clear the "
                f"{ANALYSIS_READY_N}-trade floor"
            ),
        }
    nxt = next_gate(int(n_eff))
    return {
        "strategy_id": strategy_id,
        "version": version,
        "action": "hold",
        "state": "MINIMUM SAMPLE" if n_eff >= MIN_SAMPLE else "NOT READY",
        "n": len(rows),
        "n_effective": round(n_eff, 2),
        "clusters": clusters,
        "next_gate": nxt,
        "reason": (
            f"{len(rows)} forward trades in {clusters} time "
            f"{'cluster' if clusters == 1 else 'clusters'}: effective sample "
            f"{n_eff:.1f} has not cleared the {ANALYSIS_READY_N}-trade floor"
            + (f" (next gate: {nxt})" if nxt else "")
        ),
    }


def check_live_deployment(
    strategy_id: str,
    version: int,
    *,
    user_id: str | None = None,
    db: Any = None,
) -> dict[str, Any]:
    """Enforce the gate on the money decision: creating a LIVE deployment.

    Returns the ``allow`` decision, or raises :class:`DeploymentError` with a
    reason the operator can act on. PAPER creation never reaches this function,
    so first runs are unaffected by construction.
    """
    from atr.services.paper import DeploymentError

    decision = evaluate_gate(strategy_id, version, user_id=user_id, db=db)
    if decision["action"] == "allow":
        return decision
    raise DeploymentError(
        f"strategy {strategy_id} v{version} holds at {decision['state']}: "
        f"{decision['reason']}. Accrue {ANALYSIS_READY_N} effective forward "
        "trades on paper first.",
        code="unproven_strategy",
        status=422,
    )
