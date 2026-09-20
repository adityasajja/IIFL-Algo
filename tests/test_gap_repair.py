"""Filling a missing day must never overwrite a bar the file already has."""

from datetime import date

import pandas as pd

from atr.data import gap_repair as gr


def _frame(days, closes):
    ts = [pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=15) for d in days]
    return pd.DataFrame({"ts": ts, "open": closes, "high": closes, "low": closes, "close": closes, "volume": [100] * len(days)}).astype({"volume": "int64"})


def test_only_sessions_most_names_traded_count_and_only_after_a_stocks_first_bar():
    every = {date(2026, 9, d) for d in (14, 15, 16, 17, 18)}
    dates = {"A": every, "B": every - {date(2026, 9, 17)}, "C": every, "NEW": {date(2026, 9, 17), date(2026, 9, 18)}}

    out = gr.missing_sessions(dates)

    assert out == {"B": [date(2026, 9, 17)]}  # NEW simply had not listed yet, which is not a hole


class _Client:
    def __init__(self, candles):
        self.candles, self.calls = candles, []

    def historical_data(self, exchange, conid, interval, start, end):
        self.calls.append((conid, start, end))
        return {"result": [{"candles": self.candles}]}


def _setup(tmp_path, monkeypatch, candles, *, present):
    folder = tmp_path / "iifl_daily" / "NSEEQ"
    folder.mkdir(parents=True)
    days = ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
    for name in ("AAA-EQ", "BBB-EQ", "CCC-EQ"):
        _frame(days, [10.0, 11.0, 12.0, 13.0]).to_parquet(folder / f"{name}.parquet", index=False)
    holed = [d for d in days if d != "2026-09-17"]
    _frame(holed, [20.0, 21.0, 23.0]).to_parquet(folder / "BBB-EQ.parquet", index=False)

    import atr.brokers.iifl.contracts as contracts
    import atr.scanner as scanner

    class _Master:
        def __init__(self, client):
            pass

        def load_cached(self, exchanges):
            pass

    monkeypatch.setattr(contracts, "InstrumentMaster", _Master)
    monkeypatch.setattr(scanner, "resolve_conid", lambda master, symbol, exchange: 1234)
    return folder, _Client(candles)


def test_a_missing_session_is_inserted_and_existing_bars_are_left_alone(tmp_path, monkeypatch):
    candles = [["2026-09-17T09:15:00", 22.0, 22.5, 21.5, 22.0, 900],
               ["2026-09-18T09:15:00", 999.0, 999.0, 999.0, 999.0, 1]]  # a different Friday: must not overwrite
    folder, client = _setup(tmp_path, monkeypatch, candles, present=None)

    out = gr.repair(client, tmp_path, ["AAA-EQ", "BBB-EQ", "CCC-EQ"])

    df = pd.read_parquet(folder / "BBB-EQ.parquet").set_index(pd.to_datetime(pd.read_parquet(folder / "BBB-EQ.parquet")["ts"]).dt.date.astype(str))
    assert out["inserted"] == 1 and out["symbols"] == 1
    assert df.loc["2026-09-17", "close"] == 22.0  # filled
    assert df.loc["2026-09-18", "close"] == 23.0  # the existing bar, untouched
    assert str(df["volume"].dtype) == "int64"  # dtypes preserved for the readers


def test_two_different_candles_for_one_missing_day_are_not_guessed_between(tmp_path, monkeypatch):
    candles = [["2026-09-17T09:15:00", 22.0, 22.5, 21.5, 22.0, 900], ["2026-09-17T09:15:00", 22.0, 22.5, 21.5, 21.0, 800]]
    folder, client = _setup(tmp_path, monkeypatch, candles, present=None)

    out = gr.repair(client, tmp_path, ["AAA-EQ", "BBB-EQ", "CCC-EQ"])

    assert out["inserted"] == 0 and out["skipped"] == 1
    assert len(pd.read_parquet(folder / "BBB-EQ.parquet")) == 3  # unchanged


def test_a_broker_error_is_counted_not_raised(tmp_path, monkeypatch):
    class _Refusing(_Client):
        def historical_data(self, *a):
            return {"result": [{"status": "EC500", "message": "IP address not authorized"}]}

    folder, _ = _setup(tmp_path, monkeypatch, [], present=None)

    out = gr.repair(_Refusing([]), tmp_path, ["AAA-EQ", "BBB-EQ", "CCC-EQ"])

    assert out["failed"] == 1 and out["inserted"] == 0


def test_a_holding_with_no_price_file_is_seeded_into_its_own_folder_not_the_scanners(tmp_path, monkeypatch):
    (tmp_path / "iifl_daily" / "NSEEQ").mkdir(parents=True)
    days = pd.bdate_range("2026-08-03", periods=30)
    candles = [[f"{d.date()}T09:15:00", 10.0 + i, 11.0 + i, 9.0 + i, 10.5 + i, 100] for i, d in enumerate(days)]
    import atr.brokers.iifl.contracts as contracts
    import atr.scanner as scanner

    class _Master:
        def __init__(self, client):
            pass

        def load_cached(self, exchanges):
            pass

    monkeypatch.setattr(contracts, "InstrumentMaster", _Master)
    monkeypatch.setattr(scanner, "resolve_conid", lambda master, symbol, exchange: 1)

    out = gr.seed_missing(_Client(candles), tmp_path, ["VCL-BE-EQ"])

    assert out == {"seeded": 1, "failed": 0}
    assert (tmp_path / "iifl_daily" / gr.HELD_FOLDER / "VCL-BE-EQ.parquet").exists()
    assert not list((tmp_path / "iifl_daily" / "NSEEQ").glob("*.parquet"))  # the scanners' folder stays untouched
    assert gr.seed_missing(_Client(candles), tmp_path, ["VCL-BE-EQ"]) == {"seeded": 0, "failed": 0}  # and it is not redone
