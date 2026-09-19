"""The deterministic scoring core of the Context-Aware Signal Engine.

The single rule this module exists to enforce
---------------------------------------------

**A context for a signal may only use bars at or before the signal fired.**

``enrich`` is the one entry point for both live and historical signals, and it
is look-ahead safe by construction: every stock-level indicator goes through the
same ``_as_of`` truncation that ``atr.research.learning_enrich`` uses for the
learning dataset, so a historical signal can never "know" the bars that came
after it. ``tests/test_signal_context.py`` pins this with a frame whose post-fix
bars are wildly different from its pre-fix bars.

What this module is not
-----------------------

* **Not a signal filter.** It scores, measures and reports; it never blocks a
  signal, changes strategy parameters, or alters risk/order behaviour.
* **Not a probability.** A score of 80/100 means 80% of the *predefined
  conditions* were met at signal time. Evidence that a given score is linked to
  favourable trade outcomes has to come from ``DataContextAnalytics`` using
  genuine forward observations, never from the score itself.
* **Not a data fabricator.** When a required measurement is absent the
  criterion is recorded as missing and the context class becomes
  ``INSUFFICIENT_DATA``. Nothing is estimated, interpolated or guessed.

Design notes
------------

* Environment-dependent values — market breadth, sector metrics, the
  benchmark-provenance record — are supplied as ``market_hint`` /
  ``sector_hint`` snapshots by the caller (the live path takes them from the
  Market Intelligence service; the backtest path computes them point-in-time
  per trade date). This keeps the engine pure and deterministic: feed it the
  same measurements, get the same score, every time.
* Every produced ``SignalContext`` carries ``config.version``; bumping the
  model re-labels nothing retroactively.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd

from atr.market_intel.models import PROVENANCE_NIFTYBEES
from atr.research.learning_enrich import (
    MIN_BARS_ATR,
    _as_of,
    metrics_at,
    relative_strength_at,
    trend_at,
)

#: A "clearly wrong" provenance object, used when the benchmark is unknown. It is
#: honest about being a guess: a context whose benchmark was never found must not
#: be presented as though it were measured against the official index.
_FALLBACK_PROVENANCE = PROVENANCE_NIFTYBEES

#: How many sessions before a signal the 52-week high/low needs to be defined.
MIN_BARS_52W = 21


class SignalContextEngine:
    """Deterministic, versioned scorer of the context around a strategy signal."""

    def __init__(self, *, config: Any = None) -> None:
        from atr.signal_context.models import DEFAULT_CONTEXT_MODEL_V1

        self.config = config or DEFAULT_CONTEXT_MODEL_V1

    # ----------------------------------------------------------------------
    # Snapshot builders — all point-in-time via _as_of truncation
    # ----------------------------------------------------------------------

    def build_market_snapshot(
        self,
        bench_frame: pd.DataFrame | None,
        when: Any,
        *,
        breadth_above_ema50_pct: float | None = None,
        breadth_above_ema20_pct: float | None = None,
        advance_decline_ratio: float | None = None,
        regime: str | None = None,
        benchmark_symbol: str | None = None,
        regime_model_version: str | None = None,
        as_of: str | None = None,
    ) -> Any:
        """Market-level measurements from the benchmark, truncated at ``when``.

        Only the benchmark trend / ATR / volatility are computed here. Breadth,
        advance-decline and the regime label are measurements that require a
        whole-universe scan, so they are *passed in* by the caller (who knows
        whether it can do that scan) rather than silently assumed.
        """
        from atr.signal_context.models import MarketContextSnapshot

        trend, atr_pct, vol_ratio = self._benchmark_measurements(bench_frame, when)
        if as_of is None:
            as_of = self._as_of_iso(bench_frame, when)

        # Regime label: derived from the measurements actually at hand, through
        # the same classifier as the live Market Intelligence layer.
        regime_label = regime
        if regime_label is None:
            regime_label = self._derive_regime_label(
                trend,
                breadth_above_ema50_pct,
                atr_pct,
                vol_ratio,
            )

        return MarketContextSnapshot(
            regime=regime_label,
            nifty_trend_pct=trend,
            breadth_above_ema50_pct=breadth_above_ema50_pct,
            breadth_above_ema20_pct=breadth_above_ema20_pct,
            volatility_atr_pct=atr_pct,
            volatility_ratio=vol_ratio,
            advance_decline_ratio=advance_decline_ratio,
            benchmark_symbol=benchmark_symbol or _FALLBACK_PROVENANCE.symbol,
            benchmark_is_proxy=bool(benchmark_symbol is None)
            or "BEES" in (benchmark_symbol or ""),
            regime_model_version=regime_model_version or self.config.version,
            as_of=as_of,
        )

    def build_sector_snapshot(
        self,
        sector: str | None,
        when: Any,
        *,
        return_1m_pct: float | None = None,
        relative_strength_1m: float | None = None,
        breadth_above_ema50_pct: float | None = None,
        volume_multiple: float | None = None,
        trend: str | None = None,
        as_of: str | None = None,
    ) -> Any | None:
        """Sector measurements. Returns ``None`` when the sector is unknown."""
        from atr.signal_context.models import SectorContextSnapshot

        if not sector:
            return None
        return SectorContextSnapshot(
            sector=sector,
            return_1d_pct=None,
            return_1m_pct=return_1m_pct,
            relative_strength_1m=relative_strength_1m,
            breadth_above_ema50_pct=breadth_above_ema50_pct,
            volume_multiple=volume_multiple,
            trend=trend,
            as_of=as_of or self._as_of_iso(None, when),
        )

    def build_stock_snapshot(
        self,
        stock_frame: pd.DataFrame | None,
        bench_frame: pd.DataFrame | None,
        when: Any,
    ) -> Any:
        """Stock-level point-in-time measurements at ``when``.

        Every field is computed from the frame truncated at ``when`` — the same
        truncation ``learning_enrich`` uses, and the same rule: never a bar
        after the signal.
        """
        from atr.signal_context.models import StockContextSnapshot

        pc, _missing = metrics_at(stock_frame, when, fast=20, slow=50)
        rs = relative_strength_at(stock_frame, bench_frame, when, window=20)
        trend = trend_at(stock_frame, when, window=50)
        from_high, from_low = self._fifty_two_week(stock_frame, when)

        if stock_frame is None or stock_frame.empty:
            return StockContextSnapshot(
                relative_strength_nifty_20d=rs,
                relative_volume=None,
                atr_pct=None,
                trend_pct=None,
                above_sma50=None,
                from_52w_high_pct=None,
                from_52w_low_pct=None,
                gap_pct=None,
                close=None,
                as_of=self._as_of_iso(None, when),
            )

        truncated = _as_of(stock_frame, when)
        if truncated.empty:
            return StockContextSnapshot(
                relative_strength_nifty_20d=rs,
                relative_volume=None,
                atr_pct=None,
                trend_pct=None,
                above_sma50=None,
                from_52w_high_pct=None,
                from_52w_low_pct=None,
                gap_pct=None,
                close=None,
                as_of=self._as_of_iso(None, when),
            )

        return StockContextSnapshot(
            relative_strength_nifty_20d=rs,
            relative_volume=pc.volume_multiple,
            atr_pct=pc.atr_pct,
            trend_pct=trend,
            above_sma50=pc.above_slow_sma,
            from_52w_high_pct=from_high,
            from_52w_low_pct=from_low,
            gap_pct=pc.gap_pct,
            close=pc.close,
            as_of=pc.as_of_ts if pc.as_of_ts is not None else self._as_of_iso(None, when),
        )

    # ----------------------------------------------------------------------
    # Scoring — the deterministic heart
    # ----------------------------------------------------------------------

    def evaluate(
        self,
        market: Any,
        sector: Any,
        stock: Any,
    ) -> dict[str, Any]:
        """Score one context from snapshots. Pure: same inputs, same score.

        Returns a dict of everything the assembler needs: ``score``,
        ``max_possible_score``, ``score_breakdown``, ``has_insufficient_data``,
        ``context_class`` and ``missing_fields``.
        """
        from atr.signal_context.models import ScoreCriterion, SignalContextClass

        cfg = self.config
        breakdown: list[ScoreCriterion] = []
        missing: list[str] = []

        score = 0

        for criterion in cfg.criteria:
            met, awarded, value, reason, measurement_key = self._evaluate_criterion(
                criterion, market, sector, stock
            )
            if measurement_key is not None and measurement_key not in missing:
                missing.append(measurement_key)
            breakdown.append(
                ScoreCriterion(
                    key=criterion.key,
                    label=criterion.label,
                    weight=criterion.weight,
                    met=met,
                    points_awarded=awarded,
                    value=value,
                    reason=reason,
                )
            )
            if met:
                score += awarded

        max_possible = sum(c.weight for c in cfg.criteria)
        has_insufficient = self._has_insufficient(market, sector, stock)
        context_class = (
            SignalContextClass.INSUFFICIENT_DATA
            if has_insufficient
            else cfg.classify(score, False)
        )

        return {
            "score": score,
            "max_possible_score": max_possible,
            "score_breakdown": breakdown,
            "has_insufficient_data": has_insufficient,
            "context_class": context_class,
            "missing_fields": sorted(set(missing)),
        }

    # ----------------------------------------------------------------------
    # Assembly
    # ----------------------------------------------------------------------

    def enrich(
        self,
        *,
        signal_id: str,
        symbol: str,
        action: str,
        signal_source: str,
        signal_ts: str,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        stock_frame: pd.DataFrame | None = None,
        bench_frame: pd.DataFrame | None = None,
        market_hint: Any | None = None,
        sector_hint: Any | None = None,
        sector_name: str | None = None,
        when: Any | None = None,
        created_at: str | None = None,
    ) -> Any:
        """Assemble a complete ``SignalContext`` for one signal.

        ``when`` defaults to the signal timestamp; both callers (live and
        backtest) pass it anyway, and it is *the* cut line for look-ahead
        safety. ``market_hint`` / ``sector_hint`` are optional pre-built
        snapshots; when absent, the relevant measurements are computed from the
        frames (with breadth absent, which can force ``INSUFFICIENT_DATA``).
        """
        from atr.signal_context.models import SignalContext

        market = market_hint or self.build_market_snapshot(bench_frame, when)
        sector = sector_hint
        if sector is None and sector_name:
            sector = self.build_sector_snapshot(sector_name, when)
        stock = self.build_stock_snapshot(stock_frame, bench_frame, when)

        result = self.evaluate(market, sector, stock)
        at_iso = created_at or datetime.now(UTC).isoformat()

        return SignalContext(
            signal_id=signal_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            symbol=symbol,
            action=action,
            signal_source=signal_source,
            signal_ts=signal_ts,
            context_model_version=self.config.version,
            context_class=result["context_class"],
            context_score=result["score"],
            max_possible_score=result["max_possible_score"],
            has_insufficient_data=result["has_insufficient_data"],
            market_context=market,
            sector_context=sector,
            stock_context=stock,
            score_breakdown=result["score_breakdown"],
            missing_fields=result["missing_fields"],
            benchmark_provenance={
                "symbol": market.benchmark_symbol,
                "kind": "BENCHMARK_ETF_PROXY" if market.benchmark_is_proxy else "BENCHMARK_ACTUAL_INDEX",
                "is_proxy": market.benchmark_is_proxy,
                "tracking_target": "NIFTY 50",
                "notes": "Recorded by the signal context engine at signal time.",
            },
            created_at=at_iso,
        )

    # ----------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------

    def _benchmark_measurements(
        self, bench_frame: pd.DataFrame | None, when: Any
    ) -> tuple[float | None, float | None, float | None]:
        """(trend_pct vs SMA50, ATR%, volatility_ratio) at ``when``, all None-safe."""
        if bench_frame is None or bench_frame.empty:
            return None, None, None

        window = _as_of(bench_frame, when)
        if window.empty:
            return None, None, None

        trend = self._trend_pct(window)
        atr_pct = self._atr_pct(window)
        vol_ratio = self._volatility_ratio(window)
        return trend, atr_pct, vol_ratio

    def _trend_pct(self, window: pd.DataFrame) -> float | None:
        from atr.strategy.indicators import sma

        closes = window["close"].astype(float)
        if len(closes) < 20:
            return None
        close = float(closes.iloc[-1])
        if close <= 0:
            return None
        line_window = 50 if len(closes) >= 50 else 20
        line = sma(closes, line_window).iloc[-1]
        if pd.isna(line) or line <= 0:
            return None
        return float((close / line - 1.0) * 100.0)

    def _atr_pct(self, window: pd.DataFrame) -> float | None:
        from atr.strategy.indicators import atr

        if len(window) < MIN_BARS_ATR:
            return None
        value = atr(window["high"], window["low"], window["close"]).iloc[-1]
        close = float(window["close"].iloc[-1])
        if pd.isna(value) or close <= 0:
            return None
        return float(value / close * 100.0)

    def _volatility_ratio(self, window: pd.DataFrame) -> float | None:
        import numpy as np

        if len(window) < 50:
            return None
        highs = window["high"].astype(float)
        lows = window["low"].astype(float)
        closes = window["close"].astype(float)
        tr = np.maximum(
            highs - lows,
            np.maximum(abs(highs - closes.shift()), abs(lows - closes.shift())),
        )
        short = float(tr.rolling(10).mean().iloc[-1])
        long_ = float(tr.rolling(50).mean().iloc[-1])
        if long_ <= 0:
            return None
        return short / long_

    def _derive_regime_label(
        self,
        trend: float | None,
        breadth: float | None,
        atr_pct: float | None,
        vol_ratio: float | None,
    ) -> str | None:
        """A coarse regime label from whatever measurements exist.

        When both trend and breadth are absent there is nothing to classify, and
        returning ``None`` is the honest answer — the context will be marked
        ``INSUFFICIENT_DATA`` rather than getting a guessed label.
        """
        if trend is None and breadth is None:
            return None
        t = trend if trend is not None else 0.0
        b = breadth if breadth is not None else 50.0
        if vol_ratio is not None and vol_ratio >= 1.35:
            return "HIGH_VOLATILITY"
        if atr_pct is not None and atr_pct > 2.0:
            return "HIGH_VOLATILITY"
        if t >= 1.0 and b >= 55.0:
            return "BULLISH_TREND"
        if t <= -1.0 and b <= 45.0:
            return "BEARISH_TREND"
        return "SIDEWAYS"

    def _fifty_two_week(
        self, stock_frame: pd.DataFrame | None, when: Any
    ) -> tuple[float | None, float | None]:
        if stock_frame is None or stock_frame.empty:
            return None, None
        window = _as_of(stock_frame, when)
        if len(window) < MIN_BARS_52W:
            return None, None
        lookback = min(len(window), 252)
        close = float(window["close"].iloc[-1])
        high = float(window["high"].astype(float).tail(lookback).max())
        low = float(window["low"].astype(float).tail(lookback).min())
        from_high = (close / high - 1.0) * 100.0 if high > 0 else None
        from_low = (close / low - 1.0) * 100.0 if low > 0 else None
        return from_high, from_low

    def _as_of_iso(self, frame: pd.DataFrame | None, when: Any) -> str:
        if frame is not None and not frame.empty:
            window = _as_of(frame, when)
            if not window.empty:
                return str(window["ts"].iloc[-1])
        try:
            return str(pd.to_datetime(when))
        except Exception:  # noqa: BLE001
            raise ValueError(f"cannot parse when={when!r} and no frame available for as_of")

    def _evaluate_criterion(
        self, criterion: Any, market: Any, sector: Any, stock: Any
    ) -> tuple[bool, int, str | None, str | None, str | None]:
        """Return (met, awarded, value_display, reason, missing_key)."""
        cfg = self.config
        key = criterion.key

        # (getter, missing_key, threshold, comparator, formatter)
        _CHECKS = {
            "market_bullish_trend": (
                lambda: market.nifty_trend_pct if market else None,
                "nifty_trend_pct",
                cfg.bullish_trend_min_pct,
                ">=",
                lambda m: f"NIFTY trend {m:+.2f}% {'≥' if m >= cfg.bullish_trend_min_pct else '<'} {cfg.bullish_trend_min_pct:.1f}% threshold",
            ),
            "market_breadth": (
                lambda: market.breadth_above_ema50_pct if market else None,
                "breadth_above_ema50_pct",
                cfg.breadth_strong_min_pct,
                ">=",
                lambda m: f"breadth {m:+.2f}% {'≥' if m >= cfg.breadth_strong_min_pct else '<'} {cfg.breadth_strong_min_pct:.1f}% threshold",
            ),
            "strong_sector": (
                lambda: (sector.relative_strength_1m if sector and sector.sector else None),
                "sector_rs_1m",
                cfg.sector_strong_rs_pct,
                ">",
                lambda m: f"sector RS {m:+.2f}% {'>' if m > cfg.sector_strong_rs_pct else '≤'} {cfg.sector_strong_rs_pct:+.1f}% vs NIFTY 1M",
            ),
            "stock_rs": (
                lambda: stock.relative_strength_nifty_20d,
                "stock_rs_20d",
                cfg.stock_strong_rs_pct,
                ">",
                lambda m: f"stock RS {m:+.2f}% {'>' if m > cfg.stock_strong_rs_pct else '≤'} NIFTY 20D",
            ),
            "rvol_confirmation": (
                lambda: stock.relative_volume,
                "relative_volume",
                cfg.rvol_confirmation_min,
                ">=",
                lambda m: f"RVOL {m:.2f}× {'≥' if m >= cfg.rvol_confirmation_min else '<'} {cfg.rvol_confirmation_min:.1f}×",
            ),
        }

        if key in _CHECKS:
            getter, missing_key, threshold, cmp_op, fmt = _CHECKS[key]
            m = getter()
            if m is None:
                return False, 0, None, f"{missing_key} unavailable", missing_key
            met = (m >= threshold) if cmp_op == ">=" else (m > threshold)
            display = f"{m:+.2f}%" if key != "rvol_confirmation" else f"{m:.2f}×"
            return met, criterion.weight if met else 0, display, fmt(m), None

        if key == "trend_confirmation":
            m = stock.above_sma50
            if m is None:
                return False, 0, None, "SMA50 position unavailable", "above_sma50"
            met = bool(m)
            return met, criterion.weight if met else 0, ("above" if met else "below"), (
                "close above SMA50" if met else "close below SMA50"
            ), None

        return False, 0, None, f"unknown criterion {key}", None

    def _has_insufficient(self, market: Any, sector: Any, stock: Any) -> bool:
        return any(
            self._measurement_is_none(criterion.key, market, sector, stock)
            for criterion in self.config.criteria
        )

    @staticmethod
    def _measurement_is_none(key: str, market: Any, sector: Any, stock: Any) -> bool:
        if key == "market_bullish_trend":
            return market is None or market.nifty_trend_pct is None
        if key == "market_breadth":
            return market is None or market.breadth_above_ema50_pct is None
        if key == "strong_sector":
            return sector is None or not sector.sector or sector.relative_strength_1m is None
        if key == "stock_rs":
            return stock.relative_strength_nifty_20d is None
        if key == "rvol_confirmation":
            return stock.relative_volume is None
        if key == "trend_confirmation":
            return stock.above_sma50 is None
        return False