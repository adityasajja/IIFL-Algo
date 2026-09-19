"""A backup must hold a consistent database and the state folders, and never grow forever."""

import sqlite3
import zipfile
from datetime import datetime

from atr.infra.backup import PREFIX, backup_now, prune


def _make_data(root):
    root.mkdir(parents=True)
    db = sqlite3.connect(root / "app.db")
    db.execute("create table t (x integer)")
    db.executemany("insert into t values (?)", [(i,) for i in range(50)])
    db.commit()
    db.close()
    (root / "insights").mkdir()
    (root / "insights" / "state.json").write_text("{}")
    (root / "iifl_daily").mkdir()
    (root / "iifl_daily" / "big.parquet").write_bytes(b"x" * 100)  # rebuildable, must be left out
    return root


def test_backup_holds_the_database_and_state_but_not_prices(tmp_path):
    data = _make_data(tmp_path / "data")

    out = backup_now(data, tmp_path / "out", now=datetime(2026, 9, 19, 2, 0, 0))

    assert out.name == f"{PREFIX}20260919-020000.zip"
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"app.db", "insights/state.json"} <= names
        assert not any(n.startswith("iifl_daily") for n in names)
        z.extract("app.db", tmp_path / "restored")
    restored = sqlite3.connect(tmp_path / "restored" / "app.db")
    assert restored.execute("select count(*) from t").fetchone()[0] == 50
    restored.close()


def test_only_the_newest_backups_are_kept(tmp_path):
    data = _make_data(tmp_path / "data")
    for minute in range(5):
        backup_now(data, tmp_path / "out", keep=3, now=datetime(2026, 9, 19, 2, minute, 0))

    kept = sorted(p.name for p in (tmp_path / "out").glob(f"{PREFIX}*.zip"))
    assert kept == [f"{PREFIX}20260919-02{m:02d}00.zip" for m in (2, 3, 4)]


def test_no_partial_file_is_left_behind(tmp_path):
    data = _make_data(tmp_path / "data")
    backup_now(data, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.partial"))


def test_prune_on_an_empty_folder_is_a_no_op(tmp_path):
    assert prune(tmp_path, 3) == []
