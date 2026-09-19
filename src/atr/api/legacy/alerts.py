"""Price and intelligent alerts."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from atr.api.deps import get_principal
from atr.config.settings import get_settings

from atr.api.legacy.common import _authed_client

logger = logging.getLogger("atr.api")

router = APIRouter()


class AlertRuleIn(BaseModel):
    name: str = ""
    symbol: str
    exchange: str = "NSEEQ"
    kind: str = "price_below"
    threshold: float = 0.0
    cooldown_min: int = 60
    armed: bool = True


def _alert_store():
    from atr.alerts.store import AlertStore

    return AlertStore()


@router.get("/alerts/rules")
def alert_rules() -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in _alert_store().rules()]


@router.post("/alerts/rules", dependencies=[Depends(get_principal)])
def alert_create(body: AlertRuleIn) -> dict[str, Any]:
    from atr.alerts.models import AlertRule

    return _alert_store().upsert(AlertRule(**body.model_dump())).model_dump(mode="json")


@router.patch("/alerts/rules/{rule_id}", dependencies=[Depends(get_principal)])
def alert_arm(rule_id: str, armed: bool = True) -> dict[str, Any]:
    from atr.alerts.models import AlertRule

    store = _alert_store()
    for rule in store.rules():
        if rule.id == rule_id:
            updated = AlertRule(**{**rule.model_dump(), "armed": armed})
            return store.upsert(updated).model_dump(mode="json")
    raise HTTPException(404, f"no such rule: {rule_id}")


@router.delete("/alerts/rules/{rule_id}", dependencies=[Depends(get_principal)])
def alert_delete(rule_id: str) -> dict[str, bool]:
    if not _alert_store().remove(rule_id):
        raise HTTPException(404, f"no such rule: {rule_id}")
    return {"deleted": True}


@router.get("/alerts/events")
def alert_events(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in _alert_store().events(limit)]


@router.post("/alerts/check", dependencies=[Depends(get_principal)])
def alert_check() -> dict[str, Any]:
    """Evaluate all armed rules right now. Returns what fired."""
    from atr.alerts.channels import channels_from_settings
    from atr.alerts.engine import check, market_open_now

    settings = get_settings()
    events = check(_alert_store(), _authed_client(), channels_from_settings(settings))
    return {
        "market_open": market_open_now(),
        "fired": [e.model_dump(mode="json") for e in events],
    }


@router.post("/alerts/test", dependencies=[Depends(get_principal)])
def alert_test() -> dict[str, Any]:
    """Send 'ATR test' down the channel chain. Verifies Telegram/SMS setup."""
    from atr.alerts.channels import channels_from_settings

    settings = get_settings()
    sent_on = "none"
    for ch in channels_from_settings(settings):
        if ch.send("ATR test", "alerts are wired — you will get firing rules here."):
            sent_on = ch.name
            break
    return {"sent_on": sent_on}


# ----------------------------------------------------------------------
# Intelligent Automated Buy & Sell Alerts
# ----------------------------------------------------------------------
@router.get("/alerts/intelligent/config")
def get_intelligent_alert_config() -> dict[str, Any]:
    from atr.alerts.intelligent import get_intelligent_monitor, load_intelligent_config
    cfg = load_intelligent_config()
    status = get_intelligent_monitor().get_status()
    return {
        "config": cfg.model_dump(mode="json"),
        "status": status,
    }


@router.post("/alerts/intelligent/config", dependencies=[Depends(get_principal)])
def update_intelligent_alert_config(body: dict[str, Any]) -> dict[str, Any]:
    from atr.alerts.intelligent import (
        IntelligentAlertConfig,
        get_intelligent_monitor,
        load_intelligent_config,
        save_intelligent_config,
    )
    current = load_intelligent_config().model_dump()
    current.update(body)
    new_cfg = save_intelligent_config(IntelligentAlertConfig(**current))
    return {
        "config": new_cfg.model_dump(mode="json"),
        "status": get_intelligent_monitor().get_status(),
    }


@router.post("/alerts/intelligent/evaluate", dependencies=[Depends(get_principal)])
async def evaluate_intelligent_alerts_now() -> dict[str, Any]:
    """Force an immediate evaluation cycle across target stocks right now."""
    from atr.alerts.intelligent import get_intelligent_monitor
    monitor = get_intelligent_monitor()
    signals = await monitor.run_evaluation_cycle(force=True)
    return {
        "signals": [s.model_dump(mode="json") for s in signals],
        "count": len(signals),
        "as_of": datetime.now().isoformat(),
        "status": monitor.get_status(),
    }
