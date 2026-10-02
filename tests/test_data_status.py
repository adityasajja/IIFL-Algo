"""Data freshness: what "current" means, and that an old cache is called old."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
from fastapi.testclient import TestClient

from atr.api.main import app
from atr.market_calendar import IST, NSEMarketCalendar
from atr.services.data_status import build_status, classify, expected_session, sessions_behind

CAL = NSEMarketCalendar(holidays=set())


def at(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=IST)


class TestExpectedSession:
    def test_before_the_bar_is_final_it_is_the_previous_trading_day(self):
        assert expected_session(at(2026, 7, 8, 11), CAL) == date(2026, 7, 7)  # Wed 11:00 -> Tue

    def test_after_the_close_it_is_today(self):
        assert expected_session(at(2026, 7, 8, 17), CAL) == date(2026, 7, 8)

    def test_a_monday_morning_expects_friday(self):
        assert expected_session(at(2026, 7, 6, 9), CAL) == date(2026, 7, 3)

    def test_a_weekend_expects_friday(self):
        assert expected_session(at(2026, 7, 4, 20), CAL) == date(2026, 7, 3)

    def test_a_holiday_is_skipped(self):
        cal = NSEMarketCalendar(holidays={date(2026, 7, 7)})
        assert expected_session(at(2026, 7, 8, 11), cal) == date(2026, 7, 6)


class TestBehind:
    def test_current(self):
        assert sessions_behind(date(2026, 7, 7), date(2026, 7, 7), CAL) == 0

    def test_weekends_are_not_sessions(self):
        assert sessions_behind(date(2026, 7, 3), date(2026, 7, 6), CAL) == 1  # Fri -> Mon is one session

    def test_classification(self):
        assert [classify(x) for x in (0, 1, 2, 9, None)] == ["fresh", "late", "stale", "stale", "missing"]


def _write(path, last: date, n=30):
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = pd.bdate_range(end=pd.Timestamp(last), periods=n)
    pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}).to_parquet(path)


class TestDemo:
    def test_a_marked_directory_is_reported_as_demo(self, tmp_path):
        assert build_status(tmp_path, at(2026, 7, 8, 11), CAL)["demo"] is False
        (tmp_path / ".demo").write_text("x")
        assert build_status(tmp_path, at(2026, 7, 8, 11), CAL)["demo"] is True


class TestBuild:
    def test_an_empty_data_root_says_missing_not_fresh(self, tmp_path):
        out = build_status(tmp_path, at(2026, 7, 8, 11), CAL)
        assert out["overall"] == "missing"
        assert {s["status"] for s in out["sources"]} == {"missing"}
        assert all(s["as_of"] is None for s in out["sources"])

    def test_reports_each_source_with_its_age(self, tmp_path):
        _write(tmp_path / "iifl_daily" / "NSEEQ" / "RELIANCE-EQ.parquet", date(2026, 7, 7))
        _write(tmp_path / "iifl_daily" / "INDICES" / "NIFTY50.parquet", date(2026, 7, 3))
        (tmp_path / "iifl_daily" / "INDICES" / "NIFTY50.source").write_text("Yahoo public bars")
        out = build_status(tmp_path, at(2026, 7, 8, 11), CAL)
        prices, index = out["sources"]
        assert out["expected_session"] == "2026-07-07"
        assert (prices["status"], prices["sessions_behind"]) == ("fresh", 0)
        assert (index["status"], index["sessions_behind"]) == ("stale", 2)
        assert index["origin"] == "Yahoo public bars"
        assert out["overall"] == "stale"  # the worst source decides

    def test_the_topup_job_is_reported_when_it_has_run(self, tmp_path):
        (tmp_path / "eod_refresh.json").write_text('{"ran_at": "2026-07-07T11:00:00+00:00", "ran_date_ist": "2026-07-07", "names": 10, "ok": 8, "fail": 2}')
        out = build_status(tmp_path, at(2026, 7, 8, 11), CAL)
        assert out["topup"]["updated"] == 8 and out["topup"]["failed"] == 2


def test_the_route_requires_a_login(fresh_env):
    assert TestClient(app, client=("127.0.0.1", 1)).get("/api/v1/data/status").status_code == 401


def test_the_route_returns_the_shape(fresh_env, master):
    client = TestClient(app, client=("127.0.0.1", 51234))
    token = client.post("/api/v1/auth/bootstrap", json={"email": "o@example.com", "username": "owner",
                        "password": "Str0ngPassw0rd", "display_name": "O"}).json()["token"]
    body = client.get("/api/v1/data/status", headers={"Authorization": f"Bearer {token}"}).json()
    assert set(body) >= {"expected_session", "overall", "sources", "market_open"}
    assert {s["id"] for s in body["sources"]} == {"prices", "index"}
