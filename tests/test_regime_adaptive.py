import pytest
from atr.strategy.regime_laya import LayaRegimeClassifier, MarketRegimeVerdict
from atr.strategy.strategies.regime_adaptive import RegimeAdaptiveStrategy
from atr.strategy.strategies import STRATEGIES


def test_laya_classifier_instantiation():
    classifier = LayaRegimeClassifier()
    assert "TRENDING_BULLISH" in classifier.REGIMES
    assert "TRENDING_BEARISH" in classifier.REGIMES
    assert "MEAN_REVERTING" in classifier.REGIMES
    assert "HIGH_VOLATILITY" in classifier.REGIMES


def test_regime_classification_logic():
    classifier = LayaRegimeClassifier()
    
    # Test high volatility scenario
    high_vol = classifier.classify_market_context(
        symbol="NIFTY",
        atr_pct=4.2,
        adx=20.0,
        rsi=50.0,
        return_20d=-1.0,
        sma_ratio=1.0,
    )
    assert isinstance(high_vol, MarketRegimeVerdict)
    assert high_vol.regime == "HIGH_VOLATILITY"

    # Test strong bullish trend
    bullish = classifier.classify_market_context(
        symbol="RELIANCE",
        atr_pct=1.2,
        adx=32.0,
        rsi=65.0,
        return_20d=6.5,
        sma_ratio=1.03,
    )
    assert bullish.regime == "TRENDING_BULLISH"


def test_strategy_registered():
    assert "regime_adaptive" in STRATEGIES
    strat_cls = STRATEGIES["regime_adaptive"]
    assert issubclass(strat_cls, RegimeAdaptiveStrategy)
