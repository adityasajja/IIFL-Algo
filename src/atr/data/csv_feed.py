"""CSV / Parquet backed feed.

Expects either:
  * one file per symbol named ``<SYMBOL>.csv`` inside ``directory``, or
  * a single long-format file passed via ``path``.

Required columns: ts, open, high, low, close. Optional: volume, vwap,
open_interest.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

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
    def _read(self, directory, path) -> pl.DataFrame:
        if directory:
            frames = []
            for file in sorted(Path(directory).glob("*.csv")):
                df = pl.read_csv(file).with_columns(pl.lit(file.stem.upper()).alias("symbol"))
                frames.append(df)
            if not frames:
                raise FileNotFoundError(f"no CSV files found in {directory}")
            out = pl.concat(frames, how="diagonal_relaxed")
        else:
            out = pl.read_csv(Path(path))
            if "symbol" not in out.columns:
                raise ValueError("single-file feeds require a `symbol` column")

        out = out.rename({c: c.strip().lower() for c in out.columns})
        missing = REQUIRED - set(out.columns)
        if missing:
            raise ValueError(f"missing columns: {sorted(missing)}")
        out = _parse_ts(out)
        for col in ("open", "high", "low", "close", "volume", "vwap", "open_interest"):
            if col in out.columns:
                out = out.with_columns(pl.col(col).cast(pl.Float64, strict=False))
        return out.drop_nulls(subset=["open", "high", "low", "close"]).sort(["ts", "symbol"])

    def _default_instruments(self) -> dict[str, Instrument]:
        return {
            sym: Instrument(symbol=sym, asset_class=AssetClass.EQUITY)
            for sym in sorted(self._frame["symbol"].unique().to_list())
        }

    # ------------------------------------------------------------------
    @property
    def instruments(self) -> dict[str, Instrument]:
        return self._instruments

    def load(self, start=None, end=None) -> list[MarketSnapshot]:
        snaps = pivot_to_snapshots(self._frame, self.timeframe)
        return filter_snapshots(snaps, start, end)

    @property
    def frame(self) -> pl.DataFrame:
        return self._frame


def _parse_ts(df: pl.DataFrame) -> pl.DataFrame:
    """Parse the ts column (string or already-temporal) to UTC datetime."""
    if df.schema["ts"] == pl.Utf8:
        return df.with_columns(pl.col("ts").str.to_datetime(time_zone="UTC", strict=False))
    ts = df["ts"]
    expr = pl.col("ts").dt.replace_time_zone("UTC") if ts.dtype.time_zone is None else pl.col("ts").dt.convert_time_zone("UTC")
    return df.with_columns(expr)


class ParquetFeed(CsvFeed):
    """Same contract as CsvFeed but backed by Parquet (much faster for years
    of minute data)."""

    def _read(self, directory, path) -> pl.DataFrame:  # type: ignore[override]
        src = Path(path) if path else Path(directory)
        if src.is_dir():
            frames = []
            for file in sorted(src.glob("*.parquet")):
                df = pl.read_parquet(file).with_columns(pl.lit(file.stem.upper()).alias("symbol"))
                frames.append(df)
            out = pl.concat(frames, how="diagonal_relaxed")
        else:
            out = pl.read_parquet(src)
            if "symbol" not in [c.lower() for c in out.columns]:
                out = out.with_columns(pl.lit(src.stem.upper()).alias("symbol"))
        out = out.rename({c: c.strip().lower() for c in out.columns})
        out = _parse_ts(out)
        return out.sort(["ts", "symbol"])
