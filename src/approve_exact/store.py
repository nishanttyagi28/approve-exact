"""SQLite persistence for effects, approvals, and status events."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from approve_exact.approval import Approval
from approve_exact.effect import Effect, effect_hash, effect_to_dict

STATUSES = frozenset(
    "proposed approved executing executed verified refused unknown mismatch".split()
)
ALLOWED: dict[str, frozenset[str]] = {
    "proposed": frozenset({"refused"}),
    "approved": frozenset({"refused"}),
    "executing": frozenset({"executed", "unknown", "refused", "mismatch"}),
    "executed": frozenset({"verified", "mismatch"}),
    "unknown": frozenset({"executed", "mismatch", "refused"}),
}
_SCHEMA = """
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


class CorruptEffectError(Exception):
    """Stored effect_json cannot be loaded as an Effect."""

    def __init__(self, effect_id: str, status: str, reason: str) -> None:
        super().__init__(reason)
        self.effect_id, self.status, self.reason = effect_id, status, reason


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return ts.astimezone(UTC).isoformat()


def _parse_dt(value: str) -> datetime:
    p = datetime.fromisoformat(value)
    return p.replace(tzinfo=UTC) if p.tzinfo is None else p.astimezone(UTC)


@dataclass(frozen=True)
class EffectRow:
    id: str
    effect: Effect
    effect_hash: str
    status: str
    provider_id: str | None
    updated_at: datetime


@dataclass(frozen=True)
class StoredApproval:
    approval: Approval
    used_at: datetime | None


class Store:
    """SQLite store with guarded transitions and thread-local connections."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._local = threading.local()
        self._connection().executescript(_SCHEMA)
        self._connection().commit()

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close this thread's database connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def propose(self, effect: Effect) -> str:
        """Insert a new effect in proposed status; return its id."""
        effect_id = str(uuid.uuid4())
        now = _iso(datetime.now(UTC))
        conn = self._connection()
        conn.execute(
            "INSERT INTO effects"
            " (id, effect_json, effect_hash, status, provider_id, updated_at)"
            " VALUES (?, ?, ?, 'proposed', NULL, ?)",
            (effect_id, json.dumps(effect_to_dict(effect)), effect_hash(effect), now),
        )
        self._event(conn, effect_id, "proposed", "proposed", "proposed", now)
        conn.commit()
        return effect_id

    def get_effect(self, effect_id: str) -> EffectRow | None:
        """Load one effect row, or None if missing."""
        row = (
            self._connection()
            .execute("SELECT * FROM effects WHERE id = ?", (effect_id,))
            .fetchone()
        )
        if row is None:
            return None
        try:
            effect = Effect(**json.loads(row["effect_json"]))
            effect_hash(effect)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise CorruptEffectError(effect_id, row["status"], str(exc)) from exc
        return EffectRow(
            row["id"],
            effect,
            row["effect_hash"],
            row["status"],
            row["provider_id"],
            _parse_dt(row["updated_at"]),
        )

    def get_approval(self, effect_id: str) -> StoredApproval | None:
        """Load the approval for an effect, or None."""
        row = (
            self._connection()
            .execute("SELECT * FROM approvals WHERE effect_id = ?", (effect_id,))
            .fetchone()
        )
        if row is None:
            return None
        used = row["used_at"]
        return StoredApproval(
            Approval(
                row["effect_hash"],
                row["approver"],
                _parse_dt(row["approved_at"]),
                _parse_dt(row["expires_at"]),
                row["signature"],
            ),
            None if used is None else _parse_dt(used),
        )

    def approve(self, effect_id: str, approval: Approval) -> bool:
        """Insert approval and transition proposed->approved atomically."""
        now = _iso(datetime.now(UTC))
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if self._set_status(conn, effect_id, "proposed", "approved", now) != 1:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO approvals (effect_id, effect_hash, approver,"
                " approved_at, expires_at, signature, used_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    effect_id,
                    approval.effect_hash,
                    approval.approver,
                    _iso(approval.approved_at),
                    _iso(approval.expires_at),
                    approval.signature,
                ),
            )
            self._event(conn, effect_id, "proposed", "approved", "human approved", now)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

    def claim_for_execute(self, effect_id: str) -> bool:
        """Claim approved->executing and mark approval used, atomically."""
        now = _iso(datetime.now(UTC))
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if self._set_status(conn, effect_id, "approved", "executing", now) != 1:
                conn.rollback()
                return False
            cur = conn.execute(
                "UPDATE approvals SET used_at=? WHERE effect_id=? AND used_at IS NULL",
                (now, effect_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
            self._event(
                conn, effect_id, "approved", "executing", "claimed for execute", now
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

    def transition(
        self,
        effect_id: str,
        expected: str,
        new: str,
        reason: str,
        provider_id: str | None = None,
    ) -> bool:
        """Move status from expected to new; True only if one row updated."""
        if expected not in STATUSES or new not in STATUSES:
            raise ValueError("invalid status")
        if (expected, new) in {("proposed", "approved"), ("approved", "executing")}:
            raise ValueError(
                f"transition {expected!r} -> {new!r} requires approve/claim helper"
            )
        if new not in ALLOWED.get(expected, frozenset()):
            raise ValueError(f"transition {expected!r} -> {new!r} is not allowed")
        now = _iso(datetime.now(UTC))
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if self._set_status(conn, effect_id, expected, new, now, provider_id) != 1:
                conn.rollback()
                return False
            self._event(conn, effect_id, expected, new, reason, now)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

    def log_event(
        self, effect_id: str, from_status: str, to_status: str, reason: str
    ) -> None:
        """Append an event without changing status."""
        conn = self._connection()
        now = _iso(datetime.now(UTC))
        self._event(conn, effect_id, from_status, to_status, reason, now)
        conn.commit()

    def list_events(self, effect_id: str) -> list[tuple[str, str, str, str]]:
        """Return (ts, from_status, to_status, reason) for tests."""
        sql = (
            "SELECT ts, from_status, to_status, reason FROM events"
            " WHERE effect_id=? ORDER BY id"
        )
        rows = self._connection().execute(sql, (effect_id,))
        return [(r[0], r[1], r[2], r[3]) for r in rows.fetchall()]

    @staticmethod
    def _set_status(
        conn: sqlite3.Connection,
        effect_id: str,
        expected: str,
        new: str,
        now: str,
        provider_id: str | None = None,
    ) -> int:
        if provider_id is None:
            sql = "UPDATE effects SET status=?, updated_at=? WHERE id=? AND status=?"
            args: tuple[object, ...] = (new, now, effect_id, expected)
        else:
            sql = (
                "UPDATE effects SET status=?, updated_at=?, provider_id=?"
                " WHERE id=? AND status=?"
            )
            args = (new, now, provider_id, effect_id, expected)
        return conn.execute(sql, args).rowcount

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        effect_id: str,
        from_status: str,
        to_status: str,
        reason: str,
        ts: str,
    ) -> None:
        conn.execute(
            "INSERT INTO events (ts, effect_id, from_status, to_status, reason)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, effect_id, from_status, to_status, reason),
        )
