"""The authentication service.

Everything that decides "who is this and what may they do" lives here, so that
the API layer only ever asks a question and never implements the answer.

Design decisions worth stating:

* **User enumeration is blocked in two places.** A missing account still pays for
  a password hash (so the response time does not reveal existence), and the
  message for a bad password and a bad username is identical.
* **Lockout is time-based.** A permanent lock turns a typo into a support ticket
  and turns a deliberate lockout into a denial-of-service against the owner.
* **TOTP replay is rejected.** The accepted counter is stored, so a code observed
  in transit cannot be reused inside its own 30-second window.
* **Every state change is audited**, including the failures — a denied login is
  the event you most want to see later.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from atr.appdb.engine import AppDatabase, get_app_db, utcnow
from atr.appdb.repositories import (
    ApiKeyRepository,
    SessionRepository,
    UserRepository,
)
from atr.audit.log import record as audit_record
from atr.auth import totp
from atr.auth.crypto import SecretBox
from atr.auth.models import LoginResult, Principal
from atr.auth.passwords import hash_password, needs_rehash, strength_problems, verify_password
from atr.auth.rbac import MFA_REQUIRED_ROLES, Permission, Role
from atr.auth.tokens import fingerprint, hash_token, new_api_key, new_session_token

logger = logging.getLogger("atr.auth")


class AuthError(Exception):
    """A caller-facing authentication failure."""

    def __init__(self, message: str, *, code: str = "auth_error", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


#: A real scrypt digest of a random value, verified against when the account does
#: not exist, so a missing user and a wrong password take the same time.
_DUMMY_DIGEST = hash_password("atr-dummy-password-for-constant-time-login")

_PUBLIC_USER_FIELDS = (
    "user_id",
    "email",
    "username",
    "display_name",
    "role",
    "is_active",
    "mfa_enabled",
    "created_at",
    "updated_at",
    "last_login_at",
)


def _public_user(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip credential material. Nothing outside this module sees these."""
    if row is None:
        return None
    return {k: row.get(k) for k in _PUBLIC_USER_FIELDS}


class AuthService:
    def __init__(self, db: AppDatabase | None = None) -> None:
        self._db = db
        self._secret_box: SecretBox | None = None
        self._secret_box_failed = False

    # ------------------------------------------------------------------ setup
    @property
    def db(self) -> AppDatabase:
        return self._db or get_app_db()

    def _settings(self):
        from atr.config.settings import get_settings

        return get_settings()

    @property
    def secret_box(self) -> SecretBox | None:
        """Lazily built; ``None`` when key material is unavailable.

        Returning None rather than raising lets password login work on a host
        where the key file cannot be written, and makes MFA fail with a clear
        message instead of a stack trace on every request.
        """
        if self._secret_box is None and not self._secret_box_failed:
            try:
                self._secret_box = SecretBox.from_settings()
            except Exception as exc:  # noqa: BLE001
                self._secret_box_failed = True
                logger.error("secret box unavailable, MFA disabled: %s", exc)
        return self._secret_box

    def _encrypt(self, value: str) -> str:
        box = self.secret_box
        if box is None:
            raise AuthError(
                "cannot store a secret: no encryption key available "
                "(set ATR_SECRET_KEY or make data/ writable)",
                code="secret_store_unavailable",
                status=503,
            )
        return box.encrypt(value)

    def _decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        box = self.secret_box
        if box is None:
            return None
        return box.try_decrypt(value)

    # -------------------------------------------------------------- bootstrap
    def needs_bootstrap(self) -> bool:
        """True when no account exists yet, i.e. the first-run screen applies."""
        return self.db.user_count() == 0

    def bootstrap(
        self,
        *,
        email: str,
        username: str,
        password: str,
        display_name: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any], str, datetime]:
        """Create the first account as ``owner`` and return a live session.

        The zero-user check happens inside the same transaction as the insert, so
        two concurrent bootstraps cannot both create an owner.
        """
        self._check_password(password)
        with self.db.session() as session:
            if UserRepository.count(session) > 0:
                audit_record(
                    action="auth.bootstrap",
                    result="denied",
                    detail="an account already exists",
                    ip=ip,
                    request_id=request_id,
                    session=session,
                )
                raise AuthError(
                    "this instance is already set up", code="already_bootstrapped", status=409
                )
            try:
                user = UserRepository.create(
                    session,
                    email=email,
                    username=username,
                    password_hash=hash_password(password),
                    role=Role.OWNER.value,
                    display_name=display_name,
                )
            except IntegrityError as exc:
                raise AuthError(
                    "that email or username is already taken", code="duplicate", status=409
                ) from exc

            token, expires_at = self._issue_session(
                session, user_id=user["user_id"], ip=ip, user_agent=user_agent
            )
            audit_record(
                action="auth.bootstrap",
                actor=user["email"],
                user_id=user["user_id"],
                detail={"username": user["username"], "role": Role.OWNER.value},
                ip=ip,
                request_id=request_id,
                session=session,
            )
        return _public_user(user) or {}, token, expires_at

    # ------------------------------------------------------------- registration
    def register(
        self,
        *,
        email: str,
        username: str,
        password: str,
        display_name: str | None = None,
        role: Role | str = Role.VIEWER,
        ip: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an account. New self-registered users are always ``viewer``.

        The role argument exists for the admin path (``POST /users``); the public
        route never passes anything but the default, so a crafted request cannot
        self-promote.
        """
        self._check_password(password)
        with self.db.session() as session:
            try:
                user = UserRepository.create(
                    session,
                    email=email,
                    username=username,
                    password_hash=hash_password(password),
                    role=Role.parse(role).value,
                    display_name=display_name,
                )
            except IntegrityError as exc:
                raise AuthError(
                    "that email or username is already taken", code="duplicate", status=409
                ) from exc
            audit_record(
                action="auth.register",
                actor=user["email"],
                user_id=user["user_id"],
                detail={"role": user["role"]},
                ip=ip,
                request_id=request_id,
                session=session,
            )
        return _public_user(user) or {}

    # -------------------------------------------------------------------- login
    def login(
        self,
        *,
        identifier: str,
        password: str,
        totp_code: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
    ) -> LoginResult:
        settings = self._settings()

        with self.db.session() as session:
            user = UserRepository.get_by_identifier(session, identifier)

            if user is None:
                # Pay the same cost as a real verification so response time does
                # not disclose whether the account exists.
                verify_password(password, _DUMMY_DIGEST)
                audit_record(
                    action="auth.login",
                    result="failure",
                    actor=identifier[:64],
                    detail="unknown identifier",
                    ip=ip,
                    request_id=request_id,
                    session=session,
                )
                return LoginResult(status="invalid", reason="invalid credentials")

            if user.get("locked_until") and user["locked_until"] > utcnow():
                remaining = int((user["locked_until"] - utcnow()).total_seconds() // 60) + 1
                audit_record(
                    action="auth.login",
                    result="denied",
                    actor=user["email"],
                    user_id=user["user_id"],
                    detail={"reason": "locked", "minutes_remaining": remaining},
                    ip=ip,
                    request_id=request_id,
                    session=session,
                )
                return LoginResult(
                    status="locked",
                    reason=f"account locked; try again in {remaining} minute(s)",
                )

            if not user.get("is_active", True):
                audit_record(
                    action="auth.login",
                    result="denied",
                    actor=user["email"],
                    user_id=user["user_id"],
                    detail="account deactivated",
                    ip=ip,
                    request_id=request_id,
                    session=session,
                )
                return LoginResult(status="inactive", reason="this account is deactivated")

            if not verify_password(password, user["password_hash"]):
                counters = UserRepository.register_failure(
                    session,
                    user["user_id"],
                    max_attempts=settings.login_max_attempts,
                    lockout_minutes=settings.login_lockout_minutes,
                )
                remaining = max(0, settings.login_max_attempts - counters["failed_logins"])
                audit_record(
                    action="auth.login",
                    result="failure",
                    actor=user["email"],
                    user_id=user["user_id"],
                    detail={"reason": "bad password", "attempts_remaining": remaining},
                    ip=ip,
                    request_id=request_id,
                    session=session,
                )
                return LoginResult(
                    status="locked" if counters["locked_until"] else "invalid",
                    reason=(
                        f"too many attempts; locked for {settings.login_lockout_minutes} minutes"
                        if counters["locked_until"]
                        else "invalid credentials"
                    ),
                )

            # --- password is correct from here on ---
            if user.get("mfa_enabled"):
                if not totp_code:
                    audit_record(
                        action="auth.login",
                        result="pending",
                        actor=user["email"],
                        user_id=user["user_id"],
                        detail="password accepted, awaiting TOTP",
                        ip=ip,
                        request_id=request_id,
                        session=session,
                    )
                    return LoginResult(status="mfa_required", reason="enter your authenticator code")

                secret = self._decrypt(user.get("mfa_secret"))
                if not secret:
                    audit_record(
                        action="auth.login",
                        result="failure",
                        actor=user["email"],
                        user_id=user["user_id"],
                        detail="MFA enabled but the seed could not be decrypted",
                        ip=ip,
                        request_id=request_id,
                        session=session,
                    )
                    return LoginResult(
                        status="invalid",
                        reason="MFA is misconfigured for this account; an admin must reset it",
                    )

                counter = totp.match_counter(secret, totp_code)
                last_counter = int(user.get("mfa_last_counter") or -1)
                if counter is None or counter <= last_counter:
                    audit_record(
                        action="auth.login",
                        result="failure",
                        actor=user["email"],
                        user_id=user["user_id"],
                        detail={"reason": "bad or replayed TOTP"},
                        ip=ip,
                        request_id=request_id,
                        session=session,
                    )
                    return LoginResult(status="invalid", reason="invalid authenticator code")
                UserRepository.update(session, user["user_id"], mfa_last_counter=counter)

            # A privileged account with MFA required but not enrolled gets a
            # session that can enrol and do nothing else. The gate is enforced in
            # the API dependency layer, not here.
            mfa_satisfied = True
            if settings.require_mfa_for_privileged:
                privileged = Role.parse(user["role"]) in MFA_REQUIRED_ROLES
                mfa_satisfied = bool(user.get("mfa_enabled")) if privileged else True

            # Opportunistic rehash: the cost parameters may have been raised
            # since this digest was written.
            if needs_rehash(user["password_hash"]):
                UserRepository.update(
                    session, user["user_id"], password_hash=hash_password(password)
                )

            UserRepository.register_success(session, user["user_id"])
            token, expires_at = self._issue_session(
                session,
                user_id=user["user_id"],
                ip=ip,
                user_agent=user_agent,
                mfa_satisfied=mfa_satisfied,
            )
            refreshed = UserRepository.get(session, user["user_id"]) or user
            audit_record(
                action="auth.login",
                actor=user["email"],
                user_id=user["user_id"],
                detail={
                    "role": user["role"],
                    "mfa": bool(user.get("mfa_enabled")),
                    "mfa_satisfied": mfa_satisfied,
                },
                ip=ip,
                request_id=request_id,
                session=session,
            )
            return LoginResult(
                status="ok",
                token=token,
                expires_at=expires_at,
                user=_public_user(refreshed),
            )

    # ------------------------------------------------------------- resolution
    def resolve_session(self, raw_token: str | None) -> Principal | None:
        if not raw_token:
            return None
        with self.db.session() as session:
            row = SessionRepository.get_by_token_hash(session, hash_token(raw_token))
            if row is None or row.get("revoked_at") is not None:
                return None
            if row["expires_at"] <= utcnow():
                return None
            user = UserRepository.get(session, row["user_id"])
            if user is None or not user.get("is_active", True):
                return None

            # Throttle the last-seen write: doing it on every request turns a
            # read-only dashboard poll into a write.
            last_seen = row.get("last_seen_at")
            stale = not last_seen or (utcnow() - last_seen).total_seconds() > 60
            principal = Principal.build(
                user=user,
                auth_method="session",
                session_id=row["session_id"],
                mfa_satisfied=bool(row.get("mfa_satisfied", True)),
            )
            session_id = row["session_id"]

        # The write happens *after* the read transaction has ended, in its own. Doing
        # it inside the read transaction upgrades read to write, and SQLite refuses that
        # instantly (no waiting) if anything else committed in between: two parallel
        # requests from one session, a minute after the last, made one of them fail
        # authentication with "database is locked". A missed stamp costs nothing; a
        # failed login check costs the request.
        if stale:
            self._touch_session(session_id)
        return principal

    def _touch_session(self, session_id: str) -> None:
        try:
            with self.db.session() as session:
                SessionRepository.touch(session, session_id)
        except Exception as exc:  # noqa: BLE001 - never let a bookkeeping write break a request
            logger.debug("last-seen stamp skipped: %s", exc)

    def resolve_api_key(self, raw_key: str | None) -> Principal | None:
        if not raw_key:
            return None
        with self.db.session() as session:
            row = ApiKeyRepository.get_by_hash(session, hash_token(raw_key))
            if row is None or row.get("revoked_at") is not None:
                return None
            if row.get("expires_at") and row["expires_at"] <= utcnow():
                return None
            user = UserRepository.get(session, row["user_id"])
            if user is None or not user.get("is_active", True):
                return None

            scopes: set[Permission] = set()
            for raw_scope in (row.get("scopes") or "").split(","):
                raw_scope = raw_scope.strip()
                if not raw_scope:
                    continue
                try:
                    scopes.add(Permission.parse(raw_scope))
                except ValueError:
                    # A scope that no longer exists (removed permission) is
                    # dropped rather than granted. Fail closed.
                    logger.warning("api key %s references unknown scope %r", row["key_id"], raw_scope)

            # Throttle the usage stamp: an unthrottled write here turns every
            # read-only API call into a database write.
            last_used = row.get("last_used_at")
            stale = not last_used or (utcnow() - last_used).total_seconds() > 60
            key_id = row["key_id"]
            principal = Principal.build(
                user=user,
                auth_method="apikey",
                api_key_id=key_id,
                extra_permissions=frozenset(scopes),
            )

        # Same reason as for sessions: stamp in a separate transaction, best effort.
        if stale:
            try:
                with self.db.session() as session:
                    ApiKeyRepository.touch(session, key_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("api key usage stamp skipped: %s", exc)
        return principal

    def resolve(self, credential: str | None) -> Principal | None:
        """Resolve either credential type. API keys are recognised by shape."""
        if not credential:
            return None
        from atr.auth.tokens import looks_like_api_key

        if looks_like_api_key(credential):
            return self.resolve_api_key(credential)
        return self.resolve_session(credential)

    # ----------------------------------------------------------------- logout
    def logout(self, raw_token: str | None, *, ip: str | None = None) -> bool:
        if not raw_token:
            return False
        with self.db.session() as session:
            row = SessionRepository.get_by_token_hash(session, hash_token(raw_token))
            if row is None:
                return False
            changed = SessionRepository.revoke(session, row["session_id"])
            audit_record(
                action="auth.logout",
                user_id=row["user_id"],
                actor=fingerprint(raw_token),
                detail={"session_id": row["session_id"]},
                ip=ip,
                session=session,
            )
            return changed > 0

    def logout_all(self, user_id: str, *, actor: str | None = None, ip: str | None = None) -> int:
        with self.db.session() as session:
            count = SessionRepository.revoke_all_for_user(session, user_id)
            audit_record(
                action="auth.logout_all",
                user_id=user_id,
                actor=actor,
                detail={"revoked": count},
                ip=ip,
                session=session,
            )
            return count

    def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            return SessionRepository.list_for_user(session, user_id)

    def revoke_session(self, user_id: str, session_id: str, *, ip: str | None = None) -> bool:
        with self.db.session() as session:
            row = SessionRepository.get(session, session_id)
            # Ownership check before the write: a session id from someone else
            # must look like it does not exist.
            if row is None or row["user_id"] != user_id:
                return False
            changed = SessionRepository.revoke(session, session_id)
            audit_record(
                action="auth.session_revoke",
                user_id=user_id,
                target_type="session",
                target_id=session_id,
                ip=ip,
                session=session,
            )
            return changed > 0

    # ---------------------------------------------------------------- profile
    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.db.session() as session:
            return _public_user(UserRepository.get(session, user_id))

    def update_profile(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        email: str | None = None,
        ip: str | None = None,
    ) -> dict[str, Any] | None:
        fields: dict[str, Any] = {}
        if display_name is not None:
            fields["display_name"] = display_name
        if email is not None:
            fields["email"] = email
        if not fields:
            with self.db.session() as session:
                return _public_user(UserRepository.get(session, user_id))
        try:
            with self.db.session() as session:
                if UserRepository.update(session, user_id, **fields) == 0:
                    return None
                audit_record(
                    action="auth.profile_update",
                    user_id=user_id,
                    detail={"fields": sorted(fields)},
                    ip=ip,
                    session=session,
                )
                return _public_user(UserRepository.get(session, user_id))
        except IntegrityError as exc:
            raise AuthError("that email is already taken", code="duplicate", status=409) from exc

    def change_password(
        self,
        user_id: str,
        *,
        current_password: str,
        new_password: str,
        ip: str | None = None,
    ) -> bool:
        """Change the password and revoke every other session.

        Revoking siblings is the point: if the reason for the change is that the
        old password leaked, leaving other sessions alive defeats it.
        """
        self._check_password(new_password)
        with self.db.session() as session:
            user = UserRepository.get(session, user_id)
            if user is None:
                return False
            if not verify_password(current_password, user["password_hash"]):
                audit_record(
                    action="auth.password_change",
                    result="failure",
                    user_id=user_id,
                    detail="current password did not match",
                    ip=ip,
                    session=session,
                )
                raise AuthError("current password is incorrect", code="bad_password", status=400)
            UserRepository.update(session, user_id, password_hash=hash_password(new_password))
            revoked = SessionRepository.revoke_all_for_user(session, user_id)
            audit_record(
                action="auth.password_change",
                user_id=user_id,
                detail={"sessions_revoked": revoked},
                ip=ip,
                session=session,
            )
            return True

    # -------------------------------------------------------------------- MFA
    def enroll_mfa(self, user_id: str, *, ip: str | None = None) -> tuple[str, str]:
        """Generate a seed and return ``(secret, otpauth_uri)``. Does not enable."""
        with self.db.session() as session:
            user = UserRepository.get(session, user_id)
            if user is None:
                raise AuthError("no such user", code="not_found", status=404)
            secret = totp.generate_secret()
            UserRepository.update(session, user_id, mfa_secret=self._encrypt(secret))
            uri = totp.provisioning_uri(secret, account_name=user["email"], issuer="atr")
            audit_record(
                action="auth.mfa_enroll",
                user_id=user_id,
                detail="seed issued, not yet activated",
                ip=ip,
                session=session,
            )
            return secret, uri

    def activate_mfa(self, user_id: str, code: str, *, ip: str | None = None) -> bool:
        with self.db.session() as session:
            user = UserRepository.get(session, user_id)
            if user is None:
                return False
            secret = self._decrypt(user.get("mfa_secret"))
            if not secret:
                raise AuthError(
                    "no pending MFA enrolment; call enroll first",
                    code="no_enrolment",
                    status=400,
                )
            counter = totp.match_counter(secret, code)
            if counter is None:
                audit_record(
                    action="auth.mfa_activate",
                    result="failure",
                    user_id=user_id,
                    detail="code did not verify",
                    ip=ip,
                    session=session,
                )
                return False
            UserRepository.update(
                session,
                user_id,
                mfa_enabled=True,
                mfa_last_counter=counter,
            )
            audit_record(action="auth.mfa_activate", user_id=user_id, ip=ip, session=session)
            return True

    def disable_mfa(self, user_id: str, password: str, *, ip: str | None = None) -> bool:
        """Disabling a second factor requires the password, not just a session."""
        with self.db.session() as session:
            user = UserRepository.get(session, user_id)
            if user is None:
                return False
            if not verify_password(password, user["password_hash"]):
                audit_record(
                    action="auth.mfa_disable",
                    result="failure",
                    user_id=user_id,
                    detail="password did not match",
                    ip=ip,
                    session=session,
                )
                raise AuthError("password is incorrect", code="bad_password", status=400)
            UserRepository.update(
                session, user_id, mfa_enabled=False, mfa_secret=None, mfa_last_counter=None
            )
            audit_record(action="auth.mfa_disable", user_id=user_id, ip=ip, session=session)
            return True

    # --------------------------------------------------------------- api keys
    def create_api_key(
        self,
        user_id: str,
        *,
        label: str,
        scopes: list[str] | None = None,
        expires_in_days: int | None = None,
        ip: str | None = None,
    ) -> dict[str, Any]:
        """Create a key. The secret is returned once and never again."""
        validated: list[str] = []
        for raw in scopes or []:
            try:
                validated.append(Permission.parse(raw).value)
            except ValueError as exc:
                raise AuthError(
                    f"unknown scope {raw!r}", code="bad_scope", status=400
                ) from exc

        with self.db.session() as session:
            user = UserRepository.get(session, user_id)
            if user is None:
                raise AuthError("no such user", code="not_found", status=404)
            # A key may never hold a permission its owner lacks.
            from atr.auth.rbac import permissions_for

            own = {p.value for p in permissions_for(user["role"])}
            overreach = sorted(set(validated) - own)
            if overreach:
                raise AuthError(
                    f"scopes exceed your role: {', '.join(overreach)}",
                    code="scope_exceeds_role",
                    status=403,
                )

            raw_key, prefix, digest = new_api_key()
            expires_at = (
                utcnow() + timedelta(days=expires_in_days) if expires_in_days else None
            )
            row = ApiKeyRepository.create(
                session,
                user_id=user_id,
                label=label,
                prefix=prefix,
                key_hash=digest,
                scopes=validated,
                expires_at=expires_at,
            )
            audit_record(
                action="apikey.create",
                user_id=user_id,
                target_type="api_key",
                target_id=row["key_id"],
                detail={"label": label, "scopes": validated},
                ip=ip,
                session=session,
            )
            return {
                "key_id": row["key_id"],
                "label": row["label"],
                "prefix": row["prefix"],
                "scopes": validated,
                "created_at": row["created_at"],
                "expires_at": expires_at,
                # The one and only time this value is ever returned.
                "key": raw_key,
            }

    def list_api_keys(self, user_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = ApiKeyRepository.list_for_user(session, user_id)
        return [
            {
                "key_id": r["key_id"],
                "label": r["label"],
                "prefix": r["prefix"],
                "scopes": [s for s in (r["scopes"] or "").split(",") if s],
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
                "last_used_at": r["last_used_at"],
                "revoked": r["revoked_at"] is not None,
            }
            for r in rows
        ]

    def revoke_api_key(self, user_id: str, key_id: str, *, ip: str | None = None) -> bool:
        with self.db.session() as session:
            changed = ApiKeyRepository.revoke(session, key_id, user_id)
            audit_record(
                action="apikey.revoke",
                result="success" if changed else "failure",
                user_id=user_id,
                target_type="api_key",
                target_id=key_id,
                ip=ip,
                session=session,
            )
            return changed > 0

    # ------------------------------------------------------------------ admin
    def list_users(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            return [u for u in (_public_user(r) for r in UserRepository.list_all(session)) if u]

    def update_user(
        self,
        actor: Principal,
        user_id: str,
        *,
        role: Role | str | None = None,
        is_active: bool | None = None,
        display_name: str | None = None,
        ip: str | None = None,
    ) -> dict[str, Any] | None:
        fields: dict[str, Any] = {}
        if role is not None:
            fields["role"] = Role.parse(role).value
        if is_active is not None:
            fields["is_active"] = is_active
        if display_name is not None:
            fields["display_name"] = display_name
        if not fields:
            return self.get_user(user_id)

        with self.db.session() as session:
            target = UserRepository.get(session, user_id)
            if target is None:
                return None

            # Refuse to remove the last owner. Without this an admin can demote
            # the only owner and permanently lock everyone out of user management.
            losing_owner = target["role"] == Role.OWNER.value and (
                fields.get("role") not in (None, Role.OWNER.value)
                or fields.get("is_active") is False
            )
            if losing_owner and UserRepository.count_by_role(session, Role.OWNER.value) <= 1:
                raise AuthError(
                    "cannot demote or deactivate the last owner",
                    code="last_owner",
                    status=409,
                )

            UserRepository.update(session, user_id, **fields)
            audit_record(
                action="user.update",
                user_id=actor.user_id,
                actor=actor.actor,
                target_type="user",
                target_id=user_id,
                detail={"changes": {k: str(v) for k, v in fields.items()}},
                ip=ip,
                session=session,
            )
            if fields.get("is_active") is False:
                SessionRepository.revoke_all_for_user(session, user_id)
            return _public_user(UserRepository.get(session, user_id))

    # ---------------------------------------------------------------- helpers
    def _issue_session(
        self,
        session: Session,
        *,
        user_id: str,
        ip: str | None,
        user_agent: str | None,
        mfa_satisfied: bool = True,
    ) -> tuple[str, datetime]:
        settings = self._settings()
        raw, digest = new_session_token()
        expires_at = utcnow() + timedelta(hours=settings.session_ttl_hours)
        SessionRepository.create(
            session,
            user_id=user_id,
            token_hash=digest,
            expires_at=expires_at,
            ip=ip,
            user_agent=user_agent,
            mfa_satisfied=mfa_satisfied,
        )
        return raw, expires_at

    @staticmethod
    def _check_password(password: str) -> None:
        problems = strength_problems(password)
        if problems:
            raise AuthError(
                "password does not meet requirements: " + "; ".join(problems),
                code="weak_password",
                status=400,
            )


_service: AuthService | None = None


def get_auth_service() -> AuthService:
    global _service
    if _service is None:
        _service = AuthService()
    return _service


def reset_auth_service() -> None:
    """Drop the singleton. Tests use this."""
    global _service
    _service = None


__all__ = [
    "AuthError",
    "AuthService",
    "get_auth_service",
    "reset_auth_service",
]
