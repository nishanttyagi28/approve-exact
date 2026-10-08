"""One focused test per approve/execute refusal path."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from test_executor import (
    SECRET,
    T0,
    approve,
    has_event,
    make_effect,
    make_env,
    signed,
)

from approve_exact.approval import sign


def _proposed(tmp_path: Path, name: str = "e"):
    db, store, adapter, ex = make_env(tmp_path, name)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    return db, store, adapter, ex, eid


def test_approve_corrupt(tmp_path: Path) -> None:
    db, store, adapter, ex, eid = _proposed(tmp_path)
    approval = signed(store, eid)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE effects SET effect_json='{' WHERE id=?", (eid,))
    out = ex.approve(eid, approval)
    assert out.status == "proposed" and adapter.creates == 0
    assert has_event(store, eid, "approve refused: corrupt effect")
    store.close()


def test_approve_not_found(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path)
    out = ex.approve(
        "missing-id", sign("a" * 64, "human", T0, T0 + timedelta(hours=1), SECRET)
    )
    assert out.ok is False and adapter.creates == 0
    assert has_event(store, "missing-id", "approve refused: effect not found")
    store.close()


def test_approve_stored_hash_mismatch(tmp_path: Path) -> None:
    db, store, adapter, ex, eid = _proposed(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE effects SET effect_hash=? WHERE id=?", ("0" * 64, eid))
    out = ex.approve(eid, signed(store, eid))
    assert out.status == "proposed" and adapter.creates == 0
    assert has_event(store, eid, "stored effect does not match stored hash")
    store.close()


def test_approve_foreign_hash(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path)
    a, b = (
        ex.propose(make_effect(100)).effect_id,
        ex.propose(make_effect(200)).effect_id,
    )
    assert a and b
    out = ex.approve(a, signed(store, b))
    assert out.status == "proposed" and adapter.creates == 0
    assert has_event(store, a, "approval hash does not match stored effect")
    store.close()


def test_approve_bad_signature(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path)
    out = ex.approve(eid, replace(signed(store, eid), signature="ab" * 32))
    assert out.status == "proposed" and adapter.creates == 0
    assert has_event(store, eid, "invalid approval signature")
    store.close()


def test_approve_expired(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path)
    row = store.get_effect(eid)
    assert row
    expired = sign(
        row.effect_hash,
        "human",
        T0 - timedelta(hours=2),
        T0 - timedelta(hours=1),
        SECRET,
    )
    out = ex.approve(eid, expired)
    assert out.status == "proposed" and adapter.creates == 0
    assert has_event(store, eid, "approval already expired")
    store.close()


def test_approve_future_approved_at(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path)
    row = store.get_effect(eid)
    assert row
    future = sign(
        row.effect_hash,
        "human",
        T0 + timedelta(hours=1),
        T0 + timedelta(hours=2),
        SECRET,
    )
    out = ex.approve(eid, future)
    assert out.status == "proposed" and adapter.creates == 0
    assert has_event(store, eid, "approved_at is in the future")
    store.close()


def test_approve_not_proposed(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path)
    approve(ex, store, eid)
    out = ex.approve(eid, signed(store, eid))
    assert out.status == "approved" and adapter.creates == 0
    assert has_event(store, eid, "approve refused: not proposed")
    store.close()
