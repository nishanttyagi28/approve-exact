"""Tests for guarded SQLite status transitions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from approve_exact.effect import Effect, new_idempotency_key
from approve_exact.store import Store


def _effect(amount: int = 100) -> Effect:
    return Effect(
        kind="payment_link",
        amount_paise=amount,
        currency="INR",
        customer_email="a@b.com",
        customer_name="A",
        description="t",
        idempotency_key=new_idempotency_key(),
    )


def test_propose_and_transition(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    effect_id = store.propose(_effect())
    assert store.transition(effect_id, "proposed", "approved", "ok") is True
    row = store.get_effect(effect_id)
    assert row is not None and row.status == "approved"
    assert any(
        e[1] == "proposed" and e[2] == "approved" for e in store.list_events(effect_id)
    )
    store.close()


def test_transition_wrong_expected_fails(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    effect_id = store.propose(_effect())
    assert store.transition(effect_id, "approved", "executing", "no") is False
    row = store.get_effect(effect_id)
    assert row is not None and row.status == "proposed"
    store.close()


def test_executed_to_approved_rejected(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    effect_id = store.propose(_effect())
    assert store.transition(effect_id, "proposed", "approved", "a")
    assert store.transition(effect_id, "approved", "executing", "b")
    assert store.transition(effect_id, "executing", "executed", "c")
    with pytest.raises(ValueError, match="not allowed"):
        store.transition(effect_id, "executed", "approved", "bad")
    store.close()


def test_events_are_append_only(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = Store(db)
    effect_id = store.propose(_effect())
    store.close()
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET reason='x' WHERE effect_id=?", (effect_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events WHERE effect_id=?", (effect_id,))
    conn.close()
