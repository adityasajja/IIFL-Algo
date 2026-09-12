"""Synthetic market data generator.

Used for engine tests and demos so we never depend on a live broker or a
populated database to validate the backtester. The generator produces
correlated GBM paths with intraday volatility seasonality (U-shape), which is
enough to exercise fills, margin, and metrics realistically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime

import numpy as np
import pandas as pd

from atr.core.enums import AssetClass, Timeframe
from atr.core.models import Instrument, MarketSnapshot
from atr.data.base import DataFeed, pivot_to_snapshots


@dataclass
class SyntheticConfig:
    symbols: tuple[str, ...] = ("AAPL", "MSFT")
    start: datetime = datetime(2024, 1, 1, 9, 30)
    end: datetime = datetime(2024, 12, 31, 15, 59)
    freq: str = "1min"
    start_price: float = 150.0
    annual_drift: float = 0.08
    annual_vol: float = 0.25
    seed: int = 42
    session_open: dtime = dtime(9, 30)
    session_close: dtime = dtime(15, 59)
    base_volume: float = 1_000.0


def _intraday_vol_multiplier(index: pd.DatetimeIndex, close_t: dtime, open_t: dtime) -> np.ndarray:
    """U-shaped intraday volatility: high at open, low midday, high at close."""
    minutes = index.hour * 60 + index.minute
    open_m = open_t.hour * 60 + open_t.minute
    close_m = close_t.hour * 60 + close_t.minute
    span = max(close_m - open_m, 1)
    frac = (minutes - open_m) / span
    frac = np.clip(frac, 0.0, 1.0)
    return 0.6 + 1.6 * (2 * frac - 1) ** 2


def generate_bars(cfg: SyntheticConfig) -> pd.DataFrame:
    rng = np.random.default_rng(cfg.seed)

    # Business-day intraday grid
    days = pd.bdate_range(cfg.start.date(), cfg.end.date())
    times = pd.date_range(
        f"1900-01-01 {cfg.session_open}", f"1900-01-01 {cfg.session_close}", freq=cfg.freq
    ).time
    index = pd.DatetimeIndex(
        sorted(
            pd.Timestamp.combine(d, t)
            for d in days
            for t in times
        )
    )
    n = len(index)
    steps_per_year = 252 * max(len(times), 1)
    dt = 1.0 / steps_per_year

    frames = []
    for i, sym in enumerate(cfg.symbols):
        # Correlated shocks: a shared market factor plus idiosyncratic noise.
        market = rng.standard_normal(n)
        idio = rng.standard_normal(n)
        shock = 0.6 * market + 0.8 * idio
        shock /= np.sqrt(0.6**2 + 0.8**2)

        vol_mult = _intraday_vol_multiplier(index, cfg.session_close, cfg.session_open)
        sigma = cfg.annual_vol * vol_mult
        mu = (cfg.annual_drift - 0.5 * cfg.annual_vol**2) * dt
        log_ret = mu + sigma * np.sqrt(dt) * shock

        close = cfg.start_price * (1 + i * 0.35) * np.exp(np.cumsum(log_ret))
        open_ = np.concatenate([[close[0]], close[:-1]])
        # Intraday range scales with volatility
        range_ = np.abs(rng.standard_normal(n)) * close * (sigma / np.sqrt(steps_per_year)) * 0.9
        high = np.maximum(open_, close) + range_ * 0.5
        low = np.minimum(open_, close) - range_ * 0.5
        low = np.maximum(low, 0.01)
        volume = cfg.base_volume * (1 + np.abs(rng.standard_normal(n)) * 0.6)

        frames.append(
            pd.DataFrame(
                {
                    "ts": index,
                    "symbol": sym,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                }
            )
        )

    return pd.concat(frames, ignore_index=True).sort_values(["ts", "symbol"])


class SyntheticFeed(DataFeed):
    """Feed backed by :func:`generate_bars`."""

    def __init__(self, cfg: SyntheticConfig | None = None, futures: bool = False) -> None:
        self.cfg = cfg or SyntheticConfig()
        self._futures = futures
        self._frame = generate_bars(self.cfg)

    @property
    def instruments(self) -> dict[str, Instrument]:
        out = {}
        for sym in self.cfg.symbols:
            if self._futures:
                out[sym] = Instrument(
                    symbol=sym,
                    asset_class=AssetClass.FUTURE,
                    multiplier=100.0,
                    initial_margin_per_unit=25.0,
                    maintenance_margin_per_unit=20.0,
                    exchange="CME",
                )
            else:
                out[sym] = Instrument(
                    symbol=sym,
                    asset_class=AssetClass.EQUITY,
                    multiplier=1.0,
                    exchange="SMART",
                    primary_exchange="NASDAQ",
                )
        return out

    def load(self) -> list[MarketSnapshot]:
        return pivot_to_snapshots(self._frame, Timeframe.MIN_1)

    @property
    def frame(self) -> pd.DataFrame:
        return self._frame


__all__ = ["SyntheticConfig", "SyntheticFeed", "generate_bars"]
