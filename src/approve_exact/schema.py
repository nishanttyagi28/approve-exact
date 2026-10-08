"""SQLite DDL and status graph for the effect store."""

from __future__ import annotations

STATUSES = frozenset(
    "proposed approved executing executed verified refused unknown mismatch".split()
)
ALLOWED: dict[str, frozenset[str]] = {
    "proposed": frozenset({"refused"}),
    "approved": frozenset({"refused"}),
    "executing": frozenset({"executed", "unknown", "refused", "mismatch"}),
    "executed": frozenset({"verified", "mismatch"}),
    "unknown": frozenset({"mismatch", "refused"}),
}
RESERVED = frozenset(
    {("proposed", "approved"), ("approved", "executing"), ("unknown", "executed")}
)
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS effects (
  id TEXT PRIMARY KEY, effect_json TEXT NOT NULL, effect_hash TEXT NOT NULL,
  status TEXT NOT NULL, provider_id TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
  effect_id TEXT PRIMARY KEY REFERENCES effects(id), effect_hash TEXT NOT NULL,
  approver TEXT NOT NULL, approved_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  signature TEXT NOT NULL, used_at TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, effect_id TEXT NOT NULL,
  from_status TEXT NOT NULL, to_status TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""
