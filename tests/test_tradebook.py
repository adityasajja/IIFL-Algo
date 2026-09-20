"""The tradebook is day-scoped at the broker, so a day must be captured that evening or it is lost."""

import json
from datetime import date

import pytest

from atr.services import tradebook as tb

DAY = date(2026, 9, 18)


class _Broker:
    def __init__(self, trades, positions):
        self._t, self._p = trades, positions

    def trades(self):
        return self._t

    def positions(self):
        return self._p


QUIET_TRADES = {"status": "Ok", "result": {"status": "EC926", "message": "Error : No Trade's are found for this user."}}
QUIET_POSITIONS = {"status": "Ok", "result": [{"status": "EC920", "message": "Error : No Positions found for this user."}]}
BLOCKED = {"status": "Ok", "result": [{"status": "EC500", "message": "Error : IP address not authorized for trading."}]}


def test_trades_and_positions_are_saved_as_the_broker_sent_them(tmp_path):
    fills = [{"tradingSymbol": "AAA-EQ", "qty": 10, "price": 100.5}]
    positions = [{"tradingSymbol": "AAA-EQ", "netQty": 0}]

    out = tb.capture_day(_Broker({"result": fills}, {"result": positions}), tmp_path, DAY)

    assert out == {"trades": 1, "positions": 1}
    saved = json.loads((tmp_path / "portfolio" / "tradebook" / "2026-09-18.json").read_text())
    assert saved["rows"] == fills  # untouched: the field names are not assumed
    assert (tmp_path / "portfolio" / "positions" / "2026-09-18.json").exists()


def test_a_quiet_day_is_not_a_failure_and_writes_nothing(tmp_path):
    out = tb.capture_day(_Broker(QUIET_TRADES, QUIET_POSITIONS), tmp_path, DAY)

    assert out == {"trades": 0, "positions": 0}
    assert not (tmp_path / "portfolio" / "tradebook").exists()


def test_a_refusal_raises_so_the_job_tries_again(tmp_path):
    with pytest.raises(RuntimeError, match="IP address not authorized"):
        tb.capture_day(_Broker(QUIET_TRADES, BLOCKED), tmp_path, DAY)


def test_trades_already_read_are_kept_even_if_the_positions_call_is_refused(tmp_path):
    """Losing the fills because a second call failed would be the very thing this exists to prevent."""
    fills = [{"tradingSymbol": "AAA-EQ", "qty": 1}]

    with pytest.raises(RuntimeError):
        tb.capture_day(_Broker({"result": fills}, BLOCKED), tmp_path, DAY)

    assert (tmp_path / "portfolio" / "tradebook" / "2026-09-18.json").exists()
