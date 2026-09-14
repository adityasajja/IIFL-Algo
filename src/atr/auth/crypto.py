"""Encryption for secrets at rest.

Used for the TOTP seed and (from Phase 3) per-user broker credentials. Built on
``cryptography``'s Fernet — AES-128-CBC with an HMAC-SHA256 tag — rather than
anything hand-rolled, because an authenticated-encryption construction written
in this repo would be the weakest link in the whole system.

**Key material resolution**, in order:

1. ``ATR_SECRET_KEY`` from settings/environment. This is the correct production
   answer: the key lives outside the repository and outside the database.
2. A generated key file at ``data/.app_secret`` (mode 0600). Convenient for a
   single-operator install and clearly worse — the key sits next to the data it
   protects, so it defends against a database dump or a stray backup, not against
   someone with filesystem access.

If neither is available the box refuses to operate rather than silently storing
plaintext. A security control that degrades quietly is not a control.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("atr.auth.crypto")

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KEY_FILE = ROOT / "data" / ".app_secret"

PREFIX = "v1:"


class SecretBoxError(RuntimeError):
    """Raised when the box cannot be constructed or a value cannot be opened."""


class SecretBox:
    """Symmetric encryption with a versioned, self-describing envelope."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise SecretBoxError("key material must be 32 bytes")
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    # ------------------------------------------------------------- construction
    @staticmethod
    def derive_key(material: str) -> bytes:
        """Stretch arbitrary key material to 32 bytes.

        SHA-256 rather than a slow KDF: this input is expected to be a
        high-entropy machine secret, and a slow KDF on every decrypt would put
        password-hashing cost on the request path. It is *not* a substitute for
        a real password KDF, and is not used as one.
        """
        return hashlib.sha256(material.encode("utf-8")).digest()

    @classmethod
    def from_passphrase(cls, material: str) -> SecretBox:
        if not material:
            raise SecretBoxError("empty key material")
        return cls(cls.derive_key(material))

    @classmethod
    def from_settings(cls) -> SecretBox:
        from atr.config.settings import get_settings

        configured = (get_settings().atr_secret_key or "").strip()
        if configured:
            return cls.from_passphrase(configured)

        key_file = DEFAULT_KEY_FILE
        if key_file.exists():
            raw = key_file.read_text(encoding="utf-8").strip()
            if not raw:
                raise SecretBoxError(f"key file {key_file} is empty")
            return cls.from_passphrase(raw)

        key_file.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_hex(32)
        key_file.write_text(generated, encoding="utf-8")
        with _best_effort_chmod(key_file, 0o600):
            pass
        logger.warning(
            "ATR_SECRET_KEY is not set; generated %s. This protects secrets from a "
            "database dump but not from filesystem access — set ATR_SECRET_KEY for "
            "anything beyond a local install.",
            key_file,
        )
        return cls.from_passphrase(generated)

    # ------------------------------------------------------------------- use
    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            raise SecretBoxError("cannot encrypt None")
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{PREFIX}{token}"

    def decrypt(self, envelope: str) -> str:
        if not envelope:
            raise SecretBoxError("cannot decrypt an empty value")
        if not envelope.startswith(PREFIX):
            # Refusing an unprefixed value is the point: it means an older or
            # foreign writer left plaintext in the column, and quietly returning
            # it would hide that.
            raise SecretBoxError("unrecognised envelope — refusing to treat it as plaintext")
        try:
            return self._fernet.decrypt(envelope[len(PREFIX) :].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretBoxError(
                "could not decrypt: wrong key, or the value was tampered with"
            ) from exc

    def try_decrypt(self, envelope: str) -> str | None:
        try:
            return self.decrypt(envelope)
        except SecretBoxError:
            return None


class _best_effort_chmod:
    """Tighten permissions where the platform supports it, ignore where it does not.

    Windows ignores POSIX modes, and ``os.chmod`` there can only toggle the
    read-only bit. Failing the whole setup over that would be worse than a
    logged no-op.
    """

    def __init__(self, path: Path, mode: int) -> None:
        self.path = path
        self.mode = mode

    def __enter__(self) -> None:
        try:
            os.chmod(self.path, self.mode)
        except OSError as exc:  # pragma: no cover - platform dependent
            logger.debug("could not chmod %s: %s", self.path, exc)

    def __exit__(self, *exc_info: object) -> None:
        return None


__all__ = ["DEFAULT_KEY_FILE", "PREFIX", "SecretBox", "SecretBoxError"]
