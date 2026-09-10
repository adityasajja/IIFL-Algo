"""Tiny JSON store for rules + event log. No database required."""

from __future__ import annotations

from pathlib import Path
import json

from atr.alerts.models import AlertEvent, AlertRule

DIR = Path("data/alerts")


def _load(path: Path, cls):
    if not path.exists():
        return []
    try:
        return [cls(**row) for row in json.loads(path.read_text(encoding="utf8"))]
    except Exception:
        return []


class AlertStore:
    def __init__(self, directory: Path | str = DIR) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _rules_path(self) -> Path:
        return self.directory / "rules.json"

    def _events_path(self) -> Path:
        return self.directory / "events.json"

    # -- rules --
    def rules(self) -> list[AlertRule]:
        return _load(self._rules_path(), AlertRule)

    def save_rules(self, rules: list[AlertRule]) -> None:
        self._rules_path().write_text(
            json.dumps([r.model_dump(mode="json") for r in rules], indent=1),
            encoding="utf8",
        )

    def upsert(self, rule: AlertRule) -> AlertRule:
        rules = [r for r in self.rules() if r.id != rule.id] + [rule]
        self.save_rules(rules)
        return rule

    def remove(self, rule_id: str) -> bool:
        rules = self.rules()
        kept = [r for r in rules if r.id != rule_id]
        if len(kept) == len(rules):
            return False
        self.save_rules(kept)
        return True

    # -- events --
    def events(self, limit: int = 100) -> list[AlertEvent]:
        return _load(self._events_path(), AlertEvent)[-limit:][::-1]

    def log(self, event: AlertEvent) -> None:
        history = _load(self._events_path(), AlertEvent) + [event]
        self._events_path().write_text(
            json.dumps([e.model_dump(mode="json") for e in history[-500:]], indent=1),
            encoding="utf8",
        )
