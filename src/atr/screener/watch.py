"""Watched scans: re-run a saved screen on a schedule, message on new matches.

A saved scan becomes a *watch* when its definition carries a ``watch`` block::

    "watch": {"enabled": true, "every_minutes": 15,
              "seen": ["TCS", ...], "last_run": 1.7e9, "last_error": null}

The state lives inside the definition JSON, so no schema migration is needed and
a watch travels with the scan it belongs to.

"New" means *in this run's matches but not in the previous run's*. ``seen`` is
replaced by the current match set each run, so a stock that drops out and later
returns is announced again — that is a fresh signal, not a repeat. The first run
after enabling only records the baseline; announcing every stock that already
matches would bury the one that matters.

The scan reads cached daily bars, so a short interval only finds something new
once those bars change (the end-of-day refresh, or a history sync).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("atr.screener.watch")

MIN_EVERY_MINUTES = 1
MAX_EVERY_MINUTES = 24 * 60
DEFAULT_EVERY_MINUTES = 15
#: Telegram caps a message at 4096 characters; one row is well under 100.
MAX_LINES = 25


def _watch_of(definition: dict[str, Any]) -> dict[str, Any]:
    watch = definition.get("watch")
    return watch if isinstance(watch, dict) else {}


def configure(service: Any, user_id: str, scan_id: str, *, enabled: bool, every_minutes: int | None = None) -> dict[str, Any]:
    """Turn a saved scan's watch on or off. Enabling resets the baseline."""
    from atr.screener.service import ScreenerError

    saved = service.get_saved(user_id, scan_id)
    definition = dict(saved["definition"])
    old = _watch_of(definition)
    minutes = every_minutes if every_minutes is not None else old.get("every_minutes", DEFAULT_EVERY_MINUTES)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise ScreenerError("every_minutes must be a whole number", code="bad_interval") from None
    if not MIN_EVERY_MINUTES <= minutes <= MAX_EVERY_MINUTES:
        raise ScreenerError(
            f"every_minutes must be between {MIN_EVERY_MINUTES} and {MAX_EVERY_MINUTES}",
            code="bad_interval",
        )
    watch = {"enabled": bool(enabled), "every_minutes": minutes, "last_error": None}
    if enabled and not old.get("enabled"):
        watch.update(seen=None, last_run=None)  # fresh baseline on the next tick
    else:
        watch.update(seen=old.get("seen"), last_run=old.get("last_run"))
    definition["watch"] = watch
    return service.update_saved(user_id, scan_id, definition=definition)


def _save_state(service: Any, scan: dict[str, Any], watch: dict[str, Any]) -> None:
    from atr.appdb.repositories import ScreenerRepository

    definition = dict(scan["definition"])
    definition["watch"] = watch
    with service.db.session() as session:
        ScreenerRepository.update(session, scan["scan_id"], scan["user_id"], definition=definition)


def _format(scan_name: str, new_rows: list[dict[str, Any]], result: dict[str, Any]) -> tuple[str, str]:
    title = f"Screener: {scan_name} - {len(new_rows)} new"
    lines = []
    for row in new_rows[:MAX_LINES]:
        parts = [str(row["symbol"])]
        if row.get("ltp") is not None:
            parts.append(f"{row['ltp']:.2f}")
        if row.get("change_pct") is not None:
            parts.append(f"{row['change_pct']:+.2f}%")
        if row.get("rel_volume") is not None:
            parts.append(f"vol x{row['rel_volume']:.1f}")
        lines.append("  ".join(parts))
    if len(new_rows) > MAX_LINES:
        lines.append(f"...and {len(new_rows) - MAX_LINES} more")
    lines.append(f"{result['matched']} match now - data as of {result.get('as_of')}")
    return title, "\n".join(lines)


def _send(title: str, body: str) -> bool:
    from atr.alerts.channels import TelegramChannel
    from atr.config.settings import get_settings

    s = get_settings()
    channel = TelegramChannel(getattr(s, "telegram_bot_token", ""), getattr(s, "telegram_chat_id", ""))
    if not channel.configured:
        logger.warning("watched scan has new matches but Telegram is not configured")
        return False
    return channel.send(title, body)


def run_one(service: Any, scan: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Run one watched scan, message the new matches, persist the state.

    Returns ``{"matched", "new", "notified", "baseline"}``.
    """
    now = time.time() if now is None else now
    watch = dict(_watch_of(scan["definition"]))
    try:
        result = service.run_saved(scan["user_id"], scan["scan_id"], limit=0)
    except Exception as exc:  # noqa: BLE001 - one broken scan must not stop the others
        logger.warning("watched scan %s failed: %s", scan.get("name"), exc)
        watch.update(last_run=now, last_error=str(exc)[:200])
        _save_state(service, scan, watch)
        return {"matched": 0, "new": 0, "notified": False, "baseline": False, "error": str(exc)[:200]}

    rows = result["rows"]
    current = [r["symbol"] for r in rows]
    previous = watch.get("seen")
    baseline = previous is None
    new_rows = [] if baseline else [r for r in rows if r["symbol"] not in set(previous)]

    notified = False
    if baseline:
        _send(
            f"Screener: {scan['name']} - watching",
            f"{len(current)} stocks match right now. You will be told when a new one appears.",
        )
    elif new_rows:
        notified = _send(*_format(scan["name"], new_rows, result))

    # A failed send keeps the new names out of ``seen`` so the next run retries
    # them instead of losing the alert.
    if new_rows and not notified:
        retained = set(current) - {r["symbol"] for r in new_rows}
        watch["seen"] = sorted(retained)
    else:
        watch["seen"] = sorted(current)
    watch.update(last_run=now, last_error=None)
    _save_state(service, scan, watch)
    return {"matched": len(current), "new": len(new_rows), "notified": notified, "baseline": baseline}


def run_due(service: Any, *, now: float | None = None) -> int:
    """Run every enabled watch whose interval has elapsed. Returns how many ran."""
    from atr.appdb.repositories import ScreenerRepository
    from atr.screener.service import _scan_out

    now = time.time() if now is None else now
    with service.db.session() as session:
        rows = ScreenerRepository.list_all(session)
    ran = 0
    for row in rows:
        scan = _scan_out(row)
        scan["user_id"] = row["user_id"]
        watch = _watch_of(scan["definition"])
        if not watch.get("enabled"):
            continue
        last = watch.get("last_run")
        if last is not None and now - float(last) < int(watch.get("every_minutes", DEFAULT_EVERY_MINUTES)) * 60:
            continue
        run_one(service, scan, now=now)
        ran += 1
    return ran
