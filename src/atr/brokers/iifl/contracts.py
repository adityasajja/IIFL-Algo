"""Instrument master (contract files).

The IIFL contract files are downloaded per segment, e.g.
``GET /v1/contractfiles/NSEFO.json``. Field naming differs slightly across
segments/files, so :func:`normalize_contract` tolerates the common spellings
rather than assuming one schema. Run ``atr instruments sync`` and then eyeball
the output once against your own file — better to fail loudly at load time
than to trade the wrong contract.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from loguru import logger

from atr.core.enums import AssetClass, OptionType
from atr.core.models import Instrument

CACHE_DIR = Path(".cache/contracts")


def _pick(row: dict, *keys: str, default=None):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return default


def _parse_expiry(value) -> date | None:
    if value in (None, "", "NA", 0):
        return None
    if isinstance(value, date):
        return value
    text = str(value).replace("T00:00:00", "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%Y%m%d", "%d %b %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(text, errors="coerce").date()
    except Exception:  # noqa: BLE001
        return None


def _asset_class(exchange: str, instrument_type: str | None) -> AssetClass:
    ex = exchange.upper()
    if ex.endswith("FO") or ex.endswith("COMM"):
        itype = (instrument_type or "").upper()
        if "OPT" in itype or "CE" in itype or "PE" in itype:
            return AssetClass.OPTION
        if "FUT" in itype:
            return AssetClass.FUTURE
        return AssetClass.FUTURE
    if ex.endswith("CURR"):
        return AssetClass.FUTURE
    if ex == "INDICES":
        return AssetClass.INDEX
    return AssetClass.EQUITY


def normalize_contract(row: dict, exchange: str) -> Instrument:
    """Map one raw contract-file row onto our domain :class:`Instrument`."""
    ex = exchange.upper()
    instrument_id = _pick(row, "instrumentId", "InstrumentId", "ExchangeInstrumentId",
                          "exchangeInstrumentId", "id", "ID")
    if instrument_id is None:
        raise ValueError(f"contract row has no instrument id: {row}")

    trading_symbol = str(_pick(row, "tradingSymbol", "TradingSymbol", "symbol", "Symbol",
                               "name", "Name", default=str(instrument_id)))
    description = _pick(row, "name", "Name", "description", "Description", "companyName")
    instrument_type = _pick(row, "instrumentType", "InstrumentType", "series", "Series",
                            "instrumentName")
    lot_size = float(_pick(row, "lotSize", "LotSize", "marketLot", "MarketLot",
                           "boardLot", default=1) or 1)
    tick_size = float(_pick(row, "tickSize", "TickSize", "tick", default=0.05) or 0.05)
    multiplier = float(_pick(row, "multiplier", "Multiplier", "contractMultiplier",
                             "contractSize", default=0) or 0)
    strike = _pick(row, "strike", "StrikePrice", "strikePrice")
    option_type_raw = _pick(row, "optionType", "OptionType", "option_type")

    asset_class = _asset_class(ex, instrument_type)
    if asset_class in (AssetClass.FUTURE, AssetClass.OPTION):
        multiplier = multiplier or lot_size
    else:
        multiplier = multiplier or 1.0

    option_type = None
    if option_type_raw:
        text = str(option_type_raw).upper()
        if text.startswith("C"):
            option_type = OptionType.CALL
        elif text.startswith("P"):
            option_type = OptionType.PUT

    return Instrument(
        symbol=trading_symbol.strip().upper(),
        asset_class=asset_class,
        currency="INR",
        exchange=ex,
        multiplier=multiplier,
        tick_size=tick_size,
        min_quantity=lot_size if asset_class in (AssetClass.FUTURE, AssetClass.OPTION) else 1.0,
        quantity_step=lot_size if asset_class in (AssetClass.FUTURE, AssetClass.OPTION) else 1.0,
        expiry=_parse_expiry(_pick(row, "expiry", "ExpiryDate", "expiryDate", "Expiry")),
        strike=float(strike) if strike not in (None, "") else None,
        option_type=option_type,
        underlying=str(_pick(row, "underlying", "Underlying", default="") or "").upper() or None,
        conid=int(instrument_id),
        local_symbol=str(description or trading_symbol)[:64],
        trading_class=str(instrument_type or "")[:32] or None,
    )


class InstrumentMaster:
    """Loads, caches, and queries the contract universe."""

    def __init__(self, client=None, cache_dir: Path | str = CACHE_DIR) -> None:
        self.client = client
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._frame: pd.DataFrame | None = None

    # ------------------------------------------------------------------
    def sync(self, exchanges: list[str], force: bool = False) -> pd.DataFrame:
        frames = []
        for ex in exchanges:
            path = self.cache_dir / f"{ex.upper()}.json"
            if force or not path.exists():
                if self.client is None:
                    raise RuntimeError("no client provided and cache is cold")
                rows = self.client.contract_file(ex)
                path.write_text(json.dumps(rows), encoding="utf8")
                logger.info("downloaded {} contracts for {}", len(rows), ex)
            else:
                rows = json.loads(path.read_text(encoding="utf8"))

            instruments = []
            for row in rows:
                try:
                    instruments.append(normalize_contract(row, ex))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("skipping contract row in {}: {}", ex, exc)
            frames.append(pd.DataFrame([i.model_dump() for i in instruments]))

        self._frame = pd.concat(frames, ignore_index=True)
        logger.info("instrument master holds {} contracts", len(self._frame))
        return self._frame

    def load_cached(self, exchanges: list[str]) -> pd.DataFrame:
        return self.sync(exchanges, force=False)

    # ------------------------------------------------------------------
    @property
    def frame(self) -> pd.DataFrame:
        if self._frame is None:
            raise RuntimeError("instrument master not loaded — call sync() first")
        return self._frame

    def find(
        self,
        symbol: str,
        exchange: str | None = None,
        expiry: date | None = None,
        strike: float | None = None,
        option_type: OptionType | None = None,
    ) -> Instrument:
        df = self.frame
        mask = df["symbol"].str.upper() == symbol.upper()
        if exchange:
            mask &= df["exchange"].str.upper() == exchange.upper()
        if expiry:
            mask &= df["expiry"] == expiry
        if strike is not None:
            mask &= (df["strike"] - strike).abs() < 1e-9
        if option_type:
            mask &= df["option_type"] == option_type
        hits = df[mask]
        if hits.empty:
            raise KeyError(f"no contract matching symbol={symbol} exchange={exchange}")
        return Instrument(**hits.iloc[0].to_dict())

    def search(self, text: str, exchange: str | None = None, limit: int = 25) -> pd.DataFrame:
        df = self.frame
        mask = df["symbol"].str.contains(text.upper(), na=False)
        if exchange:
            mask &= df["exchange"].str.upper() == exchange.upper()
        return df[mask].head(limit)

    def nearest_expiry(self, symbol: str, exchange: str, after: date | None = None) -> date:
        after = after or date.today()
        df = self.frame
        mask = (
            (df["symbol"].str.upper() == symbol.upper())
            & (df["exchange"].str.upper() == exchange.upper())
            & (df["expiry"].notna())
        )
        expiries = sorted(e for e in df[mask]["expiry"].dropna().unique() if e >= after)
        if not expiries:
            raise KeyError(f"no expiry for {symbol} on {exchange} after {after}")
        return expiries[0]
