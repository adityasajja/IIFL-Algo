import sqlite3

import pytest

from atr.infra.backup import backup_now, restore_backup


def _make_db(path, value):
    with sqlite3.connect(path) as c:
        c.execute("create table t(v text)")
        c.execute("insert into t values (?)", (value,))


def _value(path):
    with sqlite3.connect(path) as c:
        return c.execute("select v from t").fetchone()[0]


def test_roundtrip_and_refuses_overwrite_without_force(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    _make_db(root / "app.db", "original")
    zip_path = backup_now(root, tmp_path / "bk")
    (root / "app.db").unlink()
    restore_backup(zip_path, root)
    assert _value(root / "app.db") == "original"
    with pytest.raises(FileExistsError):
        restore_backup(zip_path, root)
    restore_backup(zip_path, root, force=True)
    assert (root / "app.db.before-restore").exists()


def test_missing_and_bad_archive(tmp_path):
    with pytest.raises(FileNotFoundError):
        restore_backup(tmp_path / "nope.zip", tmp_path)
