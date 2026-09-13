"""IIFL Capital session management.

Login flow (per the official docs):
  1. Redirect the client to https://markets.iiflcapital.com/?v=1&appkey=<APP_KEY>
     (optionally with &redirecturl=<url>).
  2. Client logs in with trading credentials + OTP/TOTP.
  3. IIFL redirects back with ``authCode`` and ``clientId`` in the query string.
  4. checkSum = SHA256(clientId + authCode + appSecret)
  5. POST /v1/getusersession {"checkSum": ...} -> {"status":"Ok","userSession":"<JWT>"}
  6. Send ``Authorization: Bearer <JWT>`` on every subsequent request.

The JWT expires at midnight IST and the authCode is single-use, so we cache
the token on disk and refuse to reuse a stale one.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path

from loguru import logger

IST = timezone(timedelta(hours=5, minutes=30))


class IiflAuthError(RuntimeError):
    pass


@dataclass
class Session:
    user_session: str
    client_id: str
    created_at: datetime

    @property
    def expires_at(self) -> datetime:
        """Tokens die at midnight IST on the day they were generated."""
        local = self.created_at.astimezone(IST)
        midnight = datetime.combine(
            local.date() + timedelta(days=1), time(0, 0), tzinfo=IST
        )
        return midnight

    def is_valid(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(tz=UTC)
        return now < self.expires_at - timedelta(seconds=30)

    def is_expired(self, now: datetime | None = None) -> bool:
        return not self.is_valid(now)

    def preferred_username(self) -> str:
        """The ``preferred_username`` claim — used as the MQTT client id and
        as the topic for order/trade updates."""
        try:
            payload = self.user_session.split(".")[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            return json.loads(base64.urlsafe_b64decode(payload))["preferred_username"]
        except Exception as exc:  # malformed or non-IIFL token
            raise IiflAuthError("could not decode preferred_username from JWT") from exc

    def to_json(self) -> str:
        return json.dumps(
            {
                "user_session": self.user_session,
                "client_id": self.client_id,
                "created_at": self.created_at.isoformat(),
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> Session:
        data = json.loads(raw)
        return cls(
            user_session=data["user_session"],
            client_id=data["client_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )


def build_checksum(client_id: str, auth_code: str, app_secret: str) -> str:
    """SHA256(clientId + authCode + appSecret)."""
    raw = f"{client_id}{auth_code}{app_secret}".encode()
    return hashlib.sha256(raw).hexdigest()


def login_url(app_key: str, redirect_url: str | None = None) -> str:
    url = f"https://markets.iiflcapital.com/?v=1&appkey={app_key}"
    if redirect_url:
        url += f"&redirecturl={redirect_url}"
    return url


class SessionStore:
    """Persists the day's token so restarts don't burn a fresh authCode."""

    def __init__(self, path: str | Path = ".cache/iifl_session.json") -> None:
        self.path = Path(path)

    def load(self) -> Session | None:
        if not self.path.exists():
            return None
        try:
            session = Session.from_json(self.path.read_text(encoding="utf8"))
        except Exception:  # noqa: BLE001
            return None
        if not session.is_valid():
            logger.info("cached IIFL session has expired (tokens die at midnight IST)")
            self.clear()
            return None
        return session

    def save(self, session: Session) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(session.to_json(), encoding="utf8")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
