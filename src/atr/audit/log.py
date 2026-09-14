"""Audit writer — one implementation, two sinks.

Rules that hold here:

* **Never raise.** An audit write that fails must not fail the action it was
  recording, or a disk-full condition becomes an outage. Failures are logged and
  swallowed; the DB write is attempted independently of the file write so one
  sink being down does not lose the other.
* **Never update.** The file is opened in append mode and the table has no update
  path. An editable audit trail is not an audit trail.
* **Record the actor's identity, not just their id.** ``actor`` is the email at
  the time of the action, denormalised, so deleting a user does not turn history
  into a list of orphaned ids.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger("atr.audit")

ROOT = Path(__file__).resolve().parents[3]
AUDIT_PATH = ROOT / "data" / "audit" / "audit.jsonl"


def _iso(ts: datetime | None = None) -> str:
    moment = ts or datetime.now(UTC).replace(tzinfo=None)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.isoformat(timespec="seconds") + "Z"


def append_file(record_row: dict[str, Any]) -> None:
    """Append one JSON object as a line. Best effort; never raises."""
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_row, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 - auditing must not break the action
        logger.warning("audit file append failed: %s", exc)


def read_file(limit: int = 200) -> list[dict[str, Any]]:
    """Newest-first slice of the file sink."""
    if not AUDIT_PATH.exists():
        return []
    try:
        lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit file read failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines[-max(1, limit * 2) :]):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            # A torn final line from a crash mid-write. Skip it; do not abort
            # the whole read, and do not pretend it parsed.
            continue
        if len(out) >= limit:
            break
    return out


def record(
    *,
    action: str,
    actor: str | None = None,
    subject: str | None = None,
    detail: Any = None,
    result: str = "success",
    user_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    strategy_id: str | None = None,
    strategy_version: int | None = None,
    request_id: str | None = None,
    ip: str | None = None,
    ts: datetime | None = None,
    session: Session | None = None,
) -> str:
    """Write an audit event to the file sink, and to the table when given a session.

    Returns the file-sink event id (or an empty string if even that failed).
    """
    moment = ts or datetime.now(UTC).replace(tzinfo=None)
    payload = {
        "ts": _iso(moment),
        "actor": actor or "system",
        "action": action,
        "subject": subject or "",
        "detail": detail if isinstance(detail, str) else json.dumps(detail, default=str),
        # Additive keys — `GET /audit` reads this file and ignores what it does
        # not know about, so adding fields cannot break it.
        "result": result,
        "user_id": user_id,
        "target_type": target_type,
        "target_id": target_id,
        "request_id": request_id,
        "ip": ip,
    }
    append_file(payload)

    if session is not None:
        try:
            from atr.appdb.repositories import AuditRepository

            return AuditRepository.append(
                session,
                action=action,
                result=result,
                user_id=user_id,
                actor=actor,
                target_type=target_type,
                target_id=target_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                request_id=request_id,
                detail=detail,
                ip=ip,
                ts=moment,
            )
        except Exception as exc:  # noqa: BLE001 - the file sink already has it
            logger.warning("audit table append failed: %s", exc)
    return ""


__all__ = ["AUDIT_PATH", "append_file", "read_file", "record"]
