"""Execution-mode gate and audit-trail tests.

The gate is the last thing standing between a validated-in-the-lab strategy and
a real order at a real broker, so it gets tested directly rather than assumed.
The property under test is asymmetric on purpose:

    default deny  ->  nothing is transmitted unless someone said so in writing
    one-way       ->  going live costs a reason; going back to paper is free
    durable       ->  the record survives the process that made it
"""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """Fresh API module with its audit file redirected into tmp_path."""
    mod = importlib.import_module("atr.api.main")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    mod._STATE.clear()
    mod._STATE.update({"kill_switch": False, "execution_mode": "paper"})
    return mod


# ─── the default ──────────────────────────────────────────────────────────────

def test_defaults_to_paper(api):
    """A cold start must not be able to spend money."""
    mode = api.get_execution_mode()
    assert mode["mode"] == "paper"
    assert mode["paper"] is True
    assert mode["live"] is False


def test_gate_blocks_orders_in_paper_mode(api):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        api._require_live_execution("Test order")
    assert exc.value.status_code == 403
    # The message has to name the way out, or the operator is stuck.
    assert "paper" in exc.value.detail.lower()
    assert "execution mode" in exc.value.detail.lower()


def test_paper_and_live_are_mutually_exclusive(api):
    mode = api.get_execution_mode()
    assert mode["live"] != mode["paper"]


# ─── transitions ──────────────────────────────────────────────────────────────

def test_going_live_requires_a_reason(api):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        api.set_execution_mode("live", reason="")
    assert exc.value.status_code == 400
    # And the refusal must not have flipped the mode as a side effect.
    assert api.get_execution_mode()["mode"] == "paper"

    with pytest.raises(HTTPException):
        api.set_execution_mode("live", reason="   ")  # whitespace is not a reason

    assert api.get_execution_mode()["mode"] == "paper"


def test_going_live_with_a_reason_succeeds_and_opens_the_gate(api):
    api.set_execution_mode("live", reason="3 weeks of paper fills matched the forecast")

    mode = api.get_execution_mode()
    assert mode["mode"] == "live"
    assert mode["reason"] == "3 weeks of paper fills matched the forecast"
    assert mode["changed_at"]

    # The gate must actually open — not just report that it did.
    api._require_live_execution("Test order")


def test_returning_to_paper_needs_no_reason(api):
    api.set_execution_mode("live", reason="testing")
    api.set_execution_mode("paper", reason="")
    assert api.get_execution_mode()["mode"] == "paper"

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        api._require_live_execution("Test order")


def test_unknown_mode_is_rejected(api):
    from fastapi import HTTPException

    for bad in ("", "LIVE", "test", "real"):
        with pytest.raises(HTTPException):
            api.set_execution_mode(bad, reason="x")
    assert api.get_execution_mode()["mode"] == "paper"


# ─── the audit trail ──────────────────────────────────────────────────────────

def test_mode_changes_are_recorded(api):
    api.set_execution_mode("live", reason="first live session", actor="aditya")
    api.set_execution_mode("paper", reason="done for the day", actor="aditya")

    entries = api.get_audit()["entries"]
    assert len(entries) == 2
    # Newest first.
    assert entries[0]["subject"] == "live -> paper"
    assert entries[1]["subject"] == "paper -> live"
    assert entries[1]["detail"] == "first live session"
    assert entries[1]["actor"] == "aditya"


def test_kill_switch_is_recorded(api):
    api.kill_switch(True)
    api.kill_switch(False)

    entries = api.get_audit()["entries"]
    actions = [e["action"] for e in entries]
    assert "kill_switch.release" in actions
    assert "kill_switch.engage" in actions


def test_trail_is_append_only(api):
    api.set_execution_mode("live", reason="a")
    first = api.get_audit()["entries"]
    api.set_execution_mode("paper", reason="b")
    second = api.get_audit()["entries"]

    # The earlier record is still there, byte-identical.
    assert second[-1] == first[0]
    assert len(second) > len(first)


def test_trail_survives_a_restart(api, tmp_path, monkeypatch):
    """A record that vanishes on restart is not an audit trail."""
    api.set_execution_mode("live", reason="persisted")

    from atr.api import main as mod

    monkeypatch.setattr(mod, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    # A fresh module object, same file on disk.
    fresh = importlib.reload(importlib.import_module("atr.api.main"))
    monkeypatch.setattr(fresh, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    entries = fresh.get_audit()["entries"]
    assert any(e.get("detail") == "persisted" for e in entries)


def test_trail_is_jsonl(api, tmp_path):
    api.set_execution_mode("live", reason="check the format")
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines
    for line in lines:
        rec = json.loads(line)  # one record per line, parseable
        assert {"ts", "actor", "action", "subject", "detail"} <= set(rec)


def test_missing_audit_file_reads_as_empty(api, tmp_path):
    assert api.get_audit()["entries"] == []
    assert not (tmp_path / "audit.jsonl").exists()
