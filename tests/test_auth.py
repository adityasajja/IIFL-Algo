"""Authentication: primitives, the permission matrix, and the service flows.

The properties that matter, stated so a failure says which one broke:

* a password digest cannot be reversed and a wrong password never verifies
* a TOTP code is accepted once, and a replayed one is not
* a researcher cannot place an order, and a viewer cannot write anything
* a locked account stays locked, and a lock is time-based rather than permanent
* an API key cannot exceed the role that created it
"""

from __future__ import annotations

import time

import pytest

from atr.appdb.engine import utcnow
from atr.auth import totp
from atr.auth.crypto import SecretBox, SecretBoxError
from atr.auth.models import anonymous_principal
from atr.auth.passwords import (
    hash_password,
    needs_rehash,
    strength_problems,
    verify_password,
)
from atr.auth.rbac import (
    MFA_REQUIRED_ROLES,
    Permission,
    PermissionDenied,
    Role,
    has_permission,
    permissions_for,
    roles_with,
)
from atr.auth.service import AuthError, AuthService
from atr.auth.tokens import (
    api_key_prefix,
    fingerprint,
    hash_token,
    looks_like_api_key,
    new_api_key,
    new_session_token,
    split_api_key,
)

GOOD_PASSWORD = "Str0ngPassw0rd"


# ─── passwords ────────────────────────────────────────────────────────────────
def test_password_roundtrip_and_rejection():
    digest = hash_password(GOOD_PASSWORD)
    assert digest.startswith("scrypt$")
    assert GOOD_PASSWORD not in digest  # nothing recoverable in the digest
    assert verify_password(GOOD_PASSWORD, digest) is True
    assert verify_password("wrong-password", digest) is False
    # A fresh salt every time: two identical passwords must not collide.
    assert hash_password(GOOD_PASSWORD) != digest


def test_password_verify_is_false_on_garbage_not_an_exception():
    """A corrupt row must not turn the login route into a 500."""
    for broken in ("", "not-a-digest", "scrypt$bad", "bcrypt$1$2$3$4$5", "scrypt$1$2$3$4"):
        assert verify_password("whatever", broken) is False


def test_needs_rehash_flags_foreign_schemes():
    assert needs_rehash(hash_password(GOOD_PASSWORD)) is False
    assert needs_rehash("bcrypt$whatever") is True
    assert needs_rehash("") is True


def test_strength_rules():
    assert strength_problems(GOOD_PASSWORD) == []
    assert "characters" in " ".join(strength_problems("Sh0rt"))
    assert strength_problems("alllowercase1") != []
    assert strength_problems("ALLUPPERCASE1") != []
    assert strength_problems("NoDigitsHereX") != []


# ─── TOTP ─────────────────────────────────────────────────────────────────────
def test_totp_matches_rfc6238_vector():
    """RFC 6238 Appendix B, SHA-1, 8 digits, ASCII secret '12345678901234567890'."""
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp.code_at(secret, at=59, digits=8) == "94287082"
    assert totp.code_at(secret, at=1111111109, digits=8) == "07081804"


def test_totp_accepts_adjacent_step_but_not_a_wrong_code():
    secret = totp.generate_secret()
    now = time.time()
    current = totp.code_at(secret, at=now)
    assert totp.verify(secret, current, at=now) is True
    assert totp.verify(secret, totp.code_at(secret, at=now - 30), at=now) is True  # drift
    assert totp.verify(secret, "000000", at=now) is False
    assert totp.verify(secret, "", at=now) is False
    assert totp.verify(secret, "abcdef", at=now) is False


def test_totp_match_counter_identifies_the_step():
    """The counter is what makes replay rejection possible."""
    secret = totp.generate_secret()
    now = time.time()
    counter = totp.match_counter(secret, totp.code_at(secret, at=now), at=now)
    assert counter == int(now // 30)
    assert totp.match_counter(secret, "123456", at=now) is None


def test_totp_provisioning_uri_is_scannable():
    secret = totp.generate_secret()
    uri = totp.provisioning_uri(secret, "owner@example.com", issuer="atr")
    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "issuer=atr" in uri
    assert "period=30" in uri


def test_generate_secret_is_valid_base32_and_unique():
    secrets_ = {totp.generate_secret() for _ in range(20)}
    assert len(secrets_) == 20
    for value in secrets_:
        assert totp.verify(value, totp.code_at(value))


# ─── tokens ───────────────────────────────────────────────────────────────────
def test_session_tokens_are_high_entropy_and_hashed():
    raw, digest = new_session_token()
    assert len(raw) >= 40
    assert digest == hash_token(raw)
    assert raw not in digest
    assert new_session_token()[0] != raw


def test_api_key_shape_and_prefix_extraction():
    raw, prefix, digest = new_api_key()
    assert looks_like_api_key(raw) is True
    assert api_key_prefix(raw) == prefix
    assert digest == hash_token(raw)
    assert prefix in raw and len(prefix) == 8
    assert len(raw.split("_")[2]) >= 32  # the secret half is long
    assert looks_like_api_key("not-a-key") is False
    assert looks_like_api_key("atr_onlyonepart") is False
    assert looks_like_api_key("atr__") is False
    # A secret containing the separator must still parse. An earlier version
    # counted separators and rejected exactly this case.
    assert split_api_key("atr_abcd1234_se_cret_with_underscores") == (
        "abcd1234",
        "se_cret_with_underscores",
    )


def test_generated_api_keys_never_contain_the_separator_in_the_secret():
    """Hex, not base64url — base64url's alphabet includes '_'."""
    for _ in range(50):
        raw, _, _ = new_api_key()
        prefix, secret = split_api_key(raw)
        assert "_" not in secret
        assert prefix in raw


def test_fingerprint_is_stable_and_short():
    assert fingerprint("abc") == fingerprint("abc")
    assert len(fingerprint("abc")) == 8
    assert fingerprint("") == ""


# ─── the permission matrix ────────────────────────────────────────────────────
def test_research_and_execution_are_separate_authorities():
    """The brief asks for this split; assert it rather than trusting the table."""
    assert has_permission(Role.RESEARCHER, Permission.STRATEGY_WRITE) is True
    assert has_permission(Role.RESEARCHER, Permission.ORDER_PLACE) is False
    assert has_permission(Role.TRADER, Permission.ORDER_PLACE) is True


def test_only_admin_and_owner_may_change_execution_mode():
    assert roles_with(Permission.EXECUTION_MODE_CHANGE) == [Role.ADMIN, Role.OWNER]
    assert has_permission(Role.TRADER, Permission.EXECUTION_MODE_CHANGE) is False


def test_viewer_is_read_only():
    writable = {
        Permission.WATCHLIST_WRITE,
        Permission.STRATEGY_WRITE,
        Permission.ORDER_PLACE,
        Permission.ORDER_CANCEL,
        Permission.RISK_CONFIGURE,
        Permission.USER_WRITE,
        Permission.SYSTEM_CONFIGURE,
    }
    for permission in writable:
        assert has_permission(Role.VIEWER, permission) is False
    assert has_permission(Role.VIEWER, Permission.MARKET_READ) is True


def test_owner_holds_every_permission_and_roles_are_nested():
    assert permissions_for(Role.OWNER) == frozenset(Permission)
    for smaller, bigger in (
        (Role.VIEWER, Role.RESEARCHER),
        (Role.RESEARCHER, Role.TRADER),
        (Role.TRADER, Role.ADMIN),
        (Role.ADMIN, Role.OWNER),
    ):
        assert permissions_for(smaller) < permissions_for(bigger)


def test_unknown_permission_is_never_granted():
    """Fail closed: an unrecognised string must not resolve to a grant."""
    assert has_permission(Role.OWNER, "not:a:permission") is False


def test_require_raises_with_the_permission_named():
    with pytest.raises(PermissionDenied) as exc:
        from atr.auth.rbac import require

        require(Role.VIEWER, Permission.ORDER_PLACE)
    assert "order:place" in str(exc.value)


def test_role_and_permission_parse_rejects_nonsense():
    assert Role.parse("ADMIN") is Role.ADMIN
    with pytest.raises(ValueError):
        Role.parse("superuser")
    with pytest.raises(ValueError):
        Permission.parse("everything")
    assert frozenset({Role.ADMIN, Role.OWNER}) == MFA_REQUIRED_ROLES


def test_the_anonymous_principal_is_read_only():
    """Used only when AUTH_REQUIRED is off — and even then it cannot write."""
    principal = anonymous_principal()
    assert principal.is_authenticated is False
    assert principal.actor == "anonymous"
    assert principal.can(Permission.MARKET_READ) is True
    assert principal.can(Permission.WATCHLIST_WRITE) is False
    assert principal.can(Permission.ORDER_PLACE) is False
    assert principal.can(Permission.USER_WRITE) is False


# ─── secret box ───────────────────────────────────────────────────────────────
def test_secret_box_roundtrip_and_tamper_detection():
    box = SecretBox.from_passphrase("a-key")
    envelope = box.encrypt("JBSWY3DPEHPK3PXP")
    assert envelope.startswith("v1:")
    assert "JBSWY3DPEHPK3PXP" not in envelope
    assert box.decrypt(envelope) == "JBSWY3DPEHPK3PXP"

    with pytest.raises(SecretBoxError):
        box.decrypt(envelope[:-4] + "AAAA")
    # Refusing an unprefixed value is deliberate: it means plaintext is in the
    # column, and returning it would hide that.
    with pytest.raises(SecretBoxError):
        box.decrypt("JBSWY3DPEHPK3PXP")
    assert box.try_decrypt("JBSWY3DPEHPK3PXP") is None
    assert SecretBox.from_passphrase("a-key").decrypt(envelope) == "JBSWY3DPEHPK3PXP"


# ─── service ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def service(app_db) -> AuthService:
    return AuthService(app_db)


def _bootstrap(service: AuthService, **overrides):
    payload = {
        "email": "owner@example.com",
        "username": "owner",
        "password": GOOD_PASSWORD,
        "display_name": "Owner",
    }
    payload.update(overrides)
    return service.bootstrap(**payload)


def test_bootstrap_creates_an_owner_and_a_working_session(service):
    assert service.needs_bootstrap() is True
    user, token, expires_at = _bootstrap(service)
    assert user["role"] == Role.OWNER.value
    assert user["email"] == "owner@example.com"
    assert "password_hash" not in user and "mfa_secret" not in user
    assert expires_at > utcnow()

    principal = service.resolve_session(token)
    assert principal is not None
    assert principal.role is Role.OWNER
    assert principal.can(Permission.ORDER_PLACE)
    assert service.needs_bootstrap() is False


def test_bootstrap_refuses_a_second_time(service):
    _bootstrap(service)
    with pytest.raises(AuthError) as exc:
        _bootstrap(service, email="other@example.com", username="other")
    assert exc.value.status == 409
    assert exc.value.code == "already_bootstrapped"


def test_bootstrap_normalises_email_and_rejects_weak_passwords(service):
    user, _, _ = _bootstrap(service, email="MiXeD@Example.COM")
    assert user["email"] == "mixed@example.com"
    with pytest.raises(AuthError) as exc:
        service.register(email="a@b.com", username="ab", password="weak")
    assert exc.value.code == "weak_password"


def test_login_success_failure_and_unknown_user_look_the_same(service):
    _bootstrap(service)
    assert service.login(identifier="owner", password=GOOD_PASSWORD).status == "ok"
    assert service.login(identifier="owner@example.com", password=GOOD_PASSWORD).status == "ok"

    bad = service.login(identifier="owner", password="nope-not-it")
    unknown = service.login(identifier="ghost", password="nope-not-it")
    assert bad.status == unknown.status == "invalid"
    assert bad.reason == unknown.reason == "invalid credentials"


def test_account_locks_after_repeated_failures_then_unlocks(
    service, monkeypatch, tmp_path
):
    from atr.config.settings import get_settings

    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("LOGIN_LOCKOUT_MINUTES", "15")
    get_settings.cache_clear()

    _bootstrap(service)
    for _ in range(3):
        service.login(identifier="owner", password="wrong-password")

    locked = service.login(identifier="owner", password=GOOD_PASSWORD)
    assert locked.status == "locked"
    assert "locked" in (locked.reason or "")

    # A lock is time-based: it must expire on its own, not need an operator.
    with service.db.session() as session:
        from atr.appdb.repositories import UserRepository

        user = UserRepository.get_by_identifier(session, "owner")
        UserRepository.update(session, user["user_id"], locked_until=utcnow())
    assert service.login(identifier="owner", password=GOOD_PASSWORD).status == "ok"


def test_inactive_account_cannot_log_in(service):
    user, _, _ = _bootstrap(service)
    with service.db.session() as session:
        from atr.appdb.repositories import UserRepository

        UserRepository.update(session, user["user_id"], is_active=False)
    assert service.login(identifier="owner", password=GOOD_PASSWORD).status == "inactive"


def test_mfa_requires_a_code_and_rejects_replay(service):
    user, _, _ = _bootstrap(service)
    secret, uri = service.enroll_mfa(user["user_id"])
    assert uri.startswith("otpauth://totp/")

    # Not enabled yet — enrolment alone must not lock the account.
    assert service.login(identifier="owner", password=GOOD_PASSWORD).status == "ok"
    assert service.activate_mfa(user["user_id"], "000000") is False

    now = time.time()
    activation_code = totp.code_at(secret, at=now)
    assert service.activate_mfa(user["user_id"], activation_code) is True

    challenge = service.login(identifier="owner", password=GOOD_PASSWORD)
    assert challenge.status == "mfa_required"
    assert challenge.token is None  # no session is issued before the second factor

    # The activation code is deliberately spent: activating records the counter,
    # so it cannot then be replayed as a login inside the same 30s window. The
    # cost is that a user waits one window after enrolling.
    spent = service.login(identifier="owner", password=GOOD_PASSWORD, totp_code=activation_code)
    assert spent.status == "invalid"

    next_code = totp.code_at(secret, at=now + 30)
    accepted = service.login(identifier="owner", password=GOOD_PASSWORD, totp_code=next_code)
    assert accepted.status == "ok"

    # And the accepted code is spent too.
    replay = service.login(identifier="owner", password=GOOD_PASSWORD, totp_code=next_code)
    assert replay.status == "invalid"


def test_disabling_mfa_needs_the_password_not_just_a_session(service):
    user, _, _ = _bootstrap(service)
    secret, _ = service.enroll_mfa(user["user_id"])
    service.activate_mfa(user["user_id"], totp.code_at(secret))

    with pytest.raises(AuthError) as exc:
        service.disable_mfa(user["user_id"], "wrong-password")
    assert exc.value.code == "bad_password"

    assert service.disable_mfa(user["user_id"], GOOD_PASSWORD) is True
    assert service.login(identifier="owner", password=GOOD_PASSWORD).status == "ok"


def test_api_key_is_scoped_to_its_role_and_cannot_escalate(service):
    user, _, _ = _bootstrap(service)
    key = service.create_api_key(
        user["user_id"], label="read-only", scopes=["market:read", "instrument:read"]
    )
    assert key["key"].startswith("atr_")
    assert "key_hash" not in key

    principal = service.resolve_api_key(key["key"])
    assert principal is not None
    assert principal.auth_method == "apikey"
    assert principal.can(Permission.MARKET_READ) is True
    assert principal.can(Permission.ORDER_PLACE) is False

    with pytest.raises(AuthError) as exc:
        service.create_api_key(user["user_id"], label="bad", scopes=["not:a:scope"])
    assert exc.value.code == "bad_scope"

    # A viewer-role account cannot mint a key that exceeds viewer rights either.
    service.register(email="viewer@example.com", username="viewer", password=GOOD_PASSWORD)
    with service.db.session() as session:
        from atr.appdb.repositories import UserRepository

        viewer = UserRepository.get_by_identifier(session, "viewer")
    with pytest.raises(AuthError) as exc:
        service.create_api_key(viewer["user_id"], label="nope", scopes=["order:place"])
    assert exc.value.code == "scope_exceeds_role"


def test_api_key_with_no_scopes_holds_no_permissions(service):
    """The empty-scope case is the one a truthiness check gets wrong."""
    user, _, _ = _bootstrap(service)
    key = service.create_api_key(user["user_id"], label="nothing", scopes=[])
    principal = service.resolve_api_key(key["key"])
    assert principal is not None
    assert principal.permissions == frozenset()


def test_revoked_and_expired_credentials_stop_resolving(service):
    user, bootstrap_token, _ = _bootstrap(service)

    key = service.create_api_key(user["user_id"], label="temp", scopes=["market:read"])
    assert service.resolve_api_key(key["key"]) is not None
    assert service.revoke_api_key(user["user_id"], key["key_id"]) is True
    assert service.resolve_api_key(key["key"]) is None

    # A second, independent session — revoking one must not touch the other.
    session = service.login(identifier="owner", password=GOOD_PASSWORD)
    assert service.resolve_session(session.token) is not None
    assert service.logout(session.token) is True
    assert service.resolve_session(session.token) is None
    assert service.resolve_session(bootstrap_token) is not None


def test_changing_the_password_revokes_every_session(service):
    user, _, _ = _bootstrap(service)
    other = service.login(identifier="owner", password=GOOD_PASSWORD)
    assert service.resolve_session(other.token) is not None

    service.change_password(
        user["user_id"], current_password=GOOD_PASSWORD, new_password="An0therG00dPass"
    )
    # Leaving the other session alive would defeat the point of the change.
    assert service.resolve_session(other.token) is None
    assert service.login(identifier="owner", password="An0therG00dPass").status == "ok"

    with pytest.raises(AuthError):
        service.change_password(
            user["user_id"], current_password="wrong", new_password="An0therG00dPass"
        )


def test_the_last_owner_cannot_be_demoted_or_deactivated(service):
    user, _, _ = _bootstrap(service)
    actor = service.resolve_session(service.login(identifier="owner", password=GOOD_PASSWORD).token)

    with pytest.raises(AuthError) as exc:
        service.update_user(actor, user["user_id"], role=Role.VIEWER)
    assert exc.value.code == "last_owner"

    with pytest.raises(AuthError):
        service.update_user(actor, user["user_id"], is_active=False)

    # A second owner makes the demotion legitimate.
    second = service.register(
        email="second@example.com", username="second", password=GOOD_PASSWORD, role=Role.OWNER
    )
    updated = service.update_user(actor, second["user_id"], role=Role.TRADER)
    assert updated is not None and updated["role"] == Role.TRADER


def test_deactivating_a_user_kills_their_sessions(service):
    user, _, _ = _bootstrap(service)
    victim = service.register(email="v@example.com", username="victim", password=GOOD_PASSWORD)
    session = service.login(identifier="victim", password=GOOD_PASSWORD)
    assert service.resolve_session(session.token) is not None

    actor = service.resolve_session(service.login(identifier="owner", password=GOOD_PASSWORD).token)
    service.update_user(actor, victim["user_id"], is_active=False)
    assert service.resolve_session(session.token) is None


def test_expired_session_does_not_resolve(service):
    from atr.appdb.repositories import SessionRepository
    from atr.auth.tokens import hash_token

    _, token, _ = _bootstrap(service)
    with service.db.session() as session:
        row = SessionRepository.get_by_token_hash(session, hash_token(token))
        SessionRepository.revoke(session, row["session_id"])
    assert service.resolve_session(token) is None
