import pandas as pd

from atr.backtest.validity import validity_warnings


def _frame(closes, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"close": closes}, index=idx)


def test_split_like_jump_is_flagged():
    notes = validity_warnings({"ABC": _frame([100, 101, 50, 51])}, allow_short=False, cost_model="india_delivery")
    assert any("ABC" in n and "unadjusted" in n for n in notes)


def test_clean_data_only_gets_survivorship_note():
    notes = validity_warnings({"ABC": _frame([100, 101, 102, 103])}, allow_short=False, cost_model="india_delivery")
    assert len(notes) == 1 and "Survivorship" in notes[0]


def test_gap_is_flagged():
    f = pd.concat([_frame([100, 101], "2024-01-01"), _frame([102, 103], "2024-03-01")])
    notes = validity_warnings({"ABC": f}, allow_short=False, cost_model="india_delivery")
    assert any("gap" in n for n in notes)


def test_short_with_delivery_costs_warns():
    notes = validity_warnings({"A": _frame([1, 1.01])}, allow_short=True, cost_model="india_delivery")
    assert any("squared off" in n for n in notes)
