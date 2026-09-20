"""A job that runs once a day, with its outcome remembered on disk.

Each job records the date it last ran and whether that worked, so a restart neither
repeats a finished run nor skips one, and the System page can show what failed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class DailyJob:
    name: str
    run: Callable[[], Any]
    at: tuple[int, int] = (2, 0)  # local hour and minute after which it is due
    weekdays_only: bool = False
    #: A job that fails is tried again this many times, this many minutes apart, the same day.
    #: For jobs that depend on something outside the app, like a broker login being live.
    retries: int = 0
    retry_minutes: int = 30


class JobStore:
    """One small JSON file per job under ``<data_root>/jobs``."""

    def __init__(self, data_root: Path) -> None:
        self._dir = Path(data_root) / "jobs"

    def read(self, name: str) -> dict[str, Any]:
        try:
            return json.loads((self._dir / f"{name}.json").read_text(encoding="utf8"))
        except (OSError, ValueError):
            return {}

    def write(self, name: str, state: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{name}.json").write_text(json.dumps(state), encoding="utf8")


def is_due(job: DailyJob, state: dict[str, Any], now: datetime) -> bool:
    if job.weekdays_only and now.weekday() >= 5:
        return False
    if state.get("ran_on") == now.date().isoformat():
        if state.get("ok") is False and state.get("attempts", 1) <= job.retries:
            return now - datetime.fromisoformat(state["at"]) >= timedelta(minutes=job.retry_minutes)
        return False
    return (now.hour, now.minute) >= job.at


def run_if_due(job: DailyJob, store: JobStore, now: datetime | None = None) -> bool:
    """Run the job if it is due. A failure is recorded and does not raise. Returns whether it ran."""
    now = now or datetime.now()
    if not is_due(job, store.read(job.name), now):
        return False
    before = store.read(job.name)
    attempts = before.get("attempts", 0) + 1 if before.get("ran_on") == now.date().isoformat() else 1
    state: dict[str, Any] = {"ran_on": now.date().isoformat(), "at": now.isoformat(timespec="seconds"), "attempts": attempts}
    try:
        job.run()
        state["ok"] = True
    except Exception as exc:  # noqa: BLE001 - one failing job must not stop the others
        logger.exception("job {} failed", job.name)
        state.update(ok=False, error=str(exc)[:300])
    store.write(job.name, state)
    return True


async def run_forever(jobs: list[DailyJob], store: JobStore, *, every: float = 300.0) -> None:
    """Check the jobs every few minutes, running each in a worker thread."""
    while True:
        for job in jobs:
            await asyncio.to_thread(run_if_due, job, store)
        await asyncio.sleep(every)
