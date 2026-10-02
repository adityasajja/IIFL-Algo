import pytest

from atr.backtest.costs import IndianDeliveryCosts, IndianIntradayCosts
from atr.backtest.config import BacktestRunConfig, ConfigError
from atr.core.enums import Side
from atr.core.models import Instrument

INST = Instrument(symbol="X", exchange="NSE", currency="INR")


def test_intraday_stt_is_sell_side_only_and_cheaper_than_delivery():
    intra, deliv = IndianIntradayCosts(), IndianDeliveryCosts()
    buy_i = intra.compute(100, 1000, INST, Side.BUY)
    sell_i = intra.compute(100, 1000, INST, Side.SELL)
    assert sell_i > buy_i  # STT only on the sell leg
    assert buy_i + sell_i < deliv.compute(100, 1000, INST, Side.BUY) + deliv.compute(100, 1000, INST, Side.SELL)
    # 0.025% STT on 1,00,000 = 25; the delivery sell leg alone pays 100 plus DP
    assert 24 < sell_i - buy_i + 3 < 60


def test_participation_default_is_capped_and_validated():
    assert BacktestRunConfig().participation_rate == 0.05
    cfg = BacktestRunConfig(participation_rate=0)
    with pytest.raises(ConfigError):
        cfg.validate()
