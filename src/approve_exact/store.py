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
    {
        "proposed",
        "approved",
        "executing",
        "executed",
        "verified",
        "refused",
        "unknown",
        "mismatch",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class EffectRow:
    """One effects table row."""

    id: str
    effect: Effect
    effect_hash: str
    status: str
    provider_id: str | None
    updated_at: datetime


class Store:
    """SQLite store; all status changes go through transition()."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._path,
            timeout=30,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._create_schema()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS effects (
                id TEXT PRIMARY KEY,
                effect_json TEXT NOT NULL,
                effect_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                provider_id TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                effect_id TEXT PRIMARY KEY REFERENCES effects(id),
                effect_hash TEXT NOT NULL,
                approver TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                effect_id TEXT NOT NULL,
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def propose(self, effect: Effect) -> str:
        """Insert a new effect in proposed status; return its id."""
        effect_id = str(uuid.uuid4())
        digest = effect_hash(effect)
        now = _iso(_now())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO effects
                (id, effect_json, effect_hash, status, provider_id, updated_at)
                VALUES (?, ?, ?, 'proposed', NULL, ?)
                """,
                (effect_id, json.dumps(effect_to_dict(effect)), digest, now),
            )
            self._insert_event(effect_id, "proposed", "proposed", "proposed", now)
            self._conn.commit()
        return effect_id

    def get_effect(self, effect_id: str) -> EffectRow | None:
        """Load one effect row, or None."""
        cur = self._conn.execute("SELECT * FROM effects WHERE id = ?", (effect_id,))
        row = cur.fetchone()
        if row is None:
            return None
        data = json.loads(row["effect_json"])
        return EffectRow(
            id=row["id"],
            effect=Effect(**data),
            effect_hash=row["effect_hash"],
            status=row["status"],
            provider_id=row["provider_id"],
            updated_at=_parse_dt(row["updated_at"]),
        )

    def save_approval(self, effect_id: str, approval: Approval) -> None:
        """Upsert the approval row for an effect."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO approvals
                (effect_id, effect_hash, approver, approved_at, expires_at, signature)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(effect_id) DO UPDATE SET
                    effect_hash=excluded.effect_hash,
                    approver=excluded.approver,
                    approved_at=excluded.approved_at,
                    expires_at=excluded.expires_at,
                    signature=excluded.signature
                """,
                (
                    effect_id,
                    approval.effect_hash,
                    approval.approver,
                    _iso(approval.approved_at),
                    _iso(approval.expires_at),
                    approval.signature,
                ),
            )
            self._conn.commit()

    def get_approval(self, effect_id: str) -> Approval | None:
        """Load the approval for an effect, or None."""
        cur = self._conn.execute(
            "SELECT * FROM approvals WHERE effect_id = ?", (effect_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return Approval(
            effect_hash=row["effect_hash"],
            approver=row["approver"],
            approved_at=_parse_dt(row["approved_at"]),
            expires_at=_parse_dt(row["expires_at"]),
            signature=row["signature"],
        )

    def set_provider_id(self, effect_id: str, provider_id: str) -> None:
        """Store the provider object id on the effect row."""
        now = _iso(_now())
        with self._lock:
            self._conn.execute(
                """
                UPDATE effects SET provider_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (provider_id, now, effect_id),
            )
            self._conn.commit()

    def transition(self, effect_id: str, expected: str, new: str, reason: str) -> bool:
        """Move status from expected to new; return True only if one row updated."""
        if new not in STATUSES or expected not in STATUSES:
            raise ValueError("invalid status")
        now = _iso(_now())
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE effects
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (new, now, effect_id, expected),
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                return False
            self._insert_event(effect_id, expected, new, reason, now)
            self._conn.commit()
            return True

    def log_event(
        self, effect_id: str, from_status: str, to_status: str, reason: str
    ) -> None:
        """Append an event without changing status."""
        now = _iso(_now())
        with self._lock:
            self._insert_event(effect_id, from_status, to_status, reason, now)
            self._conn.commit()

    def list_events(self, effect_id: str) -> list[tuple[str, str, str, str]]:
        """Return (ts, from_status, to_status, reason) rows for tests."""
        cur = self._conn.execute(
            """
            SELECT ts, from_status, to_status, reason
            FROM events WHERE effect_id = ? ORDER BY id
            """,
            (effect_id,),
        )
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]

    def _insert_event(
        self,
        effect_id: str,
        from_status: str,
        to_status: str,
        reason: str,
        ts: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO events (ts, effect_id, from_status, to_status, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ts, effect_id, from_status, to_status, reason),
        )
