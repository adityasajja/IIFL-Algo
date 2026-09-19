"""Build the day's read and send it: market scenario, buy ideas, your holdings.

One digest, once a day after the close, because the data is daily and nothing new exists
between closes. The page shows the same thing on demand. Every line is either a description
of the current state or a number measured on the local history, with its limits attached.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel

from atr.insights.evidence import DISCLAIMER
from atr.insights.holdings import load_holdings
from atr.insights.ideas import buy_ideas
from atr.insights.market import changes_since, describe_market, signature
from atr.insights.watch import watch_holding

IST = timezone(timedelta(hours=5, minutes=30))
STALE_AFTER_DAYS = 3


class InsightsSettings(BaseModel):
    enabled: bool = True
    market_changes: bool = True
    buy_ideas: bool = True
    holdings_watch: bool = True
    max_buy_ideas: int = 5
    send_after: str = "16:20"  # IST, once the day's bars exist


class InsightsService:
    def __init__(self, data_root: Path | None = None) -> None:
        from atr.market_intel.service import DATA_ROOT

        self.data_root = Path(data_root) if data_root else DATA_ROOT
        self._dir = self.data_root / "insights"
        self._lock = threading.Lock()
        self._cache: tuple[float, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ settings + state
    def settings(self) -> InsightsSettings:
        path = self._dir / "settings.json"
        if path.exists():
            try:
                return InsightsSettings(**json.loads(path.read_text(encoding="utf8")))
            except Exception as exc:  # noqa: BLE001
                logger.warning("insights settings unreadable, using defaults: {}", exc)
        return InsightsSettings()

    def save_settings(self, settings: InsightsSettings) -> InsightsSettings:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "settings.json").write_text(settings.model_dump_json(indent=2), encoding="utf8")
        self._cache = None
        return settings

    def _state(self) -> dict[str, Any]:
        path = self._dir / "state.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf8"))
            except (OSError, ValueError):
                pass
        return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "state.json").write_text(json.dumps(state), encoding="utf8")

    # ------------------------------------------------------------------ the read
    def build(self, *, user_id: str | None = None, client: Any | None = None, fresh: bool = False) -> dict[str, Any]:
        """The day's read. Cached for two minutes so opening the page is cheap."""
        with self._lock:
            if not fresh and self._cache and time.time() - self._cache[0] < 120:
                return self._cache[1]

        from atr.market_intel.service import get_market_intel_service

        svc = get_market_intel_service()
        summary, _sectors, contexts = svc.compute_all()
        sd = summary.as_dict()
        market = describe_market(sd)
        sig = signature(market)
        state = self._state()
        changes = changes_since(state.get("market"), sig)

        watchlist = self._watchlist_symbols(user_id)
        ideas = buy_ideas(
            contexts,
            nifty_1m_pct=float(sd.get("nifty_1m_return_pct") or 0.0),
            regime=market["regime"],
            watchlist=watchlist,
            limit=self.settings().max_buy_ideas,
        )
        last_ideas = set(state.get("ideas", []))
        for idea in ideas:
            idea["is_new"] = idea["symbol"] not in last_ideas

        held = load_holdings(self.data_root, client)
        items = []
        for h in held["holdings"]:
            ctx = contexts.get(h["symbol"])
            items.append(
                watch_holding(h, svc._load_frame(h["symbol"]), rs_20d=ctx.relative_strength_nifty_20d if ctx else None)
            )
        items.sort(key=lambda r: (not any(f["kind"] == "slipped" for f in r["flags"]), -len(r["flags"])))

        data_as_of = market.get("stocks_as_of")
        stale_days = None
        if data_as_of:
            stale_days = (date.today() - date.fromisoformat(data_as_of)).days

        digest = {
            "as_of": datetime.now(UTC).isoformat(),
            "data_as_of": data_as_of,
            "stale": stale_days is not None and stale_days > STALE_AFTER_DAYS,
            "stale_days": stale_days,
            "market": market,
            "changes": changes,
            "ideas": ideas,
            "holdings": {"source": held["source"], "as_of": held["as_of"], "items": items},
            "disclaimer": DISCLAIMER,
            "last_sent": state.get("last_sent"),
            "_signature": sig,
        }
        with self._lock:
            self._cache = (time.time(), digest)
        return digest

    def _watchlist_symbols(self, user_id: str | None) -> set[str]:
        if not user_id:
            return set()
        try:
            from atr.services.watchlists import WatchlistService

            service = WatchlistService()
            out: set[str] = set()
            for row in service.list_watchlists(user_id):
                detail = service.get(user_id, row["watchlist_id"])
                out.update((detail or {}).get("items", []))
            return out
        except Exception as exc:  # noqa: BLE001 - a watchlist problem must not lose the digest
            logger.debug("watchlist unavailable for insights: {}", exc)
            return set()

    # ------------------------------------------------------------------ the message
    def render(self, digest: dict[str, Any], settings: InsightsSettings | None = None) -> tuple[str, str]:
        settings = settings or self.settings()
        m = digest["market"]
        lines = [f"Market: {m['word']}. {m['headline']}", m["context"]]
        if settings.market_changes and digest["changes"]:
            lines += ["", "What changed:"] + [f"- {c}" for c in digest["changes"]]
        if settings.buy_ideas and digest["ideas"]:
            lines += ["", "Buy ideas (setups that held up in history):"]
            for i in digest["ideas"]:
                tag = " (new)" if i.get("is_new") else ""
                fact = (
                    f"down {abs(i['move_20d_pct']):.0f}% in 20 days"
                    if i["setup"] == "oversold"
                    else f"{i['vs_nifty_20d_pct']:+.0f}% vs Nifty over 20 days"
                )
                lines.append(
                    f"- {i['symbol']}{tag}: {i['setup_label'].lower()}, {fact}. "
                    f"A stop near {i['stop_price']:,.2f} ({i['stop_pct']}% below)."
                )
        if settings.holdings_watch:
            flagged = [h for h in digest["holdings"]["items"] if h["flags"]]
            if flagged:
                lines += ["", "Your holdings:"]
                for h in flagged:
                    lines.append(f"- {h['symbol']}: " + "; ".join(f["title"].lower() + " - " + f["detail"] for f in h["flags"]))
        if digest["data_as_of"]:
            note = f"Stock data as of {date.fromisoformat(digest['data_as_of']):%d %b}"
            note += f" ({digest['stale_days']} days old)." if digest["stale"] else "."
            lines += ["", note, digest["disclaimer"]]
        return "Today's read", "\n".join(lines)

    # ------------------------------------------------------------------ sending
    def send(self, *, user_id: str | None = None, client: Any | None = None, force: bool = False) -> dict[str, Any]:
        from atr.alerts.channels import channels_from_settings
        from atr.config.settings import get_settings

        settings = self.settings()
        if not settings.enabled and not force:
            return {"sent": False, "reason": "disabled"}
        digest = self.build(user_id=user_id, client=client, fresh=True)
        title, body = self.render(digest, settings)
        sent = False
        used = None
        for channel in channels_from_settings(get_settings()):
            try:
                if channel.send(title, body):
                    sent, used = True, type(channel).__name__
                    break
            except Exception as exc:  # noqa: BLE001 - try the next channel
                logger.warning("insights send via {} failed: {}", type(channel).__name__, exc)
        if sent:
            state = self._state()
            state.update(
                {
                    "last_sent": datetime.now(UTC).isoformat(),
                    "last_sent_date": datetime.now(IST).date().isoformat(),
                    "market": digest["_signature"],
                    "ideas": [i["symbol"] for i in digest["ideas"]],
                }
            )
            self._save_state(state)
            self._cache = None
        return {"sent": sent, "channel": used, "body": body}

    def maybe_send_daily(self, client: Any | None = None) -> dict[str, Any] | None:
        """Called on a timer: send once per weekday after the configured time."""
        settings = self.settings()
        now = datetime.now(IST)
        if not settings.enabled or now.weekday() >= 5:
            return None
        hh, mm = (int(x) for x in settings.send_after.split(":"))
        if (now.hour, now.minute) < (hh, mm):
            return None
        if self._state().get("last_sent_date") == now.date().isoformat():
            return None
        return self.send(client=client)


_service: InsightsService | None = None


def get_insights_service() -> InsightsService:
    global _service
    if _service is None:
        _service = InsightsService()
    return _service
