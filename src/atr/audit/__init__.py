"""Audit trail.

Two sinks, deliberately:

* ``data/audit/audit.jsonl`` — append-only, restart-proof, readable with ``cat``.
  This predates the database and the execution-mode gate depends on it.
* ``appdb.audit_events`` — the same facts, queryable.

A file survives a database that is not configured; a table answers "show me every
denied request last week". Keeping both is a real cost, accepted because neither
alone covers both cases.
"""

from __future__ import annotations

from .log import AUDIT_PATH, append_file, read_file, record

__all__ = ["AUDIT_PATH", "append_file", "read_file", "record"]
