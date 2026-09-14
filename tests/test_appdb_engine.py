"""Transaction semantics of the control-plane engine.

One bug lives here, and it is the kind that hides: with pysqlite's default
transaction handling, releasing the *outermost* savepoint commits immediately.
A caller that wrote something inside a savepoint and then failed a later step
would find that the savepoint's write had survived its own ``rollback()`` — a
partial write presented as a whole one.

This is worth a test file of its own because the symptom is invisible in the
happy path and only shows up as an inconsistency between two tables that are
supposed to agree.
"""

from __future__ import annotations

from sqlalchemy import text

from atr.appdb.engine import AppDatabase


def _seed(db: AppDatabase) -> None:
    with db.session() as session:
        session.execute(text("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, v TEXT)"))


def test_a_released_savepoint_is_undone_by_a_later_rollback(app_db):
    """The regression: a committed savepoint must not survive the caller's rollback.

    Shape of the failure this pins — the order service writes a ``VALIDATING``
    event inside a savepoint and *then* asks the risk gate. If the gate raises,
    the whole transaction must go, or the order sits at ``NEW`` while its log
    claims a transition that ``orders.status`` knows nothing about.
    """
    _seed(app_db)
    with app_db.engine.begin() as conn:
        conn.execute(text("DELETE FROM probe"))

    try:
        with app_db.session() as session:
            # 1. A savepoint that succeeds and is released.
            with session.begin_nested():
                session.execute(text("INSERT INTO probe VALUES (1, 'provisional')"))
            # 2. A later statement in the same logical transaction.
            session.execute(text("INSERT INTO probe VALUES (2, 'later')"))
            # 3. The caller fails and the session rolls back.
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    with app_db.engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM probe ORDER BY id")).fetchall()
    assert rows == [], (
        "a write made inside a savepoint survived the caller's rollback — "
        "pysqlite is treating the released savepoint as a commit"
    )


def test_a_committed_savepoint_still_persists_when_the_caller_commits(app_db):
    """The fix must not break the normal path: savepoint + commit still writes."""
    _seed(app_db)
    with app_db.engine.begin() as conn:
        conn.execute(text("DELETE FROM probe"))

    with app_db.session() as session, session.begin_nested():
        session.execute(text("INSERT INTO probe VALUES (7, 'kept')"))

    with app_db.engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM probe")).fetchall()
    assert rows == [(7,)]


def test_a_failed_savepoint_does_not_poison_the_transaction(app_db):
    """The savepoint's other job: let the caller recover and carry on."""
    _seed(app_db)
    from sqlalchemy.exc import IntegrityError

    with app_db.session() as session:
        session.execute(text("INSERT INTO probe VALUES (1, 'first')"))
        try:
            with session.begin_nested():
                session.execute(text("INSERT INTO probe VALUES (1, 'duplicate')"))
        except IntegrityError:
            pass
        # The session is still usable and the first row is still pending.
        session.execute(text("INSERT INTO probe VALUES (2, 'second')"))

    with app_db.engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM probe ORDER BY id")).fetchall()
    assert rows == [(1,), (2,)]


def test_foreign_keys_are_enforced(app_db):
    """``PRAGMA foreign_keys`` is OFF by default, which would disable every CASCADE."""
    from sqlalchemy.exc import IntegrityError

    with app_db.session() as session:
        session.execute(
            text("CREATE TABLE IF NOT EXISTS child (id INTEGER PRIMARY KEY, "
                 "parent TEXT REFERENCES users(user_id) ON DELETE CASCADE)")
        )

    import pytest

    with pytest.raises(IntegrityError), app_db.session() as session:
        session.execute(text("INSERT INTO child VALUES (1, 'no-such-user')"))
