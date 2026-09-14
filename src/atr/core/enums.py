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

    @classmethod
    def parse(cls, value: "OrderType | str") -> "OrderType":
        """Canonicalise any spelling of an order type, or refuse it.

        Every layer that handles an order needs this and each one spelling it out
        for itself is how the platform ended up calling
        ``OrderType.SL_MARKET`` — a member that does not exist. The signal-execute
        route used it on the *second* leg of a three-leg bracket, so the entry
        order had already been transmitted when the ``AttributeError`` fired: a
        live position with no stop-loss, and a 500 that told the operator nothing.

        So the spellings live here, next to the members, and a caller that wants
        to accept ``SL-M`` gets it from the same place the enum does. Raising is
        the point: an unknown type must be refused *before* anything is sent, not
        discovered halfway through a bracket.
        """
        if isinstance(value, cls):
            return value
        # Normalise separators so `SL-M`, `sl_m` and `SL M` are one spelling.
        # Only spaces and hyphens: the underscore is already the canonical form.
        key = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        try:
            return cls(key)
        except ValueError:
            pass
        aliased = _ORDER_TYPE_ALIASES.get(key)
        if aliased is not None:
            return cls(aliased)
        valid = ", ".join(sorted({m.value for m in cls} | set(_ORDER_TYPE_ALIASES)))
        raise ValueError(f"unknown order type {value!r}; expected one of: {valid}")


#: Spellings a broker or an operator may use, mapped onto the canonical member.
#: Keys are already separator-normalised (see :meth:`OrderType.parse`), so
#: ``SL-M`` arrives here as ``SL_M``. ``SL`` is a stop-*limit* order and ``SL-M``
#: a stop-*market* one; conflating them would silently attach a limit price to a
#: market stop, or drop it.
_ORDER_TYPE_ALIASES: dict[str, str] = {
    "SL_M": "STOP",
    "SLM": "STOP",
    "STOP_MARKET": "STOP",
    "STOPMARKET": "STOP",
    "SL": "STOP_LIMIT",
    "SL_L": "STOP_LIMIT",
    "STOP_LIMIT_ORDER": "STOP_LIMIT",
}


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
