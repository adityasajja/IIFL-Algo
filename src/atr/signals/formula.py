"""A safe expression language for entry/exit conditions.

The point: adding a genuinely new kind of rule (the ATR chandelier exit) meant
writing Python, which meant a code deploy, which meant a restart — no config
format changes that. This is the alternative: a strategy version can carry a
*formula string* instead, evaluated against a fixed set of precomputed
indicators, so a new condition is data (a version, published like any other)
rather than code.

This is emphatically not "run arbitrary Python": a formula is parsed and
walked against a strict allow-list of AST node types before it is ever
evaluated, and evaluation happens with no builtins, no attribute access, and
no subscripting — there is nothing in the grammar capable of doing anything
but arithmetic, comparisons, and calling one of a fixed set of named
indicator functions (see ``_ALLOWED_FUNCTIONS``) with numeric-literal
arguments only. A formula that doesn't fit that grammar is refused at publish
time (``compile_formula``), not discovered mid-session against a live account.

Function calls are the one exception to "no calls of any kind" this module
used to claim, added deliberately and just as tightly guarded: a formula can
call ``ema(9)``, ``rsi(21)``, ``sma(50)`` and the rest — mirroring the exact
built-ins (``ta.sma``, ``ta.ema``, ``ta.rsi``, ``ta.macd``, ``ta.atr``) the
chart's own Pine Script indicator studio offers — with an *arbitrary* period,
not just the several fixed ones ``build_context`` precomputes. The walk below
still refuses a call whose target isn't literally one of those names, or
whose arguments aren't literal numbers — there is no way to reach a name that
isn't in the whitelist, because nothing in the grammar can construct one.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache

#: Every node type a formula may contain. Deliberately excludes Attribute,
#: Subscript, Lambda, comprehensions, and every statement type — there is no
#: way to reach a builtin or an import from this grammar, because none of the
#: nodes that could reference one are in this set. `Call` is allowed, but see
#: the extra, non-structural check in `compile_formula`: only a whitelisted
#: name, called with only numeric-literal arguments, survives.
_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not, ast.UAdd, ast.USub,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod,
    ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
    ast.IfExp,
    ast.Name, ast.Load,
    ast.Constant,
    ast.Call,
)

#: The only names a `Call` node may target — each bound in `build_context` to
#: a closure over the current frame, taking one numeric period argument.
#: Adding a new one here means adding the matching closure there; nothing a
#: formula author writes can reach any function outside this set.
_ALLOWED_FUNCTIONS = frozenset({
    "ema", "sma", "rsi", "wma", "atr", "highest", "lowest", "stoch_k",
    # The broader library — see `atr.signals.ta_library`. Listed here (rather
    # than derived from that module at import time) so parsing a formula never
    # has to import `pandas_ta` — only evaluating one that actually calls a
    # library function does, inside `build_context`.
    "tema", "dema", "hma", "kama", "trima", "zlma", "vwma", "midpoint", "midprice",
    "cci", "willr", "mfi", "cmo", "roc", "mom", "trix",
    "adx", "plus_di", "minus_di", "stochrsi_k", "stochrsi_d",
    "aroon_up", "aroon_down", "vortex_plus", "vortex_minus", "rvi", "coppock",
    "natr", "stdev", "bbu", "bbl", "bbp", "kc_upper", "kc_lower",
    "donchian_upper", "donchian_lower", "supertrend", "supertrend_dir",
    "cmf", "efi", "eom", "obv", "ad", "psar",
})


class FormulaError(ValueError):
    """A formula that is not safe, or not valid, to evaluate."""


@dataclass(frozen=True)
class CompiledFormula:
    source: str
    _code: object

    def _run(self, context: dict[str, float | bool | None]) -> float | bool:
        try:
            return eval(self._code, {"__builtins__": {}}, dict(context))  # noqa: S307 - AST-vetted, no builtins
        except NameError as exc:
            raise FormulaError(f"unknown name in formula: {exc}") from exc
        except ZeroDivisionError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken indicator call must not crash the evaluation
            # Reachable now that a formula can call into `atr.signals.ta_library`
            # (an external, unvetted-at-this-boundary computation) rather than
            # only ever reading a precomputed scalar — an edge case in one
            # indicator, on one bar of one symbol, is a refused reading, not an
            # exception loose in a live deployment's evaluation pass.
            raise FormulaError(f"indicator call failed: {exc}") from exc

    def eval(self, context: dict[str, float | bool | None]) -> bool:
        """Evaluate as an entry/exit *condition*. Any name the formula uses
        must be present in `context` (a typo'd or unavailable indicator name
        is a refusal, not a silent `None`-flavoured falsy value that would
        look like "condition not met")."""
        try:
            result = self._run(context)
        except FormulaError:
            raise
        except ZeroDivisionError:
            return False
        return bool(result)

    def eval_value(self, context: dict[str, float | bool | None]) -> float:
        """Evaluate as a *score* (a rank formula) rather than a condition —
        the raw number, not truthiness. A comparison (`price > sma20`)
        evaluates to `True`/`False`, which Python treats as `1`/`0` under
        `float()`; a rank formula is meant to be an arithmetic expression
        (`roc20`, `day_chg_pct * volume`), not a condition, but nothing here
        stops it from being one; it would just rank everything as 0 or 1.
        """
        try:
            result = self._run(context)
        except ZeroDivisionError:
            return float("-inf")
        return float(result)


@lru_cache(maxsize=256)
def compile_formula(source: str) -> CompiledFormula:
    """Parse and vet `source`, or raise `FormulaError`. Never evaluates it.

    Called when a strategy version is published, not when it trades — a bad
    formula is refused at authoring time, the same way a malformed rule block
    already refuses a version in `atr.strategy.definition.resolve_rules`. Also
    called on every bar a deployed formula is checked (see `eval_exit` /
    `eval_entry`), so the compiled result is cached by source text — a live
    strategy's formula is fixed for the life of its (immutable) version,
    re-parsing the same string every bar would be pure cost.
    """
    text = (source or "").strip()
    if not text:
        raise FormulaError("empty formula")
    if len(text) > 2000:
        raise FormulaError("formula too long")

    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"not a valid expression: {exc.msg}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise FormulaError(
                f"'{type(node).__name__}' is not allowed in a formula "
                f"(only arithmetic, comparisons, and named indicators are)"
            )
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
            raise FormulaError("only numeric or boolean constants are allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
                bad = node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__
                raise FormulaError(
                    f"'{bad}' is not a callable indicator "
                    f"(only {sorted(_ALLOWED_FUNCTIONS)} may be called)"
                )
            if node.keywords:
                raise FormulaError(f"{node.func.id}(...) does not take keyword arguments")
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant) or isinstance(
                node.args[0].value, bool
            ) or not isinstance(node.args[0].value, (int, float)):
                raise FormulaError(f"{node.func.id}(...) takes exactly one numeric literal argument, e.g. {node.func.id}(14)")

    code = compile(tree, "<formula>", "eval")
    return CompiledFormula(source=text, _code=code)


#: Names available to every entry/exit formula, and what they mean — the
#: contract a strategy author writes against. Extending the indicator set
#: (a new name here, computed in `atr.signals.rules`) is the one thing that
#: still needs code; using any of these in a new *combination* never does.
INDICATOR_NAMES: dict[str, str] = {
    "price": "the latest close",
    "avg_price": "your average price for this position (0 for an entry check)",
    "pnl_pct": "unrealized % gain/loss vs avg_price (0 for an entry check)",
    "rsi5": "5-period RSI",
    "rsi14": "14-period RSI",
    "sma20": "20-day simple moving average",
    "sma50": "50-day simple moving average",
    "sma200": "200-day simple moving average",
    "atr14": "14-period Average True Range",
    "peak20": "highest high over the last 20 bars",
    "peak60": "highest high over the last 60 bars",
    "volume": "the latest bar's volume",
    "vol_avg20": "20-day average volume",
    "day_chg_pct": "close vs the previous bar's close, as a %",
    "macd_line": "MACD line (EMA12 - EMA26)",
    "macd_signal": "MACD signal line (EMA9 of the MACD line)",
    "macd_hist": "MACD histogram (line minus signal) — positive means rising momentum",
    "bb_upper": "20-day Bollinger upper band (SMA20 + 2 std)",
    "bb_lower": "20-day Bollinger lower band (SMA20 - 2 std)",
    "bb_width_pct": "Bollinger band width as a % of the mid — a squeeze reads low",
    "roc20": "20-day rate of change, as a % — the natural score for ranking a universe by momentum",
    "streak_up": "consecutive higher closes ending on the latest bar",
    "streak_down": "consecutive lower closes ending on the latest bar",
    "session_vwap": "today's volume-weighted average price so far (0 before any volume has traded)",
    "vwap_upper_1": "1 standard deviation above session VWAP",
    "vwap_lower_1": "1 standard deviation below session VWAP",
    "vwap_upper_2": "2 standard deviations above session VWAP",
    "vwap_lower_2": "2 standard deviations below session VWAP",
    "minutes_since_open": "minutes since 09:15 IST today (intraday deployments only; 0 in daily mode)",
    "or_high": "today's opening-range high — the first 15 minutes' high (0 until that window has bars)",
    "or_low": "today's opening-range low — the first 15 minutes' low (0 until that window has bars)",
    "open_eq_high_day": "today's open has not been broken to the upside all session — a specific Indian retail day-trading heuristic",
    "open_eq_low_day": "today's open has not been broken to the downside all session",
    "today_open": "today's opening price",
}

#: Callable indicator functions, each taking one numeric-literal period —
#: e.g. `ema(9)`, `rsi(21)`. The named values above are fixed-period
#: shortcuts for the common cases; these are the arbitrary-period versions,
#: matching what the chart's own Pine Script studio offers via `ta.*`.
INDICATOR_FUNCTIONS: dict[str, str] = {
    "ema(n)": "n-period exponential moving average",
    "sma(n)": "n-period simple moving average",
    "wma(n)": "n-period weighted moving average (recent bars weighted more)",
    "rsi(n)": "n-period RSI",
    "atr(n)": "n-period Average True Range",
    "stoch_k(n)": "n-period stochastic %K — where the close sits in the n-bar range, 0-100",
    "highest(n)": "highest high over the last n bars",
    "lowest(n)": "lowest low over the last n bars",
}


def build_context(frame, avg_price: float = 0.0) -> dict[str, float]:
    """The indicator values a formula may reference, computed once per bar.

    `frame` is an OHLCV DataFrame, newest bar last, daily or intraday — the
    same shape every other rule in `atr.signals.rules` already reads (the
    indicator functions are timeframe-agnostic; what changes is only how many
    bars a "day" or a "20-period" window actually spans). Returns `{}`
    (formula then refuses via `FormulaError`, not a silent False) if there
    isn't at least a handful of bars to read a close and a change from.

    Longer-window readings (SMA50/200) degrade to 0.0, not an error, when
    there isn't yet enough history — expected and common early in an intraday
    session, where a handful of 5-minute bars is nowhere near 50 of them.
    """
    import pandas as pd  # local import: this module has no other pandas dependency

    from atr.strategy.indicators import atr as _atr
    from atr.strategy.indicators import bollinger, ema, macd, rsi, sma, stochastic_k, wma

    if frame is None or len(frame) < 3:
        return {}

    close = frame["close"]
    price = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else price
    pnl_pct = (price / avg_price - 1.0) * 100.0 if avg_price > 0 else 0.0

    def _last(series: pd.Series) -> float:
        val = float(series.iloc[-1])
        return val if val == val else 0.0  # NaN -> 0.0, never propagated into the formula

    # Parameterized indicator functions — see `_ALLOWED_FUNCTIONS`. Each takes
    # exactly one numeric period, enforced at parse time in `compile_formula`,
    # and returns the latest bar's reading (0.0 with too little history rather
    # than NaN, same convention as every scalar reading below). This is what
    # lets a formula ask for `ema(9)` or `rsi(21)` — an arbitrary period, not
    # just the several fixed ones (`sma20`, `rsi14`, ...) precomputed as plain
    # names — mirroring the chart's own Pine Script studio, which offers the
    # same handful of indicator families (`ta.sma`, `ta.ema`, `ta.rsi`,
    # `ta.macd`, `ta.atr`) with a period the user picks.
    def _period(n: float) -> int:
        return max(int(n), 1)

    def _fn_ema(n: float) -> float:
        return _last(ema(close, _period(n)))

    def _fn_sma(n: float) -> float:
        return _last(sma(close, _period(n)))

    def _fn_wma(n: float) -> float:
        return _last(wma(close, _period(n)))

    def _fn_rsi(n: float) -> float:
        return _last(rsi(close, _period(n)))

    def _fn_atr(n: float) -> float:
        return _last(_atr(frame["high"], frame["low"], close, _period(n)))

    def _fn_stoch_k(n: float) -> float:
        return _last(stochastic_k(frame["high"], frame["low"], close, _period(n)))

    def _fn_highest(n: float) -> float:
        return float(frame["high"].tail(_period(n)).max())

    def _fn_lowest(n: float) -> float:
        return float(frame["low"].tail(_period(n)).min())

    bb_mid, bb_upper, bb_lower = bollinger(close, 20)
    macd_line, macd_signal, macd_hist = macd(close)

    roc_lookback = 20
    prior = float(close.iloc[-roc_lookback - 1]) if len(close) > roc_lookback else price
    roc20 = ((price / prior - 1.0) * 100.0) if prior else 0.0

    # Consecutive higher/lower closes ending on the latest bar — the same
    # "down 3 days in a row" shape the built-in triple_rsi rule hand-codes,
    # available generically so a formula doesn't have to re-derive it.
    diffs = close.diff().to_numpy()
    streak_up = streak_down = 0
    for d in diffs[::-1]:
        if d > 0:
            if streak_down:
                break
            streak_up += 1
        elif d < 0:
            if streak_up:
                break
            streak_down += 1
        else:
            break

    ctx = {
        "price": price,
        "avg_price": float(avg_price),
        "pnl_pct": pnl_pct,
        "rsi5": _last(rsi(close, 5)),
        "rsi14": _last(rsi(close, 14)),
        "sma20": _last(sma(close, 20)),
        "sma50": _last(sma(close, 50)) if len(frame) >= 50 else 0.0,
        "sma200": _last(sma(close, 200)) if len(frame) >= 200 else 0.0,
        "atr14": _last(_atr(frame["high"], frame["low"], close, 14)),
        "peak20": float(frame["high"].tail(20).max()),
        "peak60": float(frame["high"].tail(min(len(frame), 60)).max()),
        "volume": float(frame["volume"].iloc[-1]) if "volume" in frame.columns else 0.0,
        "vol_avg20": float(frame["volume"].tail(20).mean()) if "volume" in frame.columns else 0.0,
        "day_chg_pct": ((price / prev - 1.0) * 100.0) if prev else 0.0,
        "macd_line": _last(macd_line),
        "macd_signal": _last(macd_signal),
        "macd_hist": _last(macd_hist),
        "bb_upper": _last(bb_upper),
        "bb_lower": _last(bb_lower),
        "bb_width_pct": ((_last(bb_upper) - _last(bb_lower)) / _last(bb_mid) * 100.0) if _last(bb_mid) else 0.0,
        "roc20": roc20,
        "streak_up": float(streak_up),
        "streak_down": float(streak_down),
        "ema": _fn_ema,
        "sma": _fn_sma,
        "wma": _fn_wma,
        "rsi": _fn_rsi,
        "atr": _fn_atr,
        "stoch_k": _fn_stoch_k,
        "highest": _fn_highest,
        "lowest": _fn_lowest,
    }
    ctx.update(_session_context(frame))

    # Candlestick patterns (doji, hammer, engulfing, ...) — see
    # `atr.signals.candlestick_patterns`. Same best-effort posture as the
    # broader indicator library below: a shape-detection bug must not take
    # down a formula that never asked for one.
    try:
        from atr.signals.candlestick_patterns import detect_patterns

        ctx.update(detect_patterns(frame))
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("atr.signals.formula").debug("candlestick patterns unavailable: %s", exc)

    # The broader library (TEMA, ADX, Supertrend, ...) — see
    # `atr.signals.ta_library`. Best-effort: if `pandas_ta` itself has a
    # problem (an incompatible numpy build, for instance), the handful of
    # hand-written indicators above must keep working regardless — a formula
    # using only `ema`/`rsi`/`atr` should never fail because of a library it
    # never called.
    try:
        from atr.signals.ta_library import build_ta_functions

        ctx.update(build_ta_functions(frame))
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("atr.signals.formula").debug("ta_library unavailable: %s", exc)

    # Chart patterns (head-and-shoulders, triangles, ...) and trendlines /
    # Fibonacci — see `atr.signals.chart_patterns` and `atr.signals.trendlines`.
    # Both built on swing points, both original code (no external pattern
    # library carried a license this codebase could actually use — see the
    # module docstrings). Same best-effort isolation as everything above.
    #
    # Both read `recent_swings`, whose fractal definition needs `window` bars
    # *after* a candidate pivot to confirm it (see `swing_points.find_swings`).
    # For a live/paper deployment, `frame`'s last row is `append_live_bar`'s
    # synthetic "today" bar — a single point at the current tick, re-appended
    # on every poll with a different price, and identifiable because it has no
    # `ts` (the real historical rows all do). Feeding that into swing detection
    # makes it the "future" context for whichever of the last few *real* bars
    # are still within `window` of it, so their pivot status flips every time
    # today's price ticks — a completed pattern and its mirror-opposite can
    # both read true minutes apart on data that never actually finished
    # forming. Evaluating these two off `stable_frame` (real, closed bars only)
    # instead makes a pattern flag change at most once per session, matching
    # what a chart pattern is actually supposed to be: a shape that either
    # completed at yesterday's close or hasn't yet.
    stable_frame = frame
    if "ts" in frame.columns and len(frame) > 1 and pd.isna(frame["ts"].iloc[-1]):
        stable_frame = frame.iloc[:-1]

    try:
        from atr.signals.chart_patterns import detect_chart_patterns

        ctx.update(detect_chart_patterns(stable_frame))
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("atr.signals.formula").debug("chart patterns unavailable: %s", exc)

    try:
        from atr.signals.trendlines import fibonacci_context, trendline_context

        ctx.update(trendline_context(stable_frame))
        ctx.update(fibonacci_context(stable_frame))
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("atr.signals.formula").debug("trendlines/fibonacci unavailable: %s", exc)

    # Pivot points (Classic, Fibonacci, Camarilla) — see `atr.signals.pivot_points`.
    try:
        from atr.signals.pivot_points import pivot_point_context

        ctx.update(pivot_point_context(frame))
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("atr.signals.formula").debug("pivot points unavailable: %s", exc)

    # VCP and Narrow Range — see `atr.signals.volatility_patterns`.
    try:
        from atr.signals.volatility_patterns import detect_vcp, narrow_range_context

        ctx.update(detect_vcp(frame))
        ctx.update(narrow_range_context(frame))
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("atr.signals.formula").debug("volatility patterns unavailable: %s", exc)

    # RSI divergence — see `atr.signals.divergence`.
    try:
        from atr.signals.divergence import detect_rsi_divergence

        ctx.update(detect_rsi_divergence(frame))
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("atr.signals.formula").debug("divergence unavailable: %s", exc)

    # Gann Square of 9 and Gann Angles — see `atr.signals.gann_square`.
    try:
        from atr.signals.gann_square import gann_angle_context, gann_context

        ctx.update(gann_context(frame))
        ctx.update(gann_angle_context(frame))
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("atr.signals.formula").debug("Gann indicators unavailable: %s", exc)

    return ctx


def _session_context(frame) -> dict[str, float]:
    """Today's-session-scoped readings: VWAP, minutes since open, opening
    range. Needs a real `ts` column (intraday bars carry one; the daily path's
    synthetic forming-bar row may not) — 0.0 for all four without one, not a
    guess dressed as a reading.
    """
    zero = {
        "session_vwap": 0.0, "minutes_since_open": 0.0, "or_high": 0.0, "or_low": 0.0,
        "vwap_upper_1": 0.0, "vwap_lower_1": 0.0, "vwap_upper_2": 0.0, "vwap_lower_2": 0.0,
        "open_eq_high_day": 0.0, "open_eq_low_day": 0.0, "today_open": 0.0,
    }
    if "ts" not in frame.columns or frame.empty:
        return zero

    import pandas as pd

    ts = pd.to_datetime(frame["ts"], errors="coerce")
    if ts.isna().all():
        return zero
    today = ts.iloc[-1].date()
    today_mask = ts.dt.date == today
    session = frame.loc[today_mask]
    if session.empty:
        return zero

    typical = (session["high"] + session["low"] + session["close"]) / 3.0
    vol = session["volume"] if "volume" in session.columns else pd.Series(0.0, index=session.index)
    cum_vol = float(vol.sum())
    session_vwap = float((typical * vol).sum() / cum_vol) if cum_vol > 0 else 0.0

    # VWAP standard-deviation bands — a routine Indian intraday-desk overlay
    # (most local charting platforms plot 1st/2nd-deviation VWAP bands
    # alongside the line itself), read the same way Bollinger Bands are:
    # a volume-weighted variance of the typical price around the VWAP itself.
    if cum_vol > 0 and len(session) > 1:
        variance = float(((typical - session_vwap) ** 2 * vol).sum() / cum_vol)
        vwap_std = variance ** 0.5
    else:
        vwap_std = 0.0
    vwap_upper_1 = session_vwap + vwap_std
    vwap_lower_1 = session_vwap - vwap_std
    vwap_upper_2 = session_vwap + 2 * vwap_std
    vwap_lower_2 = session_vwap - 2 * vwap_std

    open_time = ts.loc[today_mask].iloc[0]
    minutes_since_open = max(0.0, (ts.iloc[-1] - open_time).total_seconds() / 60.0)

    opening_window = session[(ts.loc[today_mask] - open_time).dt.total_seconds() <= 15 * 60]
    or_high = float(opening_window["high"].max()) if not opening_window.empty else 0.0
    or_low = float(opening_window["low"].min()) if not opening_window.empty else 0.0

    # "Open = High day" / "Open = Low day" — a specific, commonly-cited
    # Indian retail day-trading heuristic: today's open has not been
    # breached all session, in either direction. Read live, not just at the
    # close — true the moment it's still true, false the instant it isn't.
    today_open = float(session["open"].iloc[0])
    today_high = float(session["high"].max())
    today_low = float(session["low"].min())
    open_eq_high_day = 1.0 if today_high <= today_open else 0.0
    open_eq_low_day = 1.0 if today_low >= today_open else 0.0

    return {
        "session_vwap": session_vwap,
        "vwap_upper_1": vwap_upper_1,
        "vwap_lower_1": vwap_lower_1,
        "vwap_upper_2": vwap_upper_2,
        "vwap_lower_2": vwap_lower_2,
        "minutes_since_open": minutes_since_open,
        "or_high": or_high,
        "or_low": or_low,
        "open_eq_high_day": open_eq_high_day,
        "open_eq_low_day": open_eq_low_day,
        "today_open": today_open,
    }
