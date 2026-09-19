"""Helpers shared by more than one route module."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import HTTPException


logger = logging.getLogger("atr.api")


# Append-only audit log. Every action that changes what the system will do to
# real money lands here, with who and why. Kept as a file rather than in-memory
# state so a restart cannot erase an inconvenient decision.
_AUDIT_PATH = Path("data/audit/audit.jsonl")


def _utcnow_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _append_audit(*, actor: str, action: str, subject: str, detail: str | None = None) -> dict[str, Any]:
    """Append one immutable record. Never rewrites or deletes existing lines."""
    entry = {
        "ts": _utcnow_iso(),
        "actor": actor,
        "action": action,
        "subject": subject,
        "detail": detail,
    }
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — logging must never break trading
        logger.warning("audit append failed: %s", exc)
    return entry


def _read_audit(limit: int = 200) -> list[dict[str, Any]]:
    if not _AUDIT_PATH.exists():
        return []
    limit = max(1, limit)  # `lines[-0:]` is the whole file, not zero lines
    try:
        with _AUDIT_PATH.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            # Read only the tail: the trail is append-only and grows forever, so
            # reading it all per request is O(history). ~1 KiB/entry is generous.
            fh.seek(max(0, size - limit * 1024 - 1024))
            chunk = fh.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines[-limit:]):
        with contextlib.suppress(json.JSONDecodeError):
            out.append(json.loads(line))
    return out


def _authed_client():
    """IIFL client with a restored session. Works in any env — market data
    and other read APIs don't need paper/live mode (only order placement
    goes through the `_live_broker` gate below).

    Delegates to `atr.services.broker_access.authed_client`, which is the one place
    that knows how to build and authenticate a client.
    """
    from atr.services.broker_access import BrokerUnavailable, authed_client

    try:
        return authed_client()
    except BrokerUnavailable as exc:
        raise HTTPException(exc.status, str(exc)) from exc


# ----------------------------------------------------------------------
# Serialisation helpers
# ----------------------------------------------------------------------
def _clean(value: Any) -> Any:
    """Make numpy/pandas output JSON-safe.

    ``NaN`` and ``inf`` are not valid JSON, and the metrics dataclasses emit
    both (profit factor with no losses is ``inf``, an untraded fold is ``nan``).
    Starlette would happily write the literal ``NaN`` token, which ``JSON.parse``
    rejects — so every payload crossing this boundary goes through here.
    """
    import math

    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if hasattr(value, "item"):  # numpy scalar (np.int64 is not an int)
        return _clean(value.item())
    return value


def _equity_points(series: pd.Series | None, max_points: int = 900) -> list[dict[str, Any]]:
    """Downsample an equity curve for the wire without changing its shape."""
    if series is None or len(series) == 0:
        return []
    stride = max(1, len(series) // max_points)
    return [
        {"ts": pd.Timestamp(ts).isoformat(), "value": float(v)}
        for ts, v in zip(series.index[::stride], series.to_numpy()[::stride], strict=True)
    ]


def _broker_rows(payload: Any) -> list[dict[str, Any]]:
    """IIFL wraps list responses in ``{"result": [...]}``; tolerate either.

    Reused from the CLI rather than reimplemented so the dashboard and the
    terminal cannot disagree about what a portfolio section contains.
    """
    from atr.cli import _rows

    return _rows(payload)


#: IIFL reports "there is nothing here" as an error too — e.g. EC926
#: "No Trade's are found for this user". Those are empty results, not failures.
_EMPTY_STATES = ("no trade", "no holding", "no position", "no order", "no record")

_EGRESS: dict[str, Any] = {"ip": None, "at": 0.0}


def _egress_ip(ttl: float = 600.0) -> str | None:
    """The public IPv4 this host egresses as, for diagnosing the IP whitelist.

    Fetched lazily and cached — it only runs once IIFL has already rejected us
    for an IP reason, so it costs nothing on the happy path.

    Uses a **dual-stack** endpoint with the same IPv4 pinning as `IiflClient`.
    An IPv4-only service would cheerfully report the IPv4 while the request
    itself left over IPv6, which is the exact confusion that hid this bug the
    first time round.
    """
    import time as _time

    now = _time.monotonic()
    if _EGRESS["ip"] and now - float(_EGRESS["at"]) < ttl:
        return _EGRESS["ip"]
    try:
        import httpx

        with httpx.Client(
            transport=httpx.HTTPTransport(local_address="0.0.0.0"), timeout=8
        ) as http:
            ip = http.get("https://api64.ipify.org").text.strip()
        if ip:
            _EGRESS["ip"] = ip
            _EGRESS["at"] = now
    except Exception:  # noqa: BLE001 - a diagnostic must never mask the real error
        pass
    return _EGRESS["ip"]


def _with_ip_hint(message: str) -> str:
    """Turn IIFL's opaque IP rejection into something you can act on.

    The whitelisted address is a SEBI requirement and the connection here is a
    dynamic consumer line, so this recurs whenever the ISP re-leases. Saying
    which address to register beats re-deriving it every time.
    """
    if "ip address not authorized" not in message.lower():
        return message
    ip = _egress_ip()
    if not ip:
        return message
    return (
        f"{message} This machine currently egresses as {ip} — register that "
        f"address at developers.iiflcapital.com (My Apps → View All Details → "
        f"Primary Static IP)."
    )


def _empty_state(node: Any) -> bool:
    """True for IIFL's 'nothing here' rows, which arrive carrying an error status."""
    if not isinstance(node, dict):
        return False
    message = str(node.get("message") or "").lower()
    return any(hint in message for hint in _EMPTY_STATES)


def _broker_error(payload: Any) -> str | None:
    """IIFL signals failure with a status field, it does not raise.

    The catch is that failures are nested. A rejected call comes back as::

        {"status": "Ok", "message": "Success",
         "result": [{"status": "EC500",
                     "message": "Error : IP address not authorized for trading."}]}

    The outer envelope reports Ok no matter what happened, so checking only the
    top level misses every real failure — which is exactly what happened here,
    and made a wall of failing calls look like a healthy book. Check both.
    """
    if not isinstance(payload, dict):
        return None

    def failure(node: Any) -> str | None:
        if not isinstance(node, dict):
            return None
        status = node.get("status")
        if isinstance(status, str) and status.strip().lower() not in ("ok", "success"):
            message = str(node.get("message") or status).strip()
            if any(hint in message.lower() for hint in _EMPTY_STATES):
                return None
            return message
        return None

    outer = failure(payload)
    if outer:
        return outer

    result = payload.get("result")
    for row in result if isinstance(result, list) else [result]:
        inner = failure(row)
        if inner:
            return inner
    return None
