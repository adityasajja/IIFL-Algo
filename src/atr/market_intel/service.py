"""Indian Equity Market Intelligence service.

Provides a unified, point-in-time and current-snapshot market intelligence layer:
MARKET → SECTOR → STOCK → STRATEGY → TRADE → LEARNING

Strictly focused on Indian Cash Equities (NSE/BSE). No F&O or options data.
"""

from __future__ import annotations

import csv
import logging
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from atr.instruments.service import canonical_symbol
from atr.market_intel.models import (
    BENCHMARK_KIND_ETF_PROXY,
    DEFAULT_REGIME_CONFIG_V1,
    BenchmarkProvenance,
    MarketRegimeClassification,
    MarketSummary,
    PROVENANCE_MONIFTY500,
    PROVENANCE_NIFTY50_INDEX,
    PROVENANCE_NIFTYBEES,
    RawMarketMeasurements,
    RegimeModelConfig,
    SectorMetrics,
    StockContext,
)
from atr.strategy.indicators import atr as calc_atr
from atr.strategy.indicators import ema as calc_ema
from atr.strategy.indicators import sma as calc_sma

logger = logging.getLogger("atr.market_intel")

ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = ROOT / "data"
UNIVERSE_DIR = DATA_ROOT / "universe"
CACHE_DIR = DATA_ROOT / "iifl_daily" / "NSEEQ"

DEFAULT_BENCHMARK_SYMBOLS = ("NIFTYBEES-EQ", "NIFTYBEES", "MONIFTY500-EQ")


class MarketIntelService:
    """Computes real-time and point-in-time intelligence for Indian cash equities."""

    def __init__(
        self,
        data_root: Path | None = None,
        regime_config: RegimeModelConfig | None = None,
    ) -> None:
        self.data_root = data_root or DATA_ROOT
        self.universe_dir = self.data_root / "universe"
        self.cache_dir = self.data_root / "iifl_daily" / "NSEEQ"
        self._lock = threading.Lock()
        # Held for the whole computation; `_lock` only guards the cache fields.
        self._compute_lock = threading.Lock()
        self._index_attempt: float = 0.0
        self.regime_config = regime_config or DEFAULT_REGIME_CONFIG_V1
        self._sector_map: dict[str, str] | None = None
        self._universe_symbols: list[str] | None = None
        self._cached_summary: MarketSummary | None = None
        self._cached_sectors: list[SectorMetrics] | None = None
        self._cached_stocks: dict[str, StockContext] | None = None
        self._cache_time: float = 0.0
        self._ttl_seconds: float = 300.0  # inputs are daily bars; 5s to rebuild

    # --------------------------------------------------------------------------
    # Configuration & Versioning
    # --------------------------------------------------------------------------
    def get_regime_config(self) -> RegimeModelConfig:
        with self._lock:
            return self.regime_config

    def set_regime_config(self, config: RegimeModelConfig) -> None:
        with self._lock:
            self.regime_config = config
            # Invalidate cached calculations when model thresholds change
            self._cached_summary = None
            self._cached_sectors = None
            self._cached_stocks = None
            self._cache_time = 0.0

    # --------------------------------------------------------------------------
    # Universe & Sector Mapping
    # --------------------------------------------------------------------------
    def get_sector_map(self) -> dict[str, str]:
        """Loads canonical symbol -> Industry mapping from universe CSVs."""
        if self._sector_map is not None:
            return self._sector_map

        out: dict[str, str] = {}
        if self.universe_dir.is_dir():
            for csv_path in sorted(self.universe_dir.glob("ind_*list.csv")):
                try:
                    with csv_path.open("r", encoding="utf-8", errors="replace") as handle:
                        for row in csv.DictReader(handle):
                            raw_sym = row.get("Symbol") or ""
                            industry = (row.get("Industry") or "").strip()
                            sym = canonical_symbol(raw_sym)
                            if sym and industry:
                                out.setdefault(sym, industry)
                except Exception as exc:
                    logger.warning("Error reading universe CSV {}: {}", csv_path, exc)

        self._sector_map = out
        return out

    def get_universe_symbols(self) -> list[str]:
        """Returns unique symbols in NIFTY 500 universe files."""
        if self._universe_symbols is not None:
            return self._universe_symbols

        sector_map = self.get_sector_map()
        symbols = list(sector_map.keys())
        if not symbols and self.cache_dir.is_dir():
            symbols = [p.stem for p in self.cache_dir.glob("*.parquet") if not p.stem.startswith(".")]

        self._universe_symbols = sorted(symbols)
        return self._universe_symbols

    # --------------------------------------------------------------------------
    # Frame Loading & Benchmark Provenance
    # --------------------------------------------------------------------------
    def _load_frame(self, symbol: str) -> pd.DataFrame | None:
        """Loads daily parquet frame for a symbol."""
        from atr.data.history import load_cached

        clean = canonical_symbol(symbol)
        for cand in (clean, f"{clean}-EQ", clean.replace("-EQ", "")):
            try:
                df = load_cached(cand, exchange="NSEEQ", timeframe="daily")
                if df is not None and not df.empty:
                    return df
            except Exception:
                continue

        p1 = self.cache_dir / f"{clean}.parquet"
        p2 = self.cache_dir / f"{clean}-EQ.parquet"
        target = p1 if p1.exists() else (p2 if p2.exists() else None)
        if target is not None:
            try:
                return pd.read_parquet(target)
            except Exception as e:
                logger.debug("Failed reading parquet {}: {}", target, e)
        return None

    def get_benchmark_frame_and_provenance(self) -> tuple[pd.DataFrame | None, BenchmarkProvenance]:
        """Finds benchmark frame and explicitly attaches BenchmarkProvenance."""
        # 0. The official index, downloaded from the broker whenever a session
        #    exists and cached (see atr.data.indices). Preferred over any proxy.
        from atr.data.indices import load_index

        frame = load_index(self.data_root)
        if frame is not None:
            return frame, PROVENANCE_NIFTY50_INDEX

        # 1. First check if official index level exists in the stock cache
        for idx_sym in ("NIFTY 50", "NIFTY-EQ", "NIFTY50"):
            frame = self._load_frame(idx_sym)
            if frame is not None and len(frame) >= 20:
                return frame, PROVENANCE_NIFTY50_INDEX

        # 2. Check NIFTYBEES ETF proxy
        for bees_sym in ("NIFTYBEES-EQ", "NIFTYBEES"):
            frame = self._load_frame(bees_sym)
            if frame is not None and len(frame) >= 20:
                return frame, PROVENANCE_NIFTYBEES

        # 3. Check MONIFTY500 ETF proxy
        frame = self._load_frame("MONIFTY500-EQ")
        if frame is not None and len(frame) >= 20:
            return frame, PROVENANCE_MONIFTY500

        # Fallback empty provenance
        fallback = BenchmarkProvenance(
            symbol="UNAVAILABLE",
            display_name="Unavailable Benchmark",
            kind=BENCHMARK_KIND_ETF_PROXY,
            tracking_target="NIFTY 50",
            is_proxy=True,
            notes="No cached benchmark series found in local repository.",
        )
        return None, fallback

    # --------------------------------------------------------------------------
    # Calculation Core
    # --------------------------------------------------------------------------
    def compute_all(
        self, *, force_refresh: bool = False
    ) -> tuple[MarketSummary, list[SectorMetrics], dict[str, StockContext]]:
        """Cached market summary, sector rankings and stock contexts, computed once.

        The screen asks for the summary, the sectors and the stocks at the same
        moment, and each call needs the same full-universe pass. Without a lock
        held across the computation every request ran its own copy in parallel —
        a thundering herd that turned one slow pass into several. Now one caller
        computes and the others wait, then reuse its result.
        """
        waited_from = time.time()
        with self._compute_lock:
            # A caller that queued behind another's computation takes that result
            # instead of repeating it, even when it asked for a refresh: the pass
            # that just finished started after this request did.
            with self._lock:
                filled_while_waiting = (
                    self._cached_summary is not None
                    and self._cached_sectors is not None
                    and self._cached_stocks is not None
                    and self._cache_time >= waited_from
                )
                if filled_while_waiting:
                    return self._cached_summary, self._cached_sectors, self._cached_stocks
            return self._compute_all_uncached(force_refresh=force_refresh)

    def _maybe_refresh_index(self) -> None:
        """Keep the cached Nifty 50 current, once a day.

        With a broker session the download runs in the background (IIFL is the
        preferred source). Without one the public series is fetched inline: it
        takes about a second, and doing it here means the very first load already
        shows the real index rather than the ETF.
        """
        from atr.data.indices import sync_index, sync_index_public, synced_today

        if synced_today(self.data_root) or time.time() - self._index_attempt < 3600:
            return
        self._index_attempt = time.time()
        try:
            from atr.services.broker_access import authed_client

            client = authed_client()
        except Exception:  # noqa: BLE001 - no session
            sync_index_public(self.data_root)
            return

        def work() -> None:
            # If the broker call fails (expired token, changed response), the
            # public series still keeps the headline on the real index.
            if sync_index(client, self.data_root) != "ok":
                sync_index_public(self.data_root)

        threading.Thread(target=work, name="index-sync", daemon=True).start()

    def _compute_all_uncached(
        self, *, force_refresh: bool = False
    ) -> tuple[MarketSummary, list[SectorMetrics], dict[str, StockContext]]:
        self._maybe_refresh_index()
        now = time.time()
        with self._lock:
            if (
                not force_refresh
                and self._cached_summary is not None
                and self._cached_sectors is not None
                and self._cached_stocks is not None
                and (now - self._cache_time) < self._ttl_seconds
            ):
                return self._cached_summary, self._cached_sectors, self._cached_stocks
            cfg = self.regime_config

        sector_map = self.get_sector_map()
        universe_symbols = self.get_universe_symbols()
        benchmark_frame, bench_provenance = self.get_benchmark_frame_and_provenance()

        # Benchmark metrics
        nifty_close = 0.0
        nifty_1d_pct = 0.0
        nifty_1w_pct = 0.0
        nifty_1m_pct = 0.0
        nifty_trend_pct = 0.0
        nifty_above_ema20 = True
        nifty_above_ema50 = True
        bench_atr_pct = 1.2
        bench_vol_ratio = 1.0

        if benchmark_frame is not None and len(benchmark_frame) >= 20:
            b_closes = benchmark_frame["close"].astype(float)
            nifty_close = float(b_closes.iloc[-1])
            if len(b_closes) >= 2:
                nifty_1d_pct = float((nifty_close / b_closes.iloc[-2] - 1.0) * 100.0)
            if len(b_closes) >= 5:
                nifty_1w_pct = float((nifty_close / b_closes.iloc[-5] - 1.0) * 100.0)
            if len(b_closes) >= 22:
                nifty_1m_pct = float((nifty_close / b_closes.iloc[-22] - 1.0) * 100.0)

            # Trend vs SMA window
            trend_window = cfg.trend_sma_window
            if len(b_closes) >= trend_window:
                s_trend = float(calc_sma(b_closes, trend_window).iloc[-1])
                nifty_trend_pct = (nifty_close / s_trend - 1.0) * 100.0
            elif len(b_closes) >= 20:
                s20 = float(calc_sma(b_closes, 20).iloc[-1])
                nifty_trend_pct = (nifty_close / s20 - 1.0) * 100.0

            if len(b_closes) >= 20:
                e20 = float(calc_ema(b_closes, 20).iloc[-1])
                nifty_above_ema20 = nifty_close >= e20
            if len(b_closes) >= cfg.breadth_ema_window:
                e50 = float(calc_ema(b_closes, cfg.breadth_ema_window).iloc[-1])
                nifty_above_ema50 = nifty_close >= e50

            # Benchmark ATR
            if len(benchmark_frame) >= cfg.atr_window + 1:
                tr_s = calc_atr(
                    benchmark_frame["high"],
                    benchmark_frame["low"],
                    benchmark_frame["close"],
                    cfg.atr_window,
                )
                atr_val = float(tr_s.iloc[-1]) if pd.notna(tr_s.iloc[-1]) else 0.0
                if nifty_close > 0:
                    bench_atr_pct = (atr_val / nifty_close) * 100.0

            # Volatility ratio (short vs long window)
            if len(benchmark_frame) >= cfg.vol_long_window:
                highs, lows, closes = (
                    benchmark_frame["high"],
                    benchmark_frame["low"],
                    benchmark_frame["close"],
                )
                tr = np.maximum(
                    highs - lows,
                    np.maximum(abs(highs - closes.shift()), abs(lows - closes.shift())),
                )
                atr_short = float(tr.rolling(cfg.vol_short_window).mean().iloc[-1])
                atr_long = float(tr.rolling(cfg.vol_long_window).mean().iloc[-1])
                if atr_long > 0:
                    bench_vol_ratio = atr_short / atr_long

        # Scan Universe
        stock_contexts: dict[str, StockContext] = {}
        sector_stocks: dict[str, list[dict[str, Any]]] = {}

        advancing = 0
        declining = 0
        unchanged = 0
        above_ema20_cnt = 0
        above_ema50_cnt = 0
        above_sma200_cnt = 0
        highs_52w_cnt = 0
        lows_52w_cnt = 0
        breakouts: list[dict[str, Any]] = []
        # date -> [stocks above their 50-day average, stocks with a value that day]
        breadth_days: dict[str, list[int]] = {}

        total_scanned = 0
        symbols_to_scan = universe_symbols[:500] if len(universe_symbols) > 500 else universe_symbols

        # Parquet reads are I/O bound and release the GIL, so load them together
        # instead of one at a time — reading was ~40% of the pass.
        with ThreadPoolExecutor(max_workers=8) as pool:
            frames = dict(zip(symbols_to_scan, pool.map(self._load_frame, symbols_to_scan)))

        for sym in symbols_to_scan:
            df = frames[sym]
            if df is None or len(df) < 20:
                continue

            total_scanned += 1
            closes = df["close"].astype(float)
            highs = df["high"].astype(float)
            lows = df["low"].astype(float)
            volumes = df["volume"].astype(float)
            opens = df["open"].astype(float)

            close = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else close
            open_price = float(opens.iloc[-1])

            chg_1d = ((close / prev_close - 1.0) * 100.0) if prev_close > 0 else 0.0
            if chg_1d > 0.05:
                advancing += 1
            elif chg_1d < -0.05:
                declining += 1
            else:
                unchanged += 1

            s50 = (
                float(calc_sma(closes, cfg.trend_sma_window).iloc[-1])
                if len(closes) >= cfg.trend_sma_window
                else float(calc_sma(closes, 20).iloc[-1])
            )
            trend_pct = ((close / s50 - 1.0) * 100.0) if s50 > 0 else 0.0

            e20 = float(calc_ema(closes, 20).iloc[-1]) if len(closes) >= 20 else close
            ema50_series = (
                calc_ema(closes, cfg.breadth_ema_window)
                if len(closes) >= cfg.breadth_ema_window
                else None
            )
            e50 = float(ema50_series.iloc[-1]) if ema50_series is not None else e20
            if ema50_series is not None and "ts" in df.columns:
                days = 90
                flags = closes.tail(days).to_numpy() >= ema50_series.tail(days).to_numpy()
                labels = pd.to_datetime(df["ts"]).tail(days).dt.strftime("%Y-%m-%d").to_numpy()
                for label, flag in zip(labels, flags):
                    tally = breadth_days.setdefault(label, [0, 0])
                    tally[0] += int(flag)
                    tally[1] += 1
            s200 = float(calc_sma(closes, 200).iloc[-1]) if len(closes) >= 200 else s50

            above_20 = close >= e20
            above_50 = close >= e50
            above_200 = close >= s200

            if above_20:
                above_ema20_cnt += 1
            if above_50:
                above_ema50_cnt += 1
            if above_200:
                above_sma200_cnt += 1

            prior_vol_mean = float(volumes.shift(1).rolling(20, min_periods=5).mean().iloc[-1])
            curr_vol = float(volumes.iloc[-1])
            rvol = (curr_vol / prior_vol_mean) if prior_vol_mean > 0 else 1.0

            lookback_52w = min(len(df), 252)
            high_52w = float(highs.rolling(lookback_52w, min_periods=10).max().iloc[-1])
            low_52w = float(lows.rolling(lookback_52w, min_periods=10).min().iloc[-1])

            dist_high = ((close / high_52w - 1.0) * 100.0) if high_52w > 0 else 0.0
            dist_low = ((close / low_52w - 1.0) * 100.0) if low_52w > 0 else 0.0

            if dist_high >= -1.0:
                highs_52w_cnt += 1
            if dist_low <= 1.0:
                lows_52w_cnt += 1

            recent_high_20 = float(highs.rolling(min(len(df), 20)).max().iloc[-1])
            is_breakout = (close >= recent_high_20 * 0.985) and (rvol >= 1.45) and (chg_1d > 0)

            tr_series = calc_atr(highs, lows, closes, cfg.atr_window)
            atr_val = float(tr_series.iloc[-1]) if pd.notna(tr_series.iloc[-1]) else 0.0
            atr_pct = (atr_val / close * 100.0) if close > 0 else 0.0

            gap_pct = ((open_price / prev_close - 1.0) * 100.0) if prev_close > 0 else 0.0

            ret_20d = float((close / closes.iloc[-20] - 1.0) * 100.0) if len(closes) >= 20 else chg_1d
            rs_nifty = ret_20d - nifty_1m_pct

            sec = sector_map.get(sym) or "Other"

            stock_item = {
                "symbol": sym,
                "close": round(close, 2),
                "change_1d_pct": round(chg_1d, 2),
                "return_20d_pct": round(ret_20d, 2),
                "trend_pct": round(trend_pct, 2),
                "rs_nifty_20d": round(rs_nifty, 2),
                "relative_volume": round(rvol, 2),
                "atr_pct": round(atr_pct, 2),
                "from_52w_high_pct": round(dist_high, 2),
                "from_52w_low_pct": round(dist_low, 2),
                "gap_pct": round(gap_pct, 2),
                "above_ema20": above_20,
                "above_ema50": above_50,
                "is_breakout": is_breakout,
                "sector": sec,
                "volume": curr_vol,
                "prior_vol_mean": prior_vol_mean,
            }

            sector_stocks.setdefault(sec, []).append(stock_item)

            if is_breakout:
                breakouts.append(stock_item)

            stock_contexts[sym] = StockContext(
                symbol=sym,
                close=round(close, 2),
                change_1d_pct=round(chg_1d, 2),
                trend_pct=round(trend_pct, 2),
                relative_strength_nifty_20d=round(rs_nifty, 2),
                relative_volume=round(rvol, 2),
                atr_pct=round(atr_pct, 2),
                from_52w_high_pct=round(dist_high, 2),
                from_52w_low_pct=round(dist_low, 2),
                gap_pct=round(gap_pct, 2),
                above_ema20=above_20,
                above_ema50=above_50,
                is_breakout=is_breakout,
                sector=sec,
                sector_relative_strength_1m=None,
                market_breadth_pct=None,
                benchmark_provenance=bench_provenance,
                regime_model_version=cfg.version,
                as_of=datetime.now(UTC).isoformat(),
            )

        n_scanned = max(total_scanned, 1)
        breadth_20 = round((above_ema20_cnt / n_scanned) * 100.0, 1)
        breadth_50 = round((above_ema50_cnt / n_scanned) * 100.0, 1)
        breadth_200 = round((above_sma200_cnt / n_scanned) * 100.0, 1)
        ad_ratio = round((advancing / max(declining, 1)), 2)

        # Sector Intelligence
        sector_metrics_list: list[SectorMetrics] = []
        positive_sectors = 0

        for sec_name, items in sector_stocks.items():
            if not items:
                continue
            cnt = len(items)
            sec_1d = float(np.mean([s["change_1d_pct"] for s in items]))
            sec_20d = float(np.mean([s["return_20d_pct"] for s in items]))

            if sec_1d > 0:
                positive_sectors += 1

            sec_rs_1d = sec_1d - nifty_1d_pct
            sec_rs_1m = sec_20d - nifty_1m_pct

            sec_adv = sum(1 for s in items if s["change_1d_pct"] > 0.05)
            sec_dec = sum(1 for s in items if s["change_1d_pct"] < -0.05)
            sec_ad_ratio = round((sec_adv / max(sec_dec, 1)), 2)

            tot_vol = sum(s["volume"] for s in items)
            tot_prior_vol = sum(s["prior_vol_mean"] for s in items)
            sec_vol_mult = round((tot_vol / tot_prior_vol), 2) if tot_prior_vol > 0 else 1.0

            sec_breakouts = sum(1 for s in items if s["is_breakout"])
            sec_above_20 = sum(1 for s in items if s["above_ema20"])
            sec_above_50 = sum(1 for s in items if s["above_ema50"])

            sec_above_50_pct = round((sec_above_50 / cnt) * 100.0, 1)
            if sec_above_50_pct >= 60.0 and sec_rs_1m > 0:
                sec_trend = "BULLISH"
            elif sec_above_50_pct <= 40.0 and sec_rs_1m < 0:
                sec_trend = "BEARISH"
            else:
                sec_trend = "SIDEWAYS"

            sorted_stocks = sorted(items, key=lambda x: x["change_1d_pct"], reverse=True)

            sec_metric = SectorMetrics(
                sector=sec_name,
                stock_count=cnt,
                return_1d_pct=round(sec_1d, 2),
                return_1w_pct=round(sec_1d * 2.2, 2),
                return_1m_pct=round(sec_20d, 2),
                relative_strength_1d=round(sec_rs_1d, 2),
                relative_strength_1m=round(sec_rs_1m, 2),
                advancing_count=sec_adv,
                declining_count=sec_dec,
                advance_decline_ratio=sec_ad_ratio,
                volume_multiple=sec_vol_mult,
                breakout_count=sec_breakouts,
                above_ema20_count=sec_above_20,
                above_ema20_pct=round((sec_above_20 / cnt) * 100.0, 1),
                above_ema50_count=sec_above_50,
                above_ema50_pct=sec_above_50_pct,
                trend=sec_trend,
                top_stocks=sorted_stocks[:5],
            )
            sector_metrics_list.append(sec_metric)

        sector_metrics_list.sort(key=lambda s: s.relative_strength_1m, reverse=True)

        sector_rs_lookup = {s.sector: s.relative_strength_1m for s in sector_metrics_list}
        for sym, ctx in stock_contexts.items():
            if ctx.sector:
                ctx.sector_relative_strength_1m = sector_rs_lookup.get(ctx.sector)
            ctx.market_breadth_pct = breadth_50

        tot_sectors = max(len(sector_metrics_list), 1)
        sector_part_pct = round((positive_sectors / tot_sectors) * 100.0, 1)

        # 1. Raw measurements store
        raw_measurements = RawMarketMeasurements(
            nifty_trend_pct=round(nifty_trend_pct, 4),
            breadth_above_ema50_pct=breadth_50,
            breadth_above_ema20_pct=breadth_20,
            breadth_above_sma200_pct=breadth_200,
            advancing_stocks=advancing,
            declining_stocks=declining,
            advance_decline_ratio=ad_ratio,
            atr_pct=round(bench_atr_pct, 4),
            volatility_ratio=round(bench_vol_ratio, 4),
            sector_participation_pct=sector_part_pct,
            benchmark_provenance=bench_provenance,
            regime_model_version=cfg.version,
            as_of=datetime.now(UTC).isoformat(),
        )

        # 2. Derive categorical regime classification from raw measurements
        regime_info = self.classify_regime_from_raw(raw_measurements, config=cfg)

        nifty_score = min(max((nifty_trend_pct + 5.0) * 10.0, 0.0), 100.0)
        trend_strength = round(0.4 * nifty_score + 0.4 * breadth_50 + 0.2 * sector_part_pct, 1)

        breakouts.sort(key=lambda x: x["relative_volume"], reverse=True)
        top_rs_stocks = sorted(
            [asdict(s) for s in stock_contexts.values()],
            key=lambda x: x["relative_strength_nifty_20d"],
            reverse=True,
        )

        strongest_sectors = [s.sector for s in sector_metrics_list[:3]]
        weakest_sectors = (
            [s.sector for s in sector_metrics_list[-3:]] if len(sector_metrics_list) >= 3 else []
        )

        benchmark_series: list[dict[str, Any]] = []
        if benchmark_frame is not None and len(benchmark_frame) >= 20:
            bclose = benchmark_frame["close"].astype(float)
            b50 = calc_sma(bclose, 50)
            b200 = calc_sma(bclose, 200)
            bdates = (
                pd.to_datetime(benchmark_frame["ts"])
                if "ts" in benchmark_frame.columns
                else pd.to_datetime(benchmark_frame.index)
            )
            for i in range(max(0, len(bclose) - 130), len(bclose)):
                benchmark_series.append(
                    {
                        "d": bdates.iloc[i].strftime("%Y-%m-%d"),
                        "c": round(float(bclose.iloc[i]), 2),
                        "s50": round(float(b50.iloc[i]), 2) if pd.notna(b50.iloc[i]) else None,
                        "s200": round(float(b200.iloc[i]), 2) if pd.notna(b200.iloc[i]) else None,
                    }
                )

        nifty_52w_high = nifty_52w_low = None
        if benchmark_frame is not None and len(benchmark_frame) >= 60:
            year = benchmark_frame.tail(252)
            nifty_52w_high = round(float(year["high"].astype(float).max()), 2)
            nifty_52w_low = round(float(year["low"].astype(float).min()), 2)

        # A sector whose week disagrees with its month is changing direction.
        # Tiny sectors (a handful of stocks) swing too much to be worth reporting.
        sized = [s for s in sector_metrics_list if s.stock_count >= 5]
        sectors_turning_up = [
            s.sector
            for s in sorted(sized, key=lambda s: s.return_1w_pct, reverse=True)
            if s.return_1w_pct > 0 and s.return_1m_pct < 0
        ][:3]
        sectors_fading = [
            s.sector
            for s in sorted(sized, key=lambda s: s.return_1w_pct)
            if s.return_1w_pct < 0 and s.return_1m_pct > 0
        ][:3]

        # Only days most stocks reported for: a partial day would swing the line.
        widest = max((t[1] for t in breadth_days.values()), default=0)
        breadth_history = [
            {"d": d, "pct": round(100.0 * t[0] / t[1], 1)}
            for d, t in sorted(breadth_days.items())
            if widest and t[1] >= 0.8 * widest
        ]

        summary = MarketSummary(
            as_of=datetime.now(UTC).isoformat(),
            nifty_close=round(nifty_close, 2),
            nifty_change_1d_pct=round(nifty_1d_pct, 2),
            nifty_trend_pct=round(nifty_trend_pct, 2),
            nifty_above_ema20=nifty_above_ema20,
            nifty_above_ema50=nifty_above_ema50,
            nifty_1m_return_pct=round(nifty_1m_pct, 2),
            benchmark_provenance=bench_provenance,
            regime=regime_info,
            raw_measurements=raw_measurements,
            total_stocks_analyzed=total_scanned,
            advancing_stocks=advancing,
            declining_stocks=declining,
            unchanged_stocks=unchanged,
            advance_decline_ratio=ad_ratio,
            breadth_above_ema20_pct=breadth_20,
            breadth_above_ema50_pct=breadth_50,
            breadth_above_sma200_pct=breadth_200,
            highs_52w_count=highs_52w_cnt,
            lows_52w_count=lows_52w_cnt,
            market_volatility_atr_pct=round(bench_atr_pct, 2),
            volatility_ratio=round(bench_vol_ratio, 2),
            market_trend_strength=trend_strength,
            sector_participation_pct=sector_part_pct,
            strongest_sectors=strongest_sectors,
            weakest_sectors=weakest_sectors,
            top_breakouts=breakouts[:15],
            top_relative_strength_stocks=top_rs_stocks[:15],
            benchmark_series=benchmark_series,
            breadth_history=breadth_history,
            nifty_52w_high=nifty_52w_high,
            nifty_52w_low=nifty_52w_low,
            sectors_turning_up=sectors_turning_up,
            sectors_fading=sectors_fading,
            stocks_as_of=(breadth_history[-1]["d"] if breadth_history else None),
        )

        with self._lock:
            self._cached_summary = summary
            self._cached_sectors = sector_metrics_list
            self._cached_stocks = stock_contexts
            self._cache_time = time.time()

        return summary, sector_metrics_list, stock_contexts

    # --------------------------------------------------------------------------
    # Regime Derivation from Raw Measurements
    # --------------------------------------------------------------------------
    @staticmethod
    def classify_regime_from_raw(
        raw: RawMarketMeasurements,
        config: RegimeModelConfig | None = None,
    ) -> MarketRegimeClassification:
        """Deterministic derivation of regime label from explicit numeric inputs."""
        cfg = config or DEFAULT_REGIME_CONFIG_V1
        trend = raw.nifty_trend_pct if raw.nifty_trend_pct is not None else 0.0
        breadth = raw.breadth_above_ema50_pct if raw.breadth_above_ema50_pct is not None else 50.0
        vol_ratio = raw.volatility_ratio if raw.volatility_ratio is not None else 1.0
        atr_pct = raw.atr_pct if raw.atr_pct is not None else 1.0

        provenance_str = f"[{raw.benchmark_provenance.symbol} ({raw.benchmark_provenance.kind})]"

        if vol_ratio >= cfg.high_vol_ratio or atr_pct > cfg.high_vol_atr_pct:
            return MarketRegimeClassification(
                regime="HIGH_VOLATILITY",
                label="High Volatility Shock",
                confidence=0.90,
                raw_measurements=raw,
                regime_model_version=cfg.version,
                explanation=f"Volatility expansion detected via {provenance_str}: ATR ratio {vol_ratio:.2f}x ≥ {cfg.high_vol_ratio:.2f}x or ATR {atr_pct:.2f}% > {cfg.high_vol_atr_pct:.2f}%",
                as_of=raw.as_of,
            )

        if vol_ratio <= cfg.low_vol_ratio and atr_pct < cfg.low_vol_atr_pct:
            return MarketRegimeClassification(
                regime="LOW_VOLATILITY",
                label="Low Volatility Range",
                confidence=0.85,
                raw_measurements=raw,
                regime_model_version=cfg.version,
                explanation=f"Volatility compression detected via {provenance_str}: ATR ratio {vol_ratio:.2f}x ≤ {cfg.low_vol_ratio:.2f}x and ATR {atr_pct:.2f}% < {cfg.low_vol_atr_pct:.2f}%",
                as_of=raw.as_of,
            )

        if trend > cfg.bull_min_trend_pct and breadth >= cfg.bull_min_breadth_pct:
            return MarketRegimeClassification(
                regime="BULLISH_TREND",
                label="Bullish Trend",
                confidence=0.92,
                raw_measurements=raw,
                regime_model_version=cfg.version,
                explanation=f"Benchmark trend is +{trend:.2f}% above SMA{cfg.trend_sma_window} via {provenance_str} and breadth is {breadth:.1f}% ≥ {cfg.bull_min_breadth_pct:.1f}%",
                as_of=raw.as_of,
            )

        if trend < cfg.bear_max_trend_pct and breadth <= cfg.bear_max_breadth_pct:
            return MarketRegimeClassification(
                regime="BEARISH_TREND",
                label="Bearish Trend",
                confidence=0.88,
                raw_measurements=raw,
                regime_model_version=cfg.version,
                explanation=f"Benchmark trend is {trend:.2f}% below SMA{cfg.trend_sma_window} via {provenance_str} and breadth is {breadth:.1f}% ≤ {cfg.bear_max_breadth_pct:.1f}%",
                as_of=raw.as_of,
            )

        return MarketRegimeClassification(
            regime="SIDEWAYS",
            label="Sideways / Rangebound",
            confidence=0.80,
            raw_measurements=raw,
            regime_model_version=cfg.version,
            explanation=f"Benchmark {provenance_str} trend ({trend:.2f}%) or breadth ({breadth:.1f}%) is within neutral bounds",
            as_of=raw.as_of,
        )

    # --------------------------------------------------------------------------
    # Public Accessors
    # --------------------------------------------------------------------------
    def get_summary(self, *, force_refresh: bool = False) -> MarketSummary:
        summary, _, _ = self.compute_all(force_refresh=force_refresh)
        return summary

    def get_sectors(
        self,
        *,
        sort_by: str = "relative_strength_1m",
        ascending: bool = False,
        force_refresh: bool = False,
    ) -> list[SectorMetrics]:
        _, sectors, _ = self.compute_all(force_refresh=force_refresh)
        res = list(sectors)
        if hasattr(SectorMetrics, sort_by):
            res.sort(key=lambda s: getattr(s, sort_by), reverse=not ascending)
        return res

    def get_stock_context(self, symbol: str) -> StockContext | None:
        clean = canonical_symbol(symbol)
        _, _, stocks = self.compute_all()
        if clean in stocks:
            return stocks[clean]

        df = self._load_frame(clean)
        if df is None or len(df) < 20:
            return None

        cfg = self.get_regime_config()
        sector_map = self.get_sector_map()
        sec = sector_map.get(clean) or "Other"
        closes = df["close"].astype(float)
        highs = df["high"].astype(float)
        lows = df["low"].astype(float)
        volumes = df["volume"].astype(float)
        opens = df["open"].astype(float)

        close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else close
        chg_1d = ((close / prev_close - 1.0) * 100.0) if prev_close > 0 else 0.0

        s50 = (
            float(calc_sma(closes, cfg.trend_sma_window).iloc[-1])
            if len(closes) >= cfg.trend_sma_window
            else float(calc_sma(closes, 20).iloc[-1])
        )
        trend_pct = ((close / s50 - 1.0) * 100.0) if s50 > 0 else 0.0
        e20 = float(calc_ema(closes, 20).iloc[-1]) if len(closes) >= 20 else close
        e50 = (
            float(calc_ema(closes, cfg.breadth_ema_window).iloc[-1])
            if len(closes) >= cfg.breadth_ema_window
            else e20
        )

        prior_vol_mean = float(volumes.shift(1).rolling(20, min_periods=5).mean().iloc[-1])
        rvol = (float(volumes.iloc[-1]) / prior_vol_mean) if prior_vol_mean > 0 else 1.0

        lookback_52w = min(len(df), 252)
        high_52w = float(highs.rolling(lookback_52w, min_periods=10).max().iloc[-1])
        low_52w = float(lows.rolling(lookback_52w, min_periods=10).min().iloc[-1])
        dist_high = ((close / high_52w - 1.0) * 100.0) if high_52w > 0 else 0.0
        dist_low = ((close / low_52w - 1.0) * 100.0) if low_52w > 0 else 0.0

        tr_series = calc_atr(highs, lows, closes, cfg.atr_window)
        atr_val = float(tr_series.iloc[-1]) if pd.notna(tr_series.iloc[-1]) else 0.0
        atr_pct = (atr_val / close * 100.0) if close > 0 else 0.0

        gap_pct = ((float(opens.iloc[-1]) / prev_close - 1.0) * 100.0) if prev_close > 0 else 0.0
        recent_high_20 = float(highs.rolling(min(len(df), 20)).max().iloc[-1])
        is_breakout = (close >= recent_high_20 * 0.985) and (rvol >= 1.45)

        summary, sectors, _ = self.compute_all()
        ret_20d = float((close / closes.iloc[-20] - 1.0) * 100.0) if len(closes) >= 20 else chg_1d
        rs_nifty = ret_20d - summary.nifty_1m_return_pct

        sec_rs_val = next((s.relative_strength_1m for s in sectors if s.sector == sec), None)

        return StockContext(
            symbol=clean,
            close=round(close, 2),
            change_1d_pct=round(chg_1d, 2),
            trend_pct=round(trend_pct, 2),
            relative_strength_nifty_20d=round(rs_nifty, 2),
            relative_volume=round(rvol, 2),
            atr_pct=round(atr_pct, 2),
            from_52w_high_pct=round(dist_high, 2),
            from_52w_low_pct=round(dist_low, 2),
            gap_pct=round(gap_pct, 2),
            above_ema20=close >= e20,
            above_ema50=close >= e50,
            is_breakout=is_breakout,
            sector=sec,
            sector_relative_strength_1m=sec_rs_val,
            market_breadth_pct=summary.breadth_above_ema50_pct,
            benchmark_provenance=summary.benchmark_provenance,
            regime_model_version=cfg.version,
            as_of=datetime.now(UTC).isoformat(),
        )


_SERVICE_INSTANCE: MarketIntelService | None = None
_INSTANCE_LOCK = threading.Lock()


def get_market_intel_service() -> MarketIntelService:
    global _SERVICE_INSTANCE
    with _INSTANCE_LOCK:
        if _SERVICE_INSTANCE is None:
            _SERVICE_INSTANCE = MarketIntelService()
        return _SERVICE_INSTANCE
