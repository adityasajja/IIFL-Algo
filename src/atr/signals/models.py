"""Signal models and tunable thresholds.

The defaults here are starting points, not findings. Nothing in this module has
been shown to work; the whole point of ``atr signals validate`` is to test them
before you act on them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

SignalAction = Literal["BUY", "SELL"]

#: Where the user-editable thresholds live.
DEFAULT_CONFIG_PATH = Path("data/signals/config.json")


@dataclass
class ExitRules:
    """Risk rules applied to positions you already hold.

    These are risk management, not alpha: a stop-loss does not need to beat the
    market to be worth obeying.
    """

    #: Exit once unrealised loss reaches this percentage.
    stop_loss_pct: float = 15.0
    #: Exit once unrealised gain reaches this percentage. None = let it run.
    take_profit_pct: float | None = None
    #: Exit once price falls this far below its peak *since you could have
    #: bought it* — approximated from the recent high. None disables.
    trailing_stop_pct: float | None = 25.0
    #: Exit when the daily close drops below this moving average. 0 disables.
    trend_sma: int = 50
    #: Consecutive closes required below the average before the trend rule
    #: fires. A single close below SMA50 happens constantly in ordinary noise —
    #: at 1 bar it fired on a quarter of the book in one session, which is
    #: alert fatigue rather than information.
    trend_confirm_bars: int = 3
    #: Exit when daily RSI is at or above this. None disables.
    rsi_overbought: float | None = 80.0
    #: RSI window the overbought exit reads.
    rsi_period: int = 14
    #: Sell at the close of the week's last session, if nothing else has sold it.
    exit_at_week_end: bool = False
    #: Act on the RSI exit only in the last minutes of the session, when the day's price is
    #: its close. The research sells at the close; a morning price is not that.
    close_only: bool = False
    #: Bars of daily history required before trend/RSI rules are trusted.
    min_history_bars: int = 60

    #: ATR chandelier exit: trails the recent high by a multiple of the
    #: stock's own ATR, instead of a fixed percentage — a ₹3 move means
    #: something different for a ₹50 stock than a ₹3,000 one, and a fixed %
    #: can't tell the difference. The multiple itself tightens once the trade
    #: is already up `atr_tighten_at_pct`, giving a move room to run early and
    #: protecting more of it once there is real profit behind it. For a
    #: strategy with no `take_profit_pct` (e.g. one that otherwise only exits
    #: on a momentum signal normalizing, independent of P&L), this is what
    #: actually locks in a gain rather than relying on ordinary drift.
    atr_chandelier_enabled: bool = False
    atr_period: int = 14
    atr_mult_wide: float = 3.0
    atr_mult_tight: float = 1.5
    atr_tighten_at_pct: float = 5.0

    #: A new exit *condition* as data, not code — see `atr.signals.formula`.
    #: A boolean expression over a fixed indicator set (price, avg_price,
    #: pnl_pct, rsi5/14, sma20/50/200, atr14, peak20/60, volume, vol_avg20,
    #: day_chg_pct); True fires an exit. Compiled and vetted (no calls, no
    #: attribute access, no imports — arithmetic and comparisons only) when
    #: the strategy version is published, not when it trades. None disables.
    custom_exit_formula: str | None = None


@dataclass
class EntryRules:
    """Candidate entry setups.

    Each rule is a hypothesis. ``atr signals validate`` runs them through the
    walk-forward harness; until that passes, treat the output as a watchlist
    rather than a reason to buy.
    """

    #: Rule 1 — uptrend, bought on a pullback.
    trend_fast_sma: int = 20
    trend_slow_sma: int = 50
    pullback_rsi_low: float = 40.0
    pullback_rsi_high: float = 60.0

    #: Rule 2 — breakout to new highs on volume.
    breakout_lookback: int = 63
    breakout_proximity_pct: float = 2.0
    volume_multiple: float = 1.5
    volume_lookback: int = 20

    #: Rule 3 — oversold in a longer-term uptrend.
    oversold_rsi: float = 30.0
    #: Deliberately 100, not the textbook 200. The walk-forward scores a test
    #: window of a few hundred bars, and a strategy needing 200 bars of warmup
    #: simply cannot trade inside one — it produced exactly zero trades.
    long_sma: int = 100

    #: Rule 4 — Triple RSI: a short RSI that has fallen several days running,
    #: below a low level, in a stock still above its long average.
    triple_rsi_period: int = 5
    triple_rsi_below: float = 30.0
    #: The RSI reading three bars back must have been under this.
    triple_rsi_prior_below: float = 60.0
    triple_rsi_trend_sma: int = 200

    #: Rule 5 — gap down: a stock that opens well below the last close.
    #: Fires only when ``setup == "gap_down"``, because it needs the session's
    #: open, which a daily frame does not carry while the day is still forming.
    gap_down_pct: float = 1.0
    #: Only trade when the median stock's prior-week gain exceeds this. None = no filter.
    gap_market_min_pct: float | None = None
    #: 0 = Monday. Only fire on this weekday. None = any day.
    gap_weekday: int | None = None
    #: Buy only within this many minutes of the open. The plan is to buy the open, not the afternoon.
    gap_entry_minutes: float = 15.0

    #: Act on the daily-close rules (Triple RSI) only in the last minutes of the session.
    #: The research buys at the close, so a signal read off the morning's price is a
    #: different signal: a stock that opens down can look oversold and recover by three.
    close_only: bool = False

    #: A new entry *condition* as data, not code — see `ExitRules.custom_exit_formula`
    #: and `atr.signals.formula`. `avg_price`/`pnl_pct` both read 0 here — there
    #: is no position yet. None disables.
    custom_entry_formula: str | None = None

    #: Cross-symbol ranking: a formula (same grammar and indicator set as
    #: `custom_entry_formula`) scoring every symbol that separately qualified
    #: as a candidate this bar — higher score is preferred. With
    #: `max_open_positions` capping the book, this is what turns "some names
    #: pass the entry rule" into "buy the strongest N in the universe":
    #: `rank_formula: "roc20"` ranks candidates by 20-day momentum and the
    #: runner fills the book with the top-scoring ones first. None keeps the
    #: runner's original ordering (a gap-down strategy's own gap size).
    rank_formula: str | None = None

    #: Fire only this rule ("triple_rsi", "breakout", ...). None = any of them.
    #: Without it every strategy also trades the other three rules' setups.
    setup: str | None = None

    #: Bars of history a rule needs before it will fire. Must be comfortably
    #: smaller than the walk-forward test window or the strategy never trades
    #: in validation, and "no trades" looks identical to "no edge".
    min_history_bars: int = 110


@dataclass(frozen=True)
class SessionContext:
    """What a live session knows that a daily frame does not.

    The runner builds one per evaluation. Backtests pass none, and the rules that
    need it then either read what the historical bar holds or decline to fire.
    """

    today: date
    #: The trading day before ``today``. The history's last bar must be this day.
    prior_session: date | None = None
    #: The session's first price and how long after the open it was seen.
    open_price: float | None = None
    minutes_since_open: float | None = None
    #: Median stock's gain over the five sessions before today, in percent.
    market_week_pct: float | None = None
    #: True from shortly before the close on the week's last session.
    week_end_close: bool = False
    #: True in the last minutes of any session: the day's price is close enough to its close.
    near_close: bool = False


@dataclass
class SignalConfig:
    exits: ExitRules = field(default_factory=ExitRules)
    entries: EntryRules = field(default_factory=EntryRules)
    #: Universe for buy scans. Empty = the scanner's default list.
    universe: list[str] = field(default_factory=list)
    #: Exchange for the universe.
    exchange: str = "NSEEQ"

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> SignalConfig:
        path = Path(path)
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            exits=ExitRules(**raw.get("exits", {})),
            entries=EntryRules(**raw.get("entries", {})),
            universe=raw.get("universe", []),
            exchange=raw.get("exchange", "NSEEQ"),
        )

    def save(self, path: Path | str = DEFAULT_CONFIG_PATH) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


#: Candidate values for ``atr signals validate --search``.
#:
#: Deliberately coarse. Fine-grained tuning of a hypothesis that does not work
#: mostly finds noise, and every extra combination raises the deflated-Sharpe
#: bar the winner has to clear — so a bigger grid makes the test stricter, not
#: the result better.
SEARCH_GRID: dict[str, list] = {
    "trend_fast_sma": [10, 20],
    "trend_slow_sma": [50, 100],
    "pullback_rsi_low": [35.0, 45.0],
    "pullback_rsi_high": [55.0, 70.0],
}


@dataclass
class Signal:
    symbol: str
    action: SignalAction
    rule: str
    reason: str
    price: float
    #: Rule-specific metrics that produced the decision, for auditing.
    detail: dict[str, Any] = field(default_factory=dict)
    #: False when the rule has no out-of-sample evidence behind it.
    validated: bool = False
    ts: datetime = field(default_factory=datetime.now)

    def line(self) -> str:
        flag = "" if self.validated else "  (unvalidated)"
        return f"{self.action:4} {self.symbol:16} {self.price:>10,.2f}  {self.rule}{flag}"


@dataclass
class ScanResult:
    buys: list[Signal] = field(default_factory=list)
    sells: list[Signal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.buys and not self.sells
