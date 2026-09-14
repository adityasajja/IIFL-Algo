"""Password hashing.

scrypt from the standard library: memory-hard, no third-party dependency, and
present everywhere Python 3.12 is. Parameters live inside the digest string, so
raising the cost later is a rehash-on-next-login rather than a migration.

Digest format::

    scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>

Verification is constant-time. A malformed or unknown-format digest returns
False rather than raising — a corrupt row must not become a 500 on the login
route, which would tell an attacker they found something.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

#: 128 * N * r = 16 MiB of memory per hash. High enough that a GPU is not a
#: shortcut, low enough that a login is still ~50ms.
_N = 2**14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024  # scrypt has no limit; this bounds request size


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str) -> str:
    """Hash a password with a fresh random salt."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, digest: str) -> bool:
    """Constant-time verification. Returns False on any malformed input."""
    if not password or not digest:
        return False
    try:
        scheme, n_s, r_s, p_s, salt_s, hash_s = digest.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _unb64(salt_s)
        expected = _unb64(hash_s)
    except (ValueError, TypeError):
        return False

    try:
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, MemoryError):
        # Absurd parameters in a hand-edited row. Fail closed.
        return False
    return hmac.compare_digest(candidate, expected)


def needs_rehash(digest: str) -> bool:
    """True when the digest was made with weaker parameters than the current ones."""
    try:
        scheme, n_s, r_s, p_s, _salt, _hash = digest.split("$")
    except (ValueError, AttributeError):
        return True
    if scheme != "scrypt":
        return True
    try:
        return (int(n_s), int(r_s), int(p_s)) != (_N, _R, _P)
    except ValueError:
        return True


def strength_problems(password: str) -> list[str]:
    """Human-readable reasons a password is unacceptable. Empty list means fine.

    Deliberately modest: length and variety, no character-class theatre, and no
    dictionary. A blocklist of common passwords belongs in a file this repo does
    not ship, and inventing a short one would be worse than admitting the gap.
    """
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        problems.append(f"must be at most {MAX_PASSWORD_LENGTH} characters")
    if password.lower() == password:
        problems.append("must contain an uppercase letter")
    if password.upper() == password:
        problems.append("must contain a lowercase letter")
    if not any(ch.isdigit() for ch in password):
        problems.append("must contain a digit")
    return problems


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "hash_password",
    "needs_rehash",
    "strength_problems",
    "verify_password",
]
