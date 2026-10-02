from atr.screener import watch


class FakeService:
    def __init__(self, rows):
        self.rows = rows
        self.state = None

    def run_saved(self, user_id, scan_id, **kw):
        return {"rows": [{"symbol": s, "ltp": 10.0} for s in self.rows], "matched": len(self.rows), "as_of": "2026-10-01"}

    def db(self):  # pragma: no cover
        raise AssertionError


def _scan(seen):
    return {"user_id": "u", "scan_id": "s", "name": "n",
            "definition": {"watch": {"enabled": True, "every_minutes": 5, "seen": seen}}}


def _run(monkeypatch, rows, seen):
    sent, saved = [], {}
    monkeypatch.setattr(watch, "_send", lambda t, b: sent.append((t, b)) or True)
    monkeypatch.setattr(watch, "_save_state", lambda svc, scan, w: saved.update(w))
    out = watch.run_one(FakeService(rows), _scan(seen), now=100.0)
    return out, sent, saved


def test_baseline_does_not_announce_existing(monkeypatch):
    out, sent, saved = _run(monkeypatch, ["A", "B"], None)
    assert out["baseline"] and out["new"] == 0
    assert saved["seen"] == ["A", "B"]


def test_only_new_symbols_announced(monkeypatch):
    out, sent, saved = _run(monkeypatch, ["A", "B", "C"], ["A", "B"])
    assert out["new"] == 1 and "C" in sent[0][1] and "A" not in sent[0][1].split("\n")[0]
    assert saved["seen"] == ["A", "B", "C"]


def test_failed_send_retries_next_run(monkeypatch):
    sent, saved = [], {}
    monkeypatch.setattr(watch, "_send", lambda t, b: False)
    monkeypatch.setattr(watch, "_save_state", lambda svc, scan, w: saved.update(w))
    watch.run_one(FakeService(["A", "C"]), _scan(["A"]), now=1.0)
    assert saved["seen"] == ["A"]
