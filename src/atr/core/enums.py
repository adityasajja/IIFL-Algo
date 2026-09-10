from enum import Enum, IntEnum


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    FOREX = "FOREX"
    CRYPTO = "CRYPTO"
    INDEX = "INDEX"


class OptionType(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class Side(IntEnum):
    """Direction of an order or a position.

    IntEnum so it can be used directly as a signed multiplier (+1 / -1)
    in P&L arithmetic.
    """

    BUY = 1
    SELL = -1

    @property
    def sign(self) -> int:
        return int(self)

    @property
    def closing(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class TimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class Timeframe(str, Enum):
    TICK = "tick"
    SEC_1 = "1s"
    MIN_1 = "1m"
    MIN_5 = "5m"
    MIN_15 = "15m"
    MIN_30 = "30m"
    HOUR_1 = "1h"
    DAY_1 = "1d"

    @property
    def seconds(self) -> int | None:
        return {
            Timeframe.TICK: None,
            Timeframe.SEC_1: 1,
            Timeframe.MIN_1: 60,
            Timeframe.MIN_5: 300,
            Timeframe.MIN_15: 900,
            Timeframe.MIN_30: 1800,
            Timeframe.HOUR_1: 3600,
            Timeframe.DAY_1: 86400,
        }[self]

    @property
    def bars_per_year(self) -> float:
        """Used to annualise per-bar statistics."""
        sec = self.seconds
        if not sec:
            raise ValueError("bars_per_year is undefined for tick data")
        return (365 * 24 * 3600) / sec


class TradingSession(str, Enum):
    CONTINUOUS = "CONTINUOUS"  # crypto / FX
    REGULAR = "REGULAR"  # exchange primary session only
