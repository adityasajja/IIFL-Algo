"""Notification channels. Telegram is live; SMS is a prepaid slot.

Telegram setup (2 min, free): message @BotFather on Telegram → /newbot →
paste the token into TELEGRAM_BOT_TOKEN, then message @userinfobot for your
chat id → TELEGRAM_CHAT_ID. Hit `atr alerts test` to verify.

SMS later: sign up at fast2sms.com (free test credits), paste the key into
FAST2SMS_API_KEY. Route "q" works without DLT registration for low volume.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx
from loguru import logger


class Channel(ABC):
    name: str = "base"

    @abstractmethod
    def send(self, title: str, body: str) -> bool: ...


class LogChannel(Channel):
    name = "log"

    def send(self, title: str, body: str) -> bool:
        logger.warning("ALERT {} — {}", title, body)
        return True


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, title: str, body: str) -> bool:
        if not self.configured:
            return False
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id,
                      "text": f"*{title}*\n{body}",
                      "parse_mode": "Markdown"},
                timeout=self.timeout,
            )
            return resp.status_code == 200
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram send failed: {}", exc)
            return False


class Fast2SmsChannel(Channel):
    """Real SMS. Inactive until FAST2SMS_API_KEY is set."""

    name = "sms"

    def __init__(self, api_key: str = "", timeout: float = 10.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def send(self, title: str, body: str) -> bool:
        if not self.configured:
            return False
        try:
            resp = httpx.post(
                "https://www.fast2sms.com/dev/bulkV2",
                headers={"authorization": self.api_key},
                data={"route": "q", "message": f"{title}: {body}",
                      "language": "english", "flash": "0"},
                timeout=self.timeout,
            )
            return resp.status_code == 200
        except Exception as exc:  # noqa: BLE001
            logger.warning("sms send failed: {}", exc)
            return False


def channels_from_settings(settings) -> list[Channel]:
    """Ordered by preference — engine tries each until one succeeds."""
    out: list[Channel] = []
    tg = TelegramChannel(getattr(settings, "telegram_bot_token", ""),
                         getattr(settings, "telegram_chat_id", ""))
    if tg.configured:
        out.append(tg)
    sms = Fast2SmsChannel(getattr(settings, "fast2sms_api_key", ""))
    if sms.configured:
        out.append(sms)
    out.append(LogChannel())
    return out
