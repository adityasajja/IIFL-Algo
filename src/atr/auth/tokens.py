"""Opaque credential generation and hashing.

Two credential types, one rule: **the raw value is never stored.** Sessions and
API keys are persisted as SHA-256 digests, so a database dump does not yield a
usable credential, and the API can still verify a presented value in one indexed
lookup.

SHA-256 rather than scrypt is correct here and not a shortcut. These are
high-entropy random tokens (256 bits), not human-chosen passwords, so there is
nothing to brute-force and no need to make verification slow.
"""

from __future__ import annotations

import hashlib
import secrets

#: 32 bytes = 256 bits. Guessing is not a threat model at this size.
SESSION_TOKEN_BYTES = 32
API_KEY_SECRET_BYTES = 24

API_KEY_PREFIX = "atr"
API_KEY_SEPARATOR = "_"


def hash_token(raw: str) -> str:
    """SHA-256 hex digest of a credential. Deterministic; safe to index."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_session_token() -> tuple[str, str]:
    """Return ``(raw, digest)``. The raw value is returned to the client once."""
    raw = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    return raw, hash_token(raw)


def new_api_key() -> tuple[str, str, str]:
    """Return ``(raw, prefix, digest)``.

    Format: ``atr_<8 hex>_<hex secret>``. Both segments are hex rather than
    URL-safe base64 on purpose: base64url's alphabet includes ``_``, which is the
    separator, so a generated secret could contain one and the key would no
    longer parse into three parts.

    The middle segment is stored in the clear so a UI can render
    "atr_4f9c2a1b…" and a user can tell their keys apart, while the secret half
    remains unrecoverable.
    """
    prefix = secrets.token_hex(4)
    secret = secrets.token_hex(API_KEY_SECRET_BYTES)
    raw = f"{API_KEY_PREFIX}{API_KEY_SEPARATOR}{prefix}{API_KEY_SEPARATOR}{secret}"
    return raw, prefix, hash_token(raw)


def split_api_key(raw: str) -> tuple[str, str] | None:
    """``(prefix, secret)``, or None when the shape is wrong.

    Splits with ``maxsplit=2`` so a secret containing the separator still parses —
    a previous version counted separators, which silently rejected every key whose
    random secret happened to contain one.
    """
    if not raw:
        return None
    parts = raw.split(API_KEY_SEPARATOR, 2)
    if len(parts) != 3:
        return None
    scheme, prefix, secret = parts
    if scheme != API_KEY_PREFIX or not prefix or not secret:
        return None
    return prefix, secret


def looks_like_api_key(raw: str) -> bool:
    """Cheap shape check, so an API key in the Authorization header is not
    hashed and looked up as if it were a session token."""
    return split_api_key(raw) is not None


def api_key_prefix(raw: str) -> str | None:
    """Extract the display prefix, or None if the shape is wrong."""
    parts = split_api_key(raw)
    return parts[0] if parts else None


def fingerprint(raw: str) -> str:
    """First 8 characters of the digest — safe to log, useless to an attacker.

    Logs need to correlate a credential across requests without recording it.
    """
    return hash_token(raw)[:8] if raw else ""


__all__ = [
    "API_KEY_PREFIX",
    "api_key_prefix",
    "fingerprint",
    "hash_token",
    "looks_like_api_key",
    "new_api_key",
    "new_session_token",
    "split_api_key",
]
