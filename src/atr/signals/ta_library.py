"""The broader technical-indicator library, backed by `pandas_ta`.

`atr.signals.formula` already hand-wrote the handful of indicators the chart's
own Pine Script studio offers (SMA, EMA, RSI, MACD, ATR, Bollinger). This
module is the rest of it — `pandas_ta` ships ~150 published indicators across
momentum, trend, volatility, volume and overlap studies; this registers a
curated, verified subset of the well-known ones as safe, single-period
callables a formula can invoke by name (`tema(9)`, `adx(14)`, `supertrend(7)`,
...), the same way `ema`/`rsi`/`atr` already work.

"Curated" rather than every last one of the ~150: several are multi-parameter,
rarely used, or have output shapes that need a human decision about which
column a formula should read (`ichimoku`'s five lines, for instance). This
covers the indicators anyone actually asks for by name; a specific missing one
is a small, mechanical addition here, not a redesign.

Every function here takes exactly `(frame, n)` — a DataFrame and one numeric
period — and returns a plain float, read off the OHLCV columns already on the
frame. Nothing here accepts an arbitrary column, arbitrary kwargs, or
anything else a formula could use to reach outside this contract; the safety
boundary is the same one `atr.signals.formula` already enforces (a formula can
only call a name in this registry, with one numeric-literal argument).
"""

from __future__ import annotations

from typing import Callable

import pandas as pd


def _last(value: pd.Series | pd.DataFrame | None, column_prefix: str | None = None) -> float:
    """The latest reading from whatever `pandas_ta` handed back.

    A single indicator either returns one Series, or a DataFrame of several
    related lines (e.g. `bbands` returns lower/mid/upper/bandwidth/percent).
    `column_prefix` picks which line by matching the start of its pandas_ta
    column name (their own naming convention, e.g. `BBU_20_2.0` for the upper
    band) — case-insensitive, first match wins. `None`/empty/all-NaN reads as
    0.0, the same "no error, no fabricated reading" convention every other
    indicator in `atr.signals.formula` already follows.
    """
    if value is None:
        return 0.0
    if isinstance(value, pd.DataFrame):
        if value.empty:
            return 0.0
        if column_prefix:
            match = next((c for c in value.columns if str(c).upper().startswith(column_prefix.upper())), None)
            series = value[match] if match is not None else value.iloc[:, 0]
        else:
            series = value.iloc[:, 0]
    else:
        series = value
    if series is None or len(series) == 0:
        return 0.0
    val = float(series.iloc[-1])
    return val if val == val else 0.0  # NaN -> 0.0


def _period(n: float) -> int:
    return max(int(n), 1)


def build_ta_functions(frame: pd.DataFrame) -> dict[str, Callable[[float], float]]:
    """`{name: fn(period) -> float}` for every indicator this module registers,
    bound to `frame` for this evaluation. Merged into the formula context
    alongside the hand-written ones in `atr.signals.formula.build_context`.
    """
    import pandas_ta_classic as pta

    h, l, c = frame["high"], frame["low"], frame["close"]
    v = frame["volume"] if "volume" in frame.columns else pd.Series(0.0, index=frame.index)

    fns: dict[str, Callable[[float], float]] = {
        # --- overlap / moving averages ----------------------------------
        "tema": lambda n: _last(pta.tema(c, length=_period(n))),
        "dema": lambda n: _last(pta.dema(c, length=_period(n))),
        "hma": lambda n: _last(pta.hma(c, length=_period(n))),
        "kama": lambda n: _last(pta.kama(c, length=_period(n))),
        "trima": lambda n: _last(pta.trima(c, length=_period(n))),
        "zlma": lambda n: _last(pta.zlma(c, length=_period(n))),
        "vwma": lambda n: _last(pta.vwma(c, v, length=_period(n))),
        "midpoint": lambda n: _last(pta.midpoint(c, length=_period(n))),
        "midprice": lambda n: _last(pta.midprice(h, l, length=_period(n))),

        # --- momentum -----------------------------------------------------
        "cci": lambda n: _last(pta.cci(h, l, c, length=_period(n))),
        "willr": lambda n: _last(pta.willr(h, l, c, length=_period(n))),
        "mfi": lambda n: _last(pta.mfi(h, l, c, v, length=_period(n))),
        "cmo": lambda n: _last(pta.cmo(c, length=_period(n))),
        "roc": lambda n: _last(pta.roc(c, length=_period(n))),
        "mom": lambda n: _last(pta.mom(c, length=_period(n))),
        "trix": lambda n: _last(pta.trix(c, length=_period(n)), "TRIX"),
        "adx": lambda n: _last(pta.adx(h, l, c, length=_period(n)), "ADX"),
        "plus_di": lambda n: _last(pta.adx(h, l, c, length=_period(n)), "DMP"),
        "minus_di": lambda n: _last(pta.adx(h, l, c, length=_period(n)), "DMN"),
        "stochrsi_k": lambda n: _last(pta.stochrsi(c, length=_period(n)), "STOCHRSIk"),
        "stochrsi_d": lambda n: _last(pta.stochrsi(c, length=_period(n)), "STOCHRSId"),
        "kdj_k": lambda n: _last(pta.kdj(h, l, c, length=_period(n)), "K_"),
        "kdj_d": lambda n: _last(pta.kdj(h, l, c, length=_period(n)), "D_"),
        "kdj_j": lambda n: _last(pta.kdj(h, l, c, length=_period(n)), "J_"),
        "aroon_up": lambda n: _last(pta.aroon(h, l, length=_period(n)), "AROONU"),
        "aroon_down": lambda n: _last(pta.aroon(h, l, length=_period(n)), "AROOND"),
        "vortex_plus": lambda n: _last(pta.vortex(h, l, c, length=_period(n)), "VTXP"),
        "vortex_minus": lambda n: _last(pta.vortex(h, l, c, length=_period(n)), "VTXM"),
        "rvi": lambda n: _last(pta.rvi(c, h, l, length=_period(n))),
        "coppock": lambda n: _last(pta.coppock(c, length=_period(n))),

        # --- volatility -----------------------------------------------------
        "natr": lambda n: _last(pta.natr(h, l, c, length=_period(n))),
        "stdev": lambda n: _last(pta.stdev(c, length=_period(n))),
        "bbu": lambda n: _last(pta.bbands(c, length=_period(n)), "BBU"),
        "bbl": lambda n: _last(pta.bbands(c, length=_period(n)), "BBL"),
        "bbp": lambda n: _last(pta.bbands(c, length=_period(n)), "BBP"),
        "kc_upper": lambda n: _last(pta.kc(h, l, c, length=_period(n)), "KCU"),
        "kc_lower": lambda n: _last(pta.kc(h, l, c, length=_period(n)), "KCL"),
        "donchian_upper": lambda n: _last(
            pta.donchian(h, l, lower_length=_period(n), upper_length=_period(n)), "DCU"
        ),
        "donchian_lower": lambda n: _last(
            pta.donchian(h, l, lower_length=_period(n), upper_length=_period(n)), "DCL"
        ),

        # --- trend --------------------------------------------------------
        "supertrend": lambda n: _last(pta.supertrend(h, l, c, length=_period(n)), "SUPERT_"),
        "supertrend_dir": lambda n: _last(pta.supertrend(h, l, c, length=_period(n)), "SUPERTd_"),

        # --- volume ---------------------------------------------------------
        "cmf": lambda n: _last(pta.cmf(h, l, c, v, length=_period(n))),
        "efi": lambda n: _last(pta.efi(c, v, length=_period(n))),
        "eom": lambda n: _last(pta.eom(h, l, c, v, length=_period(n))),
    }

    # --- fixed-parameter indicators: no period of their own, callable with
    # any argument (ignored) so the grammar's "exactly one numeric arg" rule
    # still applies uniformly — e.g. `obv(0)`.
    fns["obv"] = lambda _n: _last(pta.obv(c, v))
    fns["ad"] = lambda _n: _last(pta.ad(h, l, c, v))
    fns["psar"] = lambda _n: _last(pta.psar(h, l, c), "PSAR")

    return fns


#: name(period) -> meaning, for the same author-facing contract as
#: `atr.signals.formula.INDICATOR_FUNCTIONS`.
TA_LIBRARY_NAMES: dict[str, str] = {
    "tema(n)": "triple exponential moving average",
    "dema(n)": "double exponential moving average",
    "hma(n)": "Hull moving average — reduced lag vs. a plain SMA",
    "kama(n)": "Kaufman's adaptive moving average",
    "trima(n)": "triangular moving average",
    "zlma(n)": "zero-lag moving average",
    "vwma(n)": "volume-weighted moving average",
    "midpoint(n)": "midpoint of the last n closes: (highest + lowest) / 2",
    "midprice(n)": "midpoint of the last n bars' high/low range",
    "cci(n)": "Commodity Channel Index",
    "willr(n)": "Williams %R",
    "mfi(n)": "Money Flow Index — volume-weighted RSI",
    "cmo(n)": "Chande Momentum Oscillator",
    "roc(n)": "rate of change, %, over n bars",
    "mom(n)": "momentum — close minus the close n bars ago",
    "trix(n)": "TRIX — rate of change of a triple-smoothed EMA",
    "adx(n)": "Average Directional Index — trend strength, 0-100",
    "plus_di(n)": "+DI — the bullish half of ADX's directional movement",
    "minus_di(n)": "-DI — the bearish half of ADX's directional movement",
    "stochrsi_k(n)": "Stochastic RSI %K",
    "stochrsi_d(n)": "Stochastic RSI %D (signal line)",
    "kdj_k(n)": "KDJ %K — n-period stochastic, EMA-smoothed",
    "kdj_d(n)": "KDJ %D — signal line (EMA of %K)",
    "kdj_j(n)": "KDJ %J — 3*%K - 2*%D; overshoots 0-100, flags momentum extremes earlier than %K/%D",
    "aroon_up(n)": "Aroon Up — bars since the n-bar high, inverted to 0-100",
    "aroon_down(n)": "Aroon Down — bars since the n-bar low, inverted to 0-100",
    "vortex_plus(n)": "Vortex Indicator +VI",
    "vortex_minus(n)": "Vortex Indicator -VI",
    "rvi(n)": "Relative Vigor Index",
    "coppock(n)": "Coppock Curve — long-term momentum",
    "natr(n)": "Normalized ATR — ATR as a % of price",
    "stdev(n)": "standard deviation of the last n closes",
    "bbu(n)": "n-period Bollinger upper band (2 std)",
    "bbl(n)": "n-period Bollinger lower band (2 std)",
    "bbp(n)": "%B — where price sits within the n-period Bollinger bands, 0-1",
    "kc_upper(n)": "n-period Keltner Channel upper band",
    "kc_lower(n)": "n-period Keltner Channel lower band",
    "donchian_upper(n)": "n-period Donchian Channel upper band (highest high)",
    "donchian_lower(n)": "n-period Donchian Channel lower band (lowest low)",
    "supertrend(n)": "Supertrend line value at period n (ATR multiplier fixed at 3)",
    "supertrend_dir(n)": "Supertrend direction: 1 uptrend, -1 downtrend",
    "cmf(n)": "Chaikin Money Flow",
    "efi(n)": "Elder's Force Index",
    "eom(n)": "Ease of Movement",
    "obv(0)": "On-Balance Volume (no period — pass any number, e.g. obv(0))",
    "ad(0)": "Accumulation/Distribution line (no period — pass any number)",
    "psar(0)": "Parabolic SAR (no period — pass any number)",
}
