"""Reconciliation routes — ``/api/v1/reconciliation``.

The dashboard surface for the control that compares the platform's records against
the broker's. Two things about the shape are deliberate:

* **A critical mismatch is visible without anybody asking.** ``GET /status`` returns
  the most recent run that was not clean, and ``clear: true`` once a clean run
  supersedes it — so a banner clears when the problem is fixed rather than waiting
  to be dismissed.
* **A scope that could not be compared is reported, not skipped.** ``incomparable``
  names it and says why. A run that reported ``ok`` while quietly skipping half its
  scopes is the failure mode this whole control exists to prevent.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.services.broker_access import BrokerUnavailable, read_broker
from atr.services.reconcile import (
    SCOPES,
    SEVERITY_CRITICAL,
    ReconciliationService,
)

logger = logging.getLogger("atr.api.reconciliation")

router = APIRouter(prefix="/api/v1/reconciliation", tags=["reconciliation"])

_READ = require_permission(Permission.RISK_READ)
_RUN = require_permission(Permission.RISK_CONFIGURE)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A subset of ``orders``/``positions``/``holdings``/``funds``. Omit for all.
    scopes: list[str] | None = None
    #: Whether to push a notification when the run is critical. On by default —
    #: a critical mismatch that nobody hears about is the failure this prevents.
    notify: bool = True


def _service() -> ReconciliationService:
    return ReconciliationService()


def _notify(report: Any) -> None:
    """Push a critical run to the configured channels.

    Wrapped so a failed notification cannot break the check, and only sent for a
    critical run: warning-level noise is how an alarm channel stops being read.
    """
    if report.severity != SEVERITY_CRITICAL:
        return
    try:
        from atr.alerts.channels import channels_from_settings
        from atr.config.settings import get_settings

        lines = [
            f"severity: {report.severity}",
            f"scope: {report.scope}",
            f"mismatches: {report.mismatch_count} ({len(report.critical)} critical)",
        ]
        for difference in report.critical[:10]:
            lines.append(f"- {difference.scope}/{difference.key}: {difference.detail}")
        if report.incomparable:
            lines.append(
                "not compared: "
                + ", ".join(e["scope"] for e in report.incomparable)
            )
        body = "\n".join(lines)
        for channel in channels_from_settings(get_settings()):
            try:
                if channel.send("ATR reconciliation: critical mismatch", body):
                    break
            except Exception:  # noqa: BLE001 - one dead channel must not stop the others
                logger.exception("a notification channel failed")
    except Exception:  # noqa: BLE001
        logger.exception("could not send the reconciliation notification")


# --------------------------------------------------------------------- routes
@router.get("/status", dependencies=[Depends(_READ)])
def reconciliation_status(principal: Principal = Depends(_READ)) -> dict[str, Any]:
    """The banner source: the most recent run that was not clean.

    ``clear`` is true when the latest run is clean, so the banner goes away when
    the problem is fixed.
    """
    return _service().status(principal.user_id)


@router.get("/scopes", dependencies=[Depends(_READ)])
def reconciliation_scopes() -> dict[str, Any]:
    """Which scopes can be run. Served so the UI cannot request an unknown one."""
    return {"scopes": list(SCOPES)}


@router.get("/runs", dependencies=[Depends(_READ)])
def reconciliation_runs(
    principal: Principal = Depends(_READ),
    severity: str | None = Query(default=None, pattern="^(ok|warning|critical)$"),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    rows, total = _service().history(principal.user_id, severity=severity, limit=limit)
    return {"runs": rows, "total": total}


@router.get("/runs/latest", dependencies=[Depends(_READ)])
def reconciliation_latest(
    principal: Principal = Depends(_READ), scope: str | None = None
) -> dict[str, Any]:
    row = _service().latest(principal.user_id, scope=scope)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"detail": "no reconciliation run yet", "code": "not_found"},
        )
    return row


@router.post("/run")
def run_reconciliation(
    body: RunRequest, principal: Principal = Depends(_RUN)
) -> dict[str, Any]:
    """Compare the platform's records against the broker's, now.

    Requires ``risk:configure`` rather than ``risk:read`` because it writes a run
    row and an audit row. It changes nothing at the broker — every call it makes is
    a read — but a permission that lets a read-only role write rows is the wrong
    shape.

    A failure to reach the broker is **not** an error response. The run is recorded
    with a severity that is not ``ok`` and an ``error`` naming the cause, because
    reconciliation that throws is reconciliation that stops running — and this is
    the control that has to keep running.
    """
    scopes = body.scopes
    if scopes is not None:
        unknown = sorted(set(scopes) - set(SCOPES))
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "detail": f"unknown scope(s): {', '.join(unknown)}",
                    "code": "unknown_scope",
                    "valid": list(SCOPES),
                },
            )

    try:
        broker = read_broker()
    except BrokerUnavailable as exc:
        # No session, or live access is off. Recorded as a run that could not
        # check anything rather than returned as an error, so the dashboard shows
        # "reconciliation is not running" instead of a blank screen.
        report = _service().run(
            principal.user_id,
            broker=_UnreachableBroker(str(exc)),
            scopes=scopes,
            persist=True,
            actor=principal.username,
        )
        report.error = str(exc)
        return report.as_dict()

    report = _service().run(
        principal.user_id,
        broker=broker,
        scopes=scopes,
        persist=True,
        actor=principal.username,
        notifier=_notify if body.notify else None,
    )
    return report.as_dict()


class _UnreachableBroker:
    """A broker stand-in whose every read fails, so the run records why.

    Not a silent empty result: an empty book would look exactly like a clean one,
    and "the broker was unreachable" would then be indistinguishable from "there is
    nothing to reconcile".
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def _fail(self) -> None:
        raise ConnectionError(self._reason)

    def open_orders(self) -> list[Any]:
        self._fail()
        return []

    def positions(self) -> list[Any]:
        self._fail()
        return []

    def holdings(self) -> list[Any]:
        self._fail()
        return []

    def funds(self) -> Any:
        self._fail()
        return None
