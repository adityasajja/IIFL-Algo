"""Nightly backup of everything that cannot be rebuilt.

The price history is left out on purpose: it can be downloaded again. What is kept is the
app database (users, watchlists, paper runs, audit) and the state and study folders under
``data/``. The database is copied with SQLite's own backup call, because copying the file
while the server is writing to it (WAL mode) can produce a corrupt copy.
"""

from __future__ import annotations

import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from loguru import logger

#: Folders and files under ``data/`` worth keeping. Screenshots and price history are not.
KEEP = (
    "audit",
    "alerts",
    "insights",
    "portfolio",
    "paper_momentum",
    "self_learning",
    "signals",
    "trade_signals",
    "universe",
    "eod_refresh.json",
)
PREFIX = "atr-backup-"


def _copy_database(source: Path, target: Path) -> None:
    """A consistent copy of a live SQLite database."""
    # sqlite3's own context manager commits but does not close, which locks the file on Windows.
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def _add_tree(archive: zipfile.ZipFile, root: Path, relative: str) -> None:
    path = root / relative
    if path.is_file():
        archive.write(path, relative)
    elif path.is_dir():
        for file in path.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(root).as_posix())


def prune(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` backups. Returns what was removed."""
    backups = sorted(directory.glob(f"{PREFIX}*.zip"))
    stale = backups[: max(len(backups) - keep, 0)]
    for path in stale:
        path.unlink(missing_ok=True)
    return stale


def backup_now(
    data_root: Path,
    destination: Path,
    *,
    keep: int = 14,
    database: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Write one dated zip into ``destination`` and drop the oldest beyond ``keep``."""
    data_root, destination = Path(data_root), Path(destination)
    database = Path(database) if database else data_root / "app.db"
    destination.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = destination / f"{PREFIX}{stamp}.zip"
    partial = target.with_suffix(".zip.partial")

    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
        if database.exists():
            copy = Path(tmp) / "app.db"
            _copy_database(database, copy)
            archive.write(copy, "app.db")
        for name in KEEP:
            _add_tree(archive, data_root, name)
    partial.replace(target)  # a half-written zip never carries the final name

    removed = prune(destination, keep)
    logger.info("backup written: {} ({} old removed)", target.name, len(removed))
    return target


def restore_backup(archive_path: Path, data_root: Path, *, force: bool = False) -> Path:
    """Put a backup back under ``data_root``. The server must be stopped.

    Refuses to overwrite an existing database unless ``force``; with ``force`` the current one is
    first moved aside as ``app.db.before-restore`` so a wrong restore can be undone. Returns the
    restored database path.
    """
    archive_path, data_root = Path(archive_path), Path(data_root)
    if not archive_path.exists():
        raise FileNotFoundError(f"no such backup: {archive_path}")
    db = data_root / "app.db"
    with zipfile.ZipFile(archive_path) as archive:
        bad = archive.testzip()
        if bad is not None:
            raise ValueError(f"backup is corrupt (first bad file: {bad})")
        names = archive.namelist()
        if any(n.startswith(("/", "..")) or ".." in Path(n).parts for n in names):
            raise ValueError("backup contains unsafe paths")
        if "app.db" not in names:
            raise ValueError("backup has no app.db")
        if db.exists():
            if not force:
                raise FileExistsError(f"{db} exists; stop the server and pass force to replace it")
            db.replace(db.with_name("app.db.before-restore"))
            for suffix in ("-wal", "-shm"):
                stale = db.with_name(db.name + suffix)
                if stale.exists():
                    stale.unlink()
        data_root.mkdir(parents=True, exist_ok=True)
        archive.extractall(data_root)
    logger.info("restored {} into {}", archive_path.name, data_root)
    return db
