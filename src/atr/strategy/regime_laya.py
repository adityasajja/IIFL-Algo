"""Market regime classification, with an optional Laya assist.

Laya (convaiinnovations/laya) is a general-purpose typed-decision model — it
was never trained on market data. Tested against textbook regime scenarios
(clear uptrend, clear breakdown, flat consolidation, volatility crash) its
choice probabilities came back close to uniform (~0.10-0.35 across 4 choices,
confidence 0.05-0.14) and it misclassified an ADX-12 flat/sideways market as
"trending bearish". It is not a reliable regime signal on its own.

Rather than pretend it is, this only lets a Laya call *override* the
deterministic indicator-based classification when its own reported confidence
clears ``min_confidence`` (default 0.35, above anything observed in testing
against unambiguous cases) — otherwise the indicator rules decide, and the
Laya opinion is kept only for logging/inspection via
``MarketRegimeVerdict.raw_decision``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel


@dataclass
class MarketRegimeVerdict:
    regime: str
    confidence: float
    source: str = "rules"  # "rules" or "laya"
    raw_decision: Any = None


class _RegimeSchema(BaseModel):
    regime: Literal[
        "TRENDING_BULLISH", "TRENDING_BEARISH", "MEAN_REVERTING", "HIGH_VOLATILITY"
    ]


class LayaRegimeClassifier:
    """Classifies market regimes via indicator rules, with a low-weight Laya assist."""

    REGIMES = [
        "TRENDING_BULLISH",
        "TRENDING_BEARISH",
        "MEAN_REVERTING",
        "HIGH_VOLATILITY",
    ]

    def __init__(
        self,
        model_name: str = "convaiinnovations/laya",
        device: str | None = None,
        min_confidence: float = 0.35,
    ):
        self.model_name = model_name
        self.device = device
        self.min_confidence = min_confidence
        self._agent = None
        self._is_available: bool | None = None

    def _load_agent(self):
        if self._agent is not None:
            return self._agent

        try:
            import laya

            logger.info("Loading Laya agent: {}", self.model_name)
            self._agent = laya.load(self.model_name, device=self.device)
            self._is_available = True
            return self._agent
        except Exception as e:
            logger.warning(
                "Failed to load Laya agent ({}): {}. Using indicator-rule classification.",
                self.model_name,
                e,
            )
            self._is_available = False
            return None

    def classify_market_context(
        self,
        symbol: str,
        atr_pct: float,
        adx: float,
        rsi: float,
        return_20d: float,
        sma_ratio: float,
    ) -> MarketRegimeVerdict:
        """Evaluate market state indicators into a structured decision.

        Args:
            symbol: Ticker or index name (e.g. NIFTY50, RELIANCE).
            atr_pct: Average True Range as a percentage of close price.
            adx: Average Directional Index (trend strength, 0-100).
            rsi: Relative Strength Index (momentum, 0-100).
            return_20d: 20-period return in percentage (e.g. +3.5 or -2.1).
            sma_ratio: Fast SMA / Slow SMA ratio (e.g. 1.02 for +2% above slow SMA).
        """
        rule_verdict = self._rule_based_fallback(
            atr_pct=atr_pct, adx=adx, rsi=rsi, return_20d=return_20d, sma_ratio=sma_ratio
        )

        agent = self._load_agent()
        if agent is None or not self._is_available:
            return rule_verdict

        state = {
            "symbol": symbol,
            "atr_volatility_pct": round(float(atr_pct), 2),
            "trend_strength_adx": round(float(adx), 2),
            "momentum_rsi": round(float(rsi), 2),
            "return_20d_pct": round(float(return_20d), 2),
            "fast_to_slow_sma_ratio": round(float(sma_ratio), 4),
        }

        try:
            import laya

            result = laya.decide(agent, state, schema=_RegimeSchema, return_details=True)
            regime = result.values["regime"]
            confidence = float(result.confidence.get("regime", 0.0))
        except Exception as e:
            logger.error("Error during Laya inference: {}. Using indicator rules.", e)
            return rule_verdict

        if confidence >= self.min_confidence:
            return MarketRegimeVerdict(
                regime=regime, confidence=confidence, source="laya", raw_decision=result
            )

        logger.debug(
            "Laya confidence {:.2f} below threshold {:.2f} for {}; keeping rule-based {}",
            confidence,
            self.min_confidence,
            symbol,
            rule_verdict.regime,
        )
        rule_verdict.raw_decision = result
        return rule_verdict

    def _rule_based_fallback(
        self, atr_pct: float, adx: float, rsi: float, return_20d: float, sma_ratio: float
    ) -> MarketRegimeVerdict:
        """Deterministic baseline fallback matching the regime taxonomy."""
        if atr_pct > 3.0:
            return MarketRegimeVerdict(regime="HIGH_VOLATILITY", confidence=0.85)
        if adx > 25.0 and sma_ratio > 1.01 and return_20d > 0:
            return MarketRegimeVerdict(regime="TRENDING_BULLISH", confidence=0.90)
        if adx > 25.0 and sma_ratio < 0.99 and return_20d < 0:
            return MarketRegimeVerdict(regime="TRENDING_BEARISH", confidence=0.90)
        return MarketRegimeVerdict(regime="MEAN_REVERTING", confidence=0.75)


# Global singleton classifier instance
_default_classifier = None


def get_regime_classifier() -> LayaRegimeClassifier:
    global _default_classifier
    if _default_classifier is None:
        _default_classifier = LayaRegimeClassifier()
    return _default_classifier
