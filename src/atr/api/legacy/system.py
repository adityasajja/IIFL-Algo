"""Health, audit log and data-status endpoints."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from atr.config.settings import get_settings

from atr.api.legacy.common import _AUDIT_PATH, _read_audit
from atr.api.legacy.risk import _risk_state

logger = logging.getLogger("atr.api")

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    env: str
    database: bool
    session_active: bool
    # The environment banner reads these. They live on /health rather than a
    # separate call so the banner can never disagree with the rest of the
    # header, and so it refreshes on the existing poll.
    execution_mode: str = "paper"
    kill_switch: bool = False


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    database = _db_health()
    session_active = False
    try:
        from atr.brokers.iifl.auth import SessionStore

        session_active = SessionStore(settings.iifl_session_cache).load() is not None
    except Exception as exc:
        logger.warning("session check failed: %s", exc)
        session_active = False

    return HealthResponse(
        status="ok",
        env=settings.env,
        database=database,
        session_active=session_active,
        execution_mode=str(_risk_state().get("execution_mode", "paper")),
        kill_switch=bool(_risk_state().get("kill_switch")),
    )


_DB_CHECK: dict[str, Any] = {"at": 0.0, "ok": False, "probing": False}


def _db_health(ttl: float = 60.0) -> bool:
    """App-database check that never blocks the request thread.

    The probe itself is slow when no database is running: ``localhost``
    resolves to both ::1 and 127.0.0.1, so a dead Postgres burns the full
    connect timeout twice (~4s). The dashboard polls ``/health`` every 15s, so
    doing that inline made the whole UI feel broken. The check now runs on a
    daemon thread and callers get the last known answer immediately.
    """
    import threading
    import time as _time

    now = _time.monotonic()
    if now - float(_DB_CHECK["at"]) >= ttl and not _DB_CHECK["probing"]:
        _DB_CHECK["probing"] = True

        def probe() -> None:
            ok = False
            try:
                from atr.appdb.engine import get_app_db

                ok = get_app_db().health()
            except Exception:  # noqa: BLE001 - DB is optional
                ok = False
            _DB_CHECK["ok"] = ok
            _DB_CHECK["at"] = _time.monotonic()
            _DB_CHECK["probing"] = False

        threading.Thread(target=probe, daemon=True, name="atr-db-probe").start()

    return bool(_DB_CHECK["ok"])


@router.get("/audit")
def get_audit(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    """The immutable trail, newest first."""
    return {"entries": _read_audit(limit), "path": str(_AUDIT_PATH)}


# ----------------------------------------------------------------------
# Local caches — what the engine has to work with offline
# ----------------------------------------------------------------------
@router.get("/history/status")
def history_status() -> dict[str, Any]:
    """Coverage of the local daily cache. No broker session required."""
    from atr.data.history import CACHE_ROOT

    exchanges = []
    if CACHE_ROOT.is_dir():
        for folder in sorted(p for p in CACHE_ROOT.iterdir() if p.is_dir()):
            files = list(folder.glob("*.parquet"))
            if not files:
                continue
            stats = [f.stat() for f in files]
            exchanges.append(
                {
                    "exchange": folder.name,
                    "symbols": len(files),
                    "size_mb": round(sum(s.st_size for s in stats) / 1e6, 1),
                    "last_sync": datetime.fromtimestamp(
                        max(s.st_mtime for s in stats)
                    ).isoformat(),
                }
            )
    return {
        "cache_root": str(CACHE_ROOT),
        "exchanges": exchanges,
        "total_symbols": sum(e["symbols"] for e in exchanges),
    }


@router.get("/instruments/status")
def instruments_status() -> dict[str, Any]:
    """Cached contract-file coverage per segment. No broker session required."""
    from atr.brokers.iifl.contracts import CACHE_DIR

    segments = []
    if CACHE_DIR.is_dir():
        for path in sorted(CACHE_DIR.glob("*.json")):
            stat = path.stat()
            count: int | None = None
            try:
                import json

                with path.open(encoding="utf-8") as fh:
                    payload = json.load(fh)
                if isinstance(payload, list):
                    count = len(payload)
                elif isinstance(payload, dict):
                    for key in ("result", "data", "contracts"):
                        if isinstance(payload.get(key), list):
                            count = len(payload[key])
                            break
            except Exception:  # noqa: BLE001 — size alone is still informative
                count = None
            segments.append(
                {
                    "segment": path.stem,
                    "contracts": count,
                    "size_mb": round(stat.st_size / 1e6, 2),
                    "synced_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
    return {
        "cache_dir": str(CACHE_DIR),
        "segments": segments,
        "total_contracts": sum(s["contracts"] or 0 for s in segments),
    }
