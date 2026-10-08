"""Tests for guarded SQLite status transitions."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from approve_exact.approval import Approval, sign
from approve_exact.effect import Effect, new_idempotency_key
from approve_exact.store import CorruptEffectError, Store

SECRET = "test-secret-not-for-production"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


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


def _approval_for(store: Store, effect_id: str) -> Approval:
    row = store.get_effect(effect_id)
    assert row is not None
    return sign(row.effect_hash, "human", T0, T0 + timedelta(hours=1), SECRET)


def test_propose_and_approve_helper(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    effect_id = store.propose(_effect())
    assert store.approve(effect_id, _approval_for(store, effect_id)) is True
    row = store.get_effect(effect_id)
    assert row is not None and row.status == "approved"
    assert any(
        e[1] == "proposed" and e[2] == "approved" for e in store.list_events(effect_id)
    )
    store.close()


def test_transition_rejects_reserved_edges(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    effect_id = store.propose(_effect())
    with pytest.raises(ValueError, match="approve/claim helper"):
        store.transition(effect_id, "proposed", "approved", "no")
    assert store.approve(effect_id, _approval_for(store, effect_id))
    with pytest.raises(ValueError, match="approve/claim helper"):
        store.transition(effect_id, "approved", "executing", "no")
    store.close()


def test_claim_and_executed_to_approved_rejected(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    effect_id = store.propose(_effect())
    assert store.approve(effect_id, _approval_for(store, effect_id))
    assert store.claim_for_execute(effect_id) is True
    assert store.transition(effect_id, "executing", "executed", "c")
    with pytest.raises(ValueError, match="not allowed"):
        store.transition(effect_id, "executed", "approved", "bad")
    store.close()


def test_claim_refused_when_used_at_set(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    effect_id = store.propose(_effect())
    assert store.approve(effect_id, _approval_for(store, effect_id))
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(
        "UPDATE approvals SET used_at=? WHERE effect_id=?",
        (T0.isoformat(), effect_id),
    )
    conn.commit()
    conn.close()
    assert store.claim_for_execute(effect_id) is False
    row = store.get_effect(effect_id)
    assert row is not None and row.status == "approved"
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


def test_get_effect_float_is_corrupt(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    effect = _effect()
    effect_id = store.propose(effect)
    data = {
        "kind": "payment_link",
        "amount_paise": 1.5,
        "currency": "INR",
        "customer_email": "a@b.com",
        "customer_name": "A",
        "description": "t",
        "idempotency_key": effect.idempotency_key,
    }
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(
        "UPDATE effects SET effect_json=? WHERE id=?",
        (json.dumps(data), effect_id),
    )
    conn.commit()
    conn.close()
    with pytest.raises(CorruptEffectError):
        store.get_effect(effect_id)
    store.close()
