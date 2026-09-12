"""Morning briefing: pre-market Telegram with the day's list.

Pipeline (all local, no session needed at send time):
  evening  `atr history sync`   refresh the daily cache after close
  morning  `atr brief send`     score the cache, Telegram the picks (8:45 AM)

Levels are trigger-based, not predictions: longs trigger above the previous
day's high with the previous low as the stop. If price never triggers, there
is no trade — that discipline is the whole point.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import json

import pandas as pd
from pydantic import BaseModel

from atr.data.history import load_cached
from atr.scanner import UNIVERSE, score_frame


class BriefingConfig(BaseModel):
    top_n: int = 8
    avoid_n: int = 5
    min_price: float = 50.0
    min_day_value_lakh: float = 50.0
    min_atr_pct: float = 0.5  # kills flat/liquid-fund instruments near their high
    min_bars: int = 60
    universe: str = "all"  # all | watchlist
    watchlist: list[str] = list(UNIVERSE)
    ranking: str = "vs_high"  # vs_high | score — grid-tested, vs_high wins
    send_enabled: bool = True


CONFIG_PATH = Path("data/alerts/briefing.json")
LAST_PATH = Path("data/alerts/briefing_last.json")


def load_config() -> BriefingConfig:
    if CONFIG_PATH.exists():
        try:
            return BriefingConfig(**json.loads(CONFIG_PATH.read_text(encoding="utf8")))
        except Exception:
            pass
    return BriefingConfig()


def save_config(cfg: BriefingConfig) -> BriefingConfig:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=1), encoding="utf8")
    return cfg


def record_sent(message: str, channel: str) -> None:
    LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_PATH.write_text(json.dumps({
        "sent_at": datetime.now().isoformat(timespec="seconds"),
        "channel": channel,
        "chars": len(message),
    }, indent=1), encoding="utf8")


def last_sent() -> dict | None:
    if LAST_PATH.exists():
        try:
            return json.loads(LAST_PATH.read_text(encoding="utf8"))
        except Exception:
            return None
    return None


def _rows(cfg: BriefingConfig) -> pd.DataFrame:
    frames = load_cached("NSEEQ")
    if cfg.universe == "watchlist":
        frames = {s: f for s, f in frames.items() if s in set(cfg.watchlist)}
    scored = []
    for symbol, df in frames.items():
        try:
            scored.append(score_frame(symbol, df))
        except Exception:  # noqa: BLE001
            continue
    scan = pd.DataFrame(scored)
    if scan.empty:
        return scan
    scan = scan[(scan["bars"] >= cfg.min_bars)
                & (scan["last"] >= cfg.min_price)
                & (scan["day_value_lakh"] >= cfg.min_day_value_lakh)
                & (scan["atr_pct"] >= cfg.min_atr_pct)]
    col = "vs_high" if cfg.ranking == "vs_high" else "score"
    return scan.sort_values(col, ascending=False).reset_index(drop=True)


def build_brief(cfg: BriefingConfig | None = None,
                as_of: str | None = None) -> tuple[str, dict]:
    """Returns (telegram message, stats). Pure function of the cache."""
    cfg = cfg or load_config()
    scan = _rows(cfg)
    if scan.empty:
        return ("ATR morning brief: cache empty — run `atr history sync` after close.", {})

    bees = scan[scan["symbol"] == "NIFTYBEES-EQ"]
    regime = ("NIFTYBEES " + bees.iloc[0]["trend"]
              + f", RSI {bees.iloc[0]['rsi']:.0f}") if not bees.empty else "regime n/a"
    up = int((scan["trend"] == "UP").sum())
    breadth = f"{up}/{len(scan)} UP ({up / len(scan) * 100:.0f}%)"

    longs = scan[(scan["rsi"] < 80)
                 & ((scan["trend"] == "UP") | (scan["breakout"]) | (scan["rsi"] < 32))].head(cfg.top_n)
    if len(longs) < max(3, cfg.top_n // 2):
        longs = scan.head(cfg.top_n)
    avoids = scan.tail(cfg.avoid_n).iloc[::-1]

    day = as_of or date.today().strftime("%a %d %b")
    lines = [f"ATR morning brief — {day}", f"Regime: {regime} · breadth {breadth}", ""]
    lines.append(f"WATCH LONGS (trigger prev-high, exit at close):")
    for _, r in longs.iterrows():
        tag = "BO" if r["breakout"] else ("OS" if r["rsi"] < 32 else "MO")
        lines.append(f"{r['symbol'].replace('-EQ', '')} {r['last']} "
                     f"| trig {r['day_high']} | ref {r['day_low']} "
                     f"| RSI {r['rsi']:.0f} | {tag}")
    lines += ["", "AVOID / weak:"]
    for _, r in avoids.iterrows():
        lines.append(f"{r['symbol'].replace('-EQ', '')} {r['last']} "
                     f"| 1M {r['ret_1m']:+.1f}% | RSI {r['rsi']:.0f}")
    lines += ["", "No trigger = no trade. Exit at close — tested better than stops/targets. Not advice."]
    stats = {"scored": len(scan), "breadth_up": up,
             "longs": len(longs), "universe": cfg.universe}
    return "\n".join(lines), stats
