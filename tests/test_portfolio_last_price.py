"""Holdings carry the broker's last price, so the page can value every stock."""

from atr.api.legacy.dashboard import _attach_last_price


class Client:
    def __init__(self, quotes=None, fail=False):
        self.quotes, self.fail, self.asked = quotes or [], fail, []

    def market_quotes(self, legs):
        self.asked.append(list(legs))
        if self.fail:
            raise RuntimeError("down")
        return {"status": "Ok", "result": self.quotes}


def test_each_holding_gets_its_price_and_a_stock_in_two_lots_gets_it_twice():
    rows = [
        {"nseTradingSymbol": "CUB-EQ", "nseInstrumentId": "5701", "totalQuantity": 50},
        {"nseTradingSymbol": "CUB-EQ", "nseInstrumentId": "5701", "totalQuantity": 24},
        {"nseTradingSymbol": "ABB-EQ", "nseInstrumentId": "13", "totalQuantity": 5},
    ]
    client = Client([{"instrumentId": 5701, "ltp": 236.5}, {"instrumentId": 13, "ltp": 5100.0}])

    _attach_last_price(client, rows)

    assert [r["ltp"] for r in rows] == [236.5, 236.5, 5100.0]
    assert len(client.asked) == 1 and len(client.asked[0]) == 2  # one batch, one leg per instrument


def test_a_failed_quote_leaves_the_holdings_as_they_were():
    rows = [{"nseInstrumentId": "5701", "totalQuantity": 50}]
    _attach_last_price(Client(fail=True), rows)
    assert "ltp" not in rows[0]


def test_a_zero_or_missing_price_is_not_attached():
    rows = [{"nseInstrumentId": "5701"}, {"nseInstrumentId": "13"}, {"totalQuantity": 1}]
    _attach_last_price(Client([{"instrumentId": 5701, "ltp": 0}, {"instrumentId": 13}]), rows)
    assert all("ltp" not in r for r in rows)
