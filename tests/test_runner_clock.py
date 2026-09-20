"""The runner asks the market calendar in Indian time."""

from datetime import datetime

from atr.market_calendar import IST


def test_the_runner_asks_the_calendar_in_indian_time_not_utc():
    from atr.market_calendar import get_market_calendar
    from atr.services import runner

    cal = get_market_calendar()
    now = runner._now_ist()
    assert now.tzinfo is not None and now.utcoffset().total_seconds() == 5.5 * 3600
    # 10:00 on a Monday in India is open, however the machine clock is set
    assert cal.is_market_open(datetime(2026, 9, 21, 10, 0, tzinfo=IST))


def test_a_pass_without_a_time_uses_the_indian_clock(app_db, monkeypatch):
    from atr.services import runner as runner_module

    monday_ten = datetime(2026, 9, 21, 10, 0, tzinfo=IST)
    monkeypatch.setattr(runner_module, "_now_ist", lambda: monday_ten)

    tick = runner_module.PaperRunner(db=app_db).pass_once()

    assert tick.at == monday_ten
