"""CSV / Parquet backed feed.

Expects either:
  * one file per symbol named ``<SYMBOL>.csv`` inside ``directory``, or
  * a single long-format file passed via ``path``.

Required columns: ts, open, high, low, close. Optional: volume, vwap,
open_interest.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from atr.core.enums import AssetClass, Timeframe
from atr.core.models import Instrument, MarketSnapshot
from atr.data.base import DataFeed, filter_snapshots, pivot_to_snapshots

REQUIRED = {"ts", "open", "high", "low", "close"}


class CsvFeed(DataFeed):
    def __init__(
        self,
        directory: str | Path | None = None,
        path: str | Path | None = None,
        timeframe: Timeframe = Timeframe.MIN_1,
        instruments: dict[str, Instrument] | None = None,
    ) -> None:
        if not directory and not path:
            raise ValueError("provide either `directory` or `path`")
        self.timeframe = timeframe
        self._frame = self._read(directory, path)
        self._instruments = instruments or self._default_instruments()

    # ------------------------------------------------------------------
    def _read(self, directory, path) -> pd.DataFrame:
        if directory:
            frames = []
            for file in sorted(Path(directory).glob("*.csv")):
                df = pd.read_csv(file)
                df["symbol"] = file.stem.upper()
                frames.append(df)
            if not frames:
                raise FileNotFoundError(f"no CSV files found in {directory}")
            out = pd.concat(frames, ignore_index=True)
        else:
            out = pd.read_csv(Path(path))
            if "symbol" not in out.columns:
                raise ValueError("single-file feeds require a `symbol` column")

        out.columns = [c.strip().lower() for c in out.columns]
        missing = REQUIRED - set(out.columns)
        if missing:
            raise ValueError(f"missing columns: {sorted(missing)}")
        out["ts"] = pd.to_datetime(out["ts"], utc=True)
        for col in ("open", "high", "low", "close", "volume", "vwap", "open_interest"):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        return out.dropna(subset=["open", "high", "low", "close"]).sort_values(["ts", "symbol"])

    def _default_instruments(self) -> dict[str, Instrument]:
        return {
            sym: Instrument(symbol=sym, asset_class=AssetClass.EQUITY)
            for sym in sorted(self._frame["symbol"].unique())
        }

    # ------------------------------------------------------------------
    @property
    def instruments(self) -> dict[str, Instrument]:
        return self._instruments

    def load(self, start=None, end=None) -> list[MarketSnapshot]:
        snaps = pivot_to_snapshots(self._frame, self.timeframe)
        return filter_snapshots(snaps, start, end)

    @property
    def frame(self) -> pd.DataFrame:
        return self._frame


class ParquetFeed(CsvFeed):
    """Same contract as CsvFeed but backed by Parquet (much faster for years
    of minute data)."""

    def _read(self, directory, path) -> pd.DataFrame:  # type: ignore[override]
        src = Path(path) if path else Path(directory)
        if src.is_dir():
            frames = []
            for file in sorted(src.glob("*.parquet")):
                df = pd.read_parquet(file)
                df["symbol"] = file.stem.upper()
                frames.append(df)
            out = pd.concat(frames, ignore_index=True)
        else:
            out = pd.read_parquet(src)
        out.columns = [c.strip().lower() for c in out.columns]
        out["ts"] = pd.to_datetime(out["ts"], utc=True)
        return out.sort_values(["ts", "symbol"])
