"""TOTP (RFC 6238) — implemented here rather than pulled in.

The algorithm is ~30 lines, entirely specified, and the alternative is another
dependency on the authentication path. ``hmac`` and ``base64`` are stdlib.

Defaults follow what authenticator apps expect: SHA-1, 6 digits, 30-second step.
SHA-1 is correct here despite its reputation — HMAC-SHA1 is not affected by the
collision attacks on plain SHA-1, and RFC 6238 specifies it as the default that
every app implements.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, urlencode

DEFAULT_DIGITS = 6
DEFAULT_STEP = 30
DEFAULT_ALGORITHM = "sha1"

_ALGORITHMS = {
    "sha1": hashlib.sha1,
    "sha256": hashlib.sha256,
    "sha512": hashlib.sha512,
}


def generate_secret(byte_length: int = 20) -> str:
    """A fresh base32 seed. 20 bytes = 160 bits, the RFC's recommendation."""
    return base64.b32encode(secrets.token_bytes(byte_length)).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    cleaned = secret.strip().replace(" ", "").upper()
    # Authenticator apps strip padding; b32decode demands it be correct.
    padding = "=" * (-len(cleaned) % 8)
    try:
        return base64.b32decode(cleaned + padding, casefold=True)
    except Exception as exc:  # noqa: BLE001 - surfaced as a ValueError to callers
        raise ValueError("not a valid base32 TOTP secret") from exc


def _hotp(key: bytes, counter: int, digits: int, algorithm: str) -> str:
    digest = _ALGORITHMS[algorithm]
    mac = hmac.new(key, counter.to_bytes(8, "big"), digest).digest()
    # Dynamic truncation (RFC 4226 §5.3).
    offset = mac[-1] & 0x0F
    truncated = int.from_bytes(mac[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


def code_at(
    secret: str,
    at: float | None = None,
    *,
    digits: int = DEFAULT_DIGITS,
    step: int = DEFAULT_STEP,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    """The code valid at a given moment. ``at`` is a Unix timestamp."""
    key = _decode_secret(secret)
    moment = time.time() if at is None else at
    return _hotp(key, int(moment // step), digits, algorithm)


def match_counter(
    secret: str,
    code: str,
    at: float | None = None,
    *,
    digits: int = DEFAULT_DIGITS,
    step: int = DEFAULT_STEP,
    algorithm: str = DEFAULT_ALGORITHM,
    window: int = 1,
) -> int | None:
    """Return the counter a code matched, or None.

    Returning the counter rather than a bool is what makes replay rejection
    possible: the caller stores the accepted counter and refuses anything less
    than or equal to it, so the same code cannot be used twice inside its window.

    ``window=1`` accepts the previous and next step, which is the standard
    concession to clock drift. Widening it is a real security cost — every extra
    step is another code an attacker may guess.
    """
    cleaned = (code or "").strip().replace(" ", "")
    if not cleaned.isdigit() or len(cleaned) != digits:
        return None
    try:
        key = _decode_secret(secret)
    except ValueError:
        return None

    moment = time.time() if at is None else at
    current = int(moment // step)
    for offset in range(-window, window + 1):
        counter = current + offset
        if counter < 0:
            continue
        candidate = _hotp(key, counter, digits, algorithm)
        # Constant-time: a timing difference between "wrong at position 0" and
        # "wrong at position 5" leaks the prefix.
        if hmac.compare_digest(candidate, cleaned):
            return counter
    return None


def verify(
    secret: str,
    code: str,
    at: float | None = None,
    *,
    digits: int = DEFAULT_DIGITS,
    step: int = DEFAULT_STEP,
    algorithm: str = DEFAULT_ALGORITHM,
    window: int = 1,
) -> bool:
    return match_counter(
        secret, code, at, digits=digits, step=step, algorithm=algorithm, window=window
    ) is not None


def provisioning_uri(
    secret: str,
    account_name: str,
    issuer: str = "atr",
    *,
    digits: int = DEFAULT_DIGITS,
    step: int = DEFAULT_STEP,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    """``otpauth://`` URI for a QR code or manual entry."""
    label = quote(f"{issuer}:{account_name}", safe="")
    params = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": algorithm.upper(),
            "digits": digits,
            "period": step,
        }
    )
    return f"otpauth://totp/{label}?{params}"


def seconds_remaining(at: float | None = None, step: int = DEFAULT_STEP) -> int:
    moment = time.time() if at is None else at
    return int(step - (moment % step))


__all__ = [
    "DEFAULT_DIGITS",
    "DEFAULT_STEP",
    "code_at",
    "generate_secret",
    "match_counter",
    "provisioning_uri",
    "seconds_remaining",
    "verify",
]
