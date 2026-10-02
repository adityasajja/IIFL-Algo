"""One columnar file in place of thousands of small ones.

The daily cache is one parquet per symbol: ~3,000 files holding ~1.6M rows in
total. Reading them one by one costs ~5s no matter how it is threaded, because
the cost is per-file overhead, not data. The same rows in a single file load in
~0.1s.

The per-symbol files stay the source of truth: every writer (history sync, the
end-of-day refresh, gap repair) still writes them. This module keeps a
consolidated copy, ``data/panel/<name>.parquet``, current with them:

* a manifest records each source file's (mtime, size) as of the last build;
* on every access the directory is stat-ed (~25ms for 3,000 files) and compared
  with the manifest, so a changed file is always noticed;
* only changed files are re-read, so a refresh that touched ten symbols costs ten
  reads, not three thousand.

The in-process frames are kept between calls and updated per symbol, so a
repeated read while nothing changed is a dictionary lookup.
"""

from __future__ import annotations

import json
import os
import threading
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
from loguru import logger

PANEL_ROOT = Path("data/panel")
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
_NUMERIC = COLUMNS[1:]
_VERSION = 1  # bump to force every panel to rebuild


class _Entry:
    def __init__(self) -> None:
        self.state: dict[str, tuple[int, int]] = {}
        self.frames: dict[str, pd.DataFrame] = {}
        self.lock = threading.Lock()
        self.scanned_at = 0.0  # monotonic time of the last directory scan


_ENTRIES: dict[str, _Entry] = {}
_ENTRIES_LOCK = threading.Lock()


def _entry(key: str) -> _Entry:
    with _ENTRIES_LOCK:
        return _ENTRIES.setdefault(key, _Entry())


def _scan(source: Path) -> dict[str, tuple[int, int]]:
    """{stem: (mtime_ns, size)} for every parquet in the directory."""
    out: dict[str, tuple[int, int]] = {}
    try:
        with os.scandir(source) as it:
            for e in it:
                if e.name.endswith(".parquet") and not e.name.startswith("."):
                    st = e.stat()
                    out[e.name[:-8]] = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        pass
    return out


@lru_cache(maxsize=32)
def _paths_for(source_str: str) -> tuple[str, Path, Path]:
    # resolve() is a syscall per path component on Windows; it is called once per
    # lookup in loops of hundreds, so it is memoised on the string it was given.
    resolved = Path(source_str).resolve()
    name = "_".join(resolved.parts[-2:]).replace(":", "").replace("\\", "_")
    return str(resolved), PANEL_ROOT / f"{name}.parquet", PANEL_ROOT / f"{name}.json"


def _paths(source: Path) -> tuple[str, Path, Path]:
    return _paths_for(str(source))


def _read_file(path: Path) -> pl.DataFrame | None:
    try:
        df = pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - a corrupt file just drops out
        logger.debug("panel: cannot read {}: {}", path, exc)
        return None
    if "ts" not in df.columns:
        return None
    keep = [c for c in COLUMNS if c in df.columns]
    df = df.select(keep)
    for c in _NUMERIC:
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    return df.select(COLUMNS).with_columns(pl.col(_NUMERIC).cast(pl.Float64))


def _read_many(source: Path, stems: list[str]) -> dict[str, pl.DataFrame]:
    from concurrent.futures import ThreadPoolExecutor

    def one(stem: str):
        return stem, _read_file(source / f"{stem}.parquet")

    out: dict[str, pl.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for stem, df in pool.map(one, stems):
            if df is not None and len(df):
                out[stem] = df
    return out


def _split(panel: pl.DataFrame) -> dict[str, pd.DataFrame]:
    """Per-symbol pandas frames from a panel sorted by (symbol, ts).

    Converting the panel once and slicing is ~10x faster than converting 3,000
    small polars frames, each of which pays a fixed arrow round trip.
    """
    if panel.is_empty():
        return {}
    syms = panel["symbol"].to_numpy()
    cuts = np.flatnonzero(syms[1:] != syms[:-1]) + 1
    starts = np.r_[0, cuts]
    ends = np.r_[cuts, len(syms)]
    cols = {c: panel[c].to_numpy() for c in COLUMNS}
    return {
        str(syms[s]): pd.DataFrame({c: v[s:e] for c, v in cols.items()})
        for s, e in zip(starts, ends)
    }


def _write_panel(panel: pl.DataFrame, state: dict[str, tuple[int, int]], pq: Path, mf: Path) -> None:
    PANEL_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = pq.with_suffix(".tmp")
    panel.write_parquet(tmp)
    os.replace(tmp, pq)
    mtmp = mf.with_suffix(".tmp")
    mtmp.write_text(json.dumps({"v": _VERSION, "files": {k: list(v) for k, v in state.items()}}), encoding="utf8")
    os.replace(mtmp, mf)  # manifest last: a crash between the two just forces a rebuild


#: A directory is re-stat-ed at most this often. Callers fetch one symbol at a time
#: in loops of hundreds; without this each lookup paid a full scan of ~3,000 files.
SCAN_INTERVAL_S = 2.0


def frames(source: str | Path, *, max_age: float | None = None) -> dict[str, pd.DataFrame]:
    """Every symbol's frame for a cache directory, current to within ``max_age`` seconds.

    ``max_age=0`` forces a fresh scan (use after writing files). The returned dict
    and its frames are shared: treat them as read-only, or copy.
    """
    source = Path(source)
    key, pq, mf = _paths(source)
    entry = _entry(key)
    with entry.lock:
        t0 = time.monotonic()
        age = SCAN_INTERVAL_S if max_age is None else max_age
        if entry.frames and (t0 - entry.scanned_at) < age:
            return entry.frames
        current = _scan(source)
        entry.scanned_at = time.monotonic()

        if entry.frames and entry.state == current:
            return entry.frames

        if entry.frames:
            # Warm process: patch only what changed.
            changed = [s for s, v in current.items() if entry.state.get(s) != v]
            gone = [s for s in entry.state if s not in current]
            fresh = _read_many(source, changed)
            for s in gone:
                entry.frames.pop(s, None)
            for s in changed:
                if s in fresh:
                    entry.frames[s] = fresh[s].to_pandas()
                else:
                    entry.frames.pop(s, None)
            entry.state = current
            if changed or gone:
                _persist_async(source, current, entry)
            logger.debug("panel: patched {} changed / {} removed in {:.2f}s", len(changed), len(gone), time.monotonic() - t0)
            return entry.frames

        # Cold process: use the stored panel where it still matches.
        stored: dict[str, tuple[int, int]] = {}
        panel: pl.DataFrame | None = None
        try:
            meta = json.loads(mf.read_text(encoding="utf8"))
            if meta.get("v") == _VERSION and pq.exists():
                stored = {k: (v[0], v[1]) for k, v in meta["files"].items()}
                panel = pl.read_parquet(pq)
        except Exception:  # noqa: BLE001 - missing or corrupt: rebuild
            stored, panel = {}, None

        if panel is None:
            changed = list(current)
            kept = pl.DataFrame()
        else:
            changed = [s for s, v in current.items() if stored.get(s) != v]
            gone = {s for s in stored if s not in current}
            drop = set(changed) | gone
            kept = panel.filter(~pl.col("symbol").is_in(list(drop))) if drop else panel

        if changed:
            fresh = _read_many(source, changed)
            parts = [df.with_columns(pl.lit(s).alias("symbol")) for s, df in fresh.items()]
            if parts:
                new = pl.concat(parts).select(["symbol", *COLUMNS])
                kept = new if kept.is_empty() else pl.concat([kept, new])
            panel = kept.sort(["symbol", "ts"])
            _write_panel(panel, current, pq, mf)
            logger.info("panel: rebuilt {} ({} of {} files re-read) in {:.1f}s", pq.name, len(changed), len(current), time.monotonic() - t0)
        else:
            panel = kept

        entry.frames = _split(panel)
        entry.state = current
        return entry.frames


def _persist_async(source: Path, current: dict, entry: _Entry) -> None:
    """Rewrite the stored panel in the background after an in-process patch."""

    def work() -> None:
        try:
            with entry.lock:
                parts = [
                    pl.from_pandas(df).with_columns(pl.lit(s).alias("symbol")).select(["symbol", *COLUMNS])
                    for s, df in entry.frames.items()
                ]
                state = dict(entry.state)
            if not parts:
                return
            _, pq, mf = _paths(source)
            _write_panel(pl.concat(parts).sort(["symbol", "ts"]), state, pq, mf)
        except Exception as exc:  # noqa: BLE001 - next cold start just rebuilds
            logger.debug("panel persist failed: {}", exc)

    threading.Thread(target=work, name="atr-panel-persist", daemon=True).start()


def clear() -> None:
    """Forget in-process frames (tests)."""
    with _ENTRIES_LOCK:
        _ENTRIES.clear()
