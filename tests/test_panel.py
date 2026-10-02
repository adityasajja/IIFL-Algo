"""The consolidated panel must always agree with the per-symbol files it summarises."""

import os
import time

import pandas as pd
import pytest

from atr.data import panel


def _write(path, closes, start="2026-01-01"):
    n = len(closes)
    pd.DataFrame(
        {
            "ts": pd.date_range(start, periods=n, freq="D"),
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [100] * n,  # int on purpose: the panel stores floats
        }
    ).to_parquet(path, index=False)


@pytest.fixture()
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "PANEL_ROOT", tmp_path / "panel")
    panel.clear()
    panel._paths_for.cache_clear()
    src = tmp_path / "iifl_daily" / "NSEEQ"
    src.mkdir(parents=True)
    _write(src / "AAA-EQ.parquet", [10.0, 11.0, 12.0])
    _write(src / "BBB-EQ.parquet", [20.0, 21.0])
    yield src
    panel.clear()
    panel._paths_for.cache_clear()


def test_frames_match_the_files(source):
    frames = panel.frames(source)
    assert set(frames) == {"AAA-EQ", "BBB-EQ"}
    pd.testing.assert_frame_equal(
        frames["AAA-EQ"].reset_index(drop=True),
        pd.read_parquet(source / "AAA-EQ.parquet").astype({"volume": "float64"}),
    )


def test_a_cold_process_reads_the_stored_panel_and_still_notices_changes(source):
    panel.frames(source)
    panel.clear()  # a new process: nothing in memory, panel file on disk
    assert len(panel.frames(source)["BBB-EQ"]) == 2

    time.sleep(0.01)
    _write(source / "BBB-EQ.parquet", [20.0, 21.0, 22.0, 23.0])
    panel.clear()
    assert len(panel.frames(source)["BBB-EQ"]) == 4  # the stored copy was stale


def test_a_warm_process_picks_up_new_files_and_removals(source):
    panel.frames(source)
    _write(source / "CCC-EQ.parquet", [5.0, 6.0])
    os.remove(source / "AAA-EQ.parquet")
    frames = panel.frames(source, max_age=0)
    assert set(frames) == {"BBB-EQ", "CCC-EQ"}


def test_scans_are_rate_limited(source, monkeypatch):
    panel.frames(source)
    calls = []
    real = panel._scan
    monkeypatch.setattr(panel, "_scan", lambda s: calls.append(1) or real(s))
    for _ in range(50):
        panel.frames(source)
    assert calls == []  # within the interval nothing re-stats the directory
    panel.frames(source, max_age=0)
    assert calls == [1]


def test_load_cached_returns_copies_callers_may_modify(source, monkeypatch):
    from atr.data import history

    monkeypatch.setattr(history, "CACHE_ROOT", source.parent)
    first = history.load_cached("NSEEQ")
    first["AAA-EQ"]["close"] = -1.0
    assert history.load_cached("NSEEQ")["AAA-EQ"]["close"].iloc[0] == 10.0
