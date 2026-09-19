"""The public top-up must only append, keep dtypes, and refuse suspicious bars."""

from datetime import datetime, timedelta, timezone

import pandas as pd

from atr.data import eod_refresh as eod

IST = timezone(timedelta(hours=5, minutes=30))
AFTER_CLOSE = datetime(2026, 9, 18, 17, 0, tzinfo=IST)  # a Friday
BEFORE_CLOSE = datetime(2026, 9, 18, 11, 0, tzinfo=IST)


def _stored(days: list[str], close: float = 100.0) -> pd.DataFrame:
    ts = pd.to_datetime(days) + pd.Timedelta(hours=9, minutes=15)
    return pd.DataFrame(
        {"ts": ts, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": [1000] * len(ts)}
    ).astype({"volume": "int64"})


def _fresh(days: list[str], close: float) -> pd.DataFrame:
    ts = pd.to_datetime(days) + pd.Timedelta(hours=9, minutes=15)
    return pd.DataFrame(
        {"ts": ts, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": [2000.0] * len(ts)}
    )


def test_appends_only_newer_bars_and_keeps_dtypes(tmp_path):
    path = tmp_path / "AAA-EQ.parquet"
    _stored(["2026-09-10", "2026-09-11"]).to_parquet(path, index=False)

    out = eod.append_new_bars(path, _fresh(["2026-09-11", "2026-09-14", "2026-09-15"], 101.0))

    assert out == "ok"
    df = pd.read_parquet(path)
    assert list(df["ts"].dt.strftime("%Y-%m-%d")) == ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"]
    assert df["volume"].dtype == "int64"  # not promoted to float by the public feed
    assert df["ts"].dt.hour.eq(9).all()
    # The stored bar for the overlapping day is untouched.
    assert df.loc[1, "close"] == 100.0


def test_second_run_is_a_no_op(tmp_path):
    path = tmp_path / "AAA-EQ.parquet"
    _stored(["2026-09-11"]).to_parquet(path, index=False)
    fresh = _fresh(["2026-09-14"], 101.0)
    assert eod.append_new_bars(path, fresh) == "ok"
    assert eod.append_new_bars(path, fresh) == "current"
    assert len(pd.read_parquet(path)) == 2


def test_a_price_jump_is_refused_and_the_file_left_alone(tmp_path):
    path = tmp_path / "AAA-EQ.parquet"
    _stored(["2026-09-11"], close=1000.0).to_parquet(path, index=False)
    before = pd.read_parquet(path)

    assert eod.append_new_bars(path, _fresh(["2026-09-14"], 500.0)) == "suspect"  # looks like a split
    pd.testing.assert_frame_equal(pd.read_parquet(path), before)


def test_an_unreadable_file_is_reported_not_raised(tmp_path):
    path = tmp_path / "AAA-EQ.parquet"
    path.write_bytes(b"not parquet")
    assert eod.append_new_bars(path, _fresh(["2026-09-14"], 100.0)) == "fail"


def test_todays_bar_is_dropped_until_after_the_close(monkeypatch):
    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            days = pd.to_datetime(["2026-09-17", "2026-09-18"])
            secs = ((days - pd.Timestamp("1970-01-01")).total_seconds() - 19800).astype(int).tolist()
            q = {k: [10.0, 11.0] for k in ("open", "high", "low", "close")} | {"volume": [1, 2]}
            return {"chart": {"result": [{"timestamp": secs, "meta": {"gmtoffset": 19800}, "indicators": {"quote": [q]}}]}}

    monkeypatch.setattr(eod.httpx, "get", lambda *a, **k: Resp())

    early = eod.fetch_recent("AAA-EQ", now=BEFORE_CLOSE)
    late = eod.fetch_recent("AAA-EQ", now=AFTER_CLOSE)

    assert list(early["ts"].dt.strftime("%Y-%m-%d")) == ["2026-09-17"]
    assert list(late["ts"].dt.strftime("%Y-%m-%d")) == ["2026-09-17", "2026-09-18"]


def test_refresh_counts_outcomes_and_survives_a_failing_fetch(tmp_path, monkeypatch):
    root = eod.cache_dir(tmp_path)
    root.mkdir(parents=True)
    for stem in ("AAA-EQ", "BBB-EQ"):
        _stored(["2026-09-11"]).to_parquet(root / f"{stem}.parquet", index=False)

    def fake_fetch(stem, now=None):
        if stem == "BBB-EQ":
            raise RuntimeError("rate limited")
        return _fresh(["2026-09-14"], 101.0)

    monkeypatch.setattr(eod, "fetch_recent", fake_fetch)

    summary = eod.refresh(tmp_path, workers=2, now=AFTER_CLOSE)

    assert (summary["ok"], summary["fail"]) == (1, 1)
    assert eod.read_state(tmp_path)["ran_date_ist"] == "2026-09-18"


def test_due_once_per_weekday_after_the_close(tmp_path):
    yesterday = {"ran_date_ist": "2026-09-17"}
    (tmp_path / "eod_refresh.json").write_text(__import__("json").dumps(yesterday))

    assert eod.due(tmp_path, BEFORE_CLOSE) is False  # not yet
    assert eod.due(tmp_path, AFTER_CLOSE) is True
    (tmp_path / "eod_refresh.json").write_text(__import__("json").dumps({"ran_date_ist": "2026-09-18"}))
    assert eod.due(tmp_path, AFTER_CLOSE) is False  # already done today
    saturday = datetime(2026, 9, 19, 17, 0, tzinfo=IST)
    (tmp_path / "eod_refresh.json").write_text(__import__("json").dumps({"ran_date_ist": "2026-09-18"}))
    assert eod.due(tmp_path, saturday) is False  # no session on weekends


def test_bare_tickers_find_their_series_files(tmp_path, monkeypatch):
    root = eod.cache_dir(tmp_path)
    root.mkdir(parents=True)
    _stored(["2026-09-11"]).to_parquet(root / "AAA-EQ.parquet", index=False)
    monkeypatch.setattr(eod, "fetch_recent", lambda stem, now=None: _fresh(["2026-09-14"], 101.0))

    summary = eod.refresh(tmp_path, ["AAA", "ZZZ"], workers=1, now=AFTER_CLOSE)

    assert (summary["ok"], summary["missing"]) == (1, 1)
