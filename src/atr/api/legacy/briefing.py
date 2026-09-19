"""The daily briefing message."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from atr.api.deps import get_principal
from atr.config.settings import get_settings

logger = logging.getLogger("atr.api")

router = APIRouter()


class BriefingIn(BaseModel):
    top_n: int = 8
    avoid_n: int = 5
    min_price: float = 50.0
    min_day_value_lakh: float = 50.0
    min_atr_pct: float = 0.5
    min_bars: int = 60
    universe: str = "all"
    watchlist: list[str] = []
    ranking: str = "vs_high"
    send_enabled: bool = True


@router.get("/briefing/config")
def briefing_config() -> dict[str, Any]:
    from atr.briefing import last_sent, load_config

    return {"config": load_config().model_dump(), "last_sent": last_sent()}


@router.put("/briefing/config", dependencies=[Depends(get_principal)])
def briefing_save(body: BriefingIn) -> dict[str, Any]:
    from atr.briefing import BriefingConfig, save_config

    data = body.model_dump()
    if not data["watchlist"]:
        from atr.scanner import UNIVERSE
        data["watchlist"] = list(UNIVERSE)
    return save_config(BriefingConfig(**data)).model_dump()


@router.post("/briefing/preview", dependencies=[Depends(get_principal)])
def briefing_preview() -> dict[str, Any]:
    from atr.briefing import build_brief, load_config

    message, stats = build_brief(load_config())
    return {"message": message, "stats": stats}


@router.post("/briefing/send", dependencies=[Depends(get_principal)])
def briefing_send() -> dict[str, Any]:
    from atr.alerts.channels import channels_from_settings
    from atr.briefing import build_brief, load_config, record_sent

    cfg = load_config()
    message, stats = build_brief(cfg)
    if not cfg.send_enabled:
        return {"sent_on": "disabled", "stats": stats}
    for ch in channels_from_settings(get_settings()):
        if ch.send("ATR morning brief", message):
            record_sent(message, ch.name)
            return {"sent_on": ch.name, "stats": stats}
    return {"sent_on": "none", "stats": stats}
