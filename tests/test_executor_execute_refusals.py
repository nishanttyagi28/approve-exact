"""One focused test per approve/execute refusal path."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_executor import (
    SECRET,
    T0,
    approve,
    existing,
    has_event,
    make_effect,
    make_env,
)

from approve_exact.adapters.base import AlreadyExists, ProviderError, ProviderTimeout
from approve_exact.effect import effect_to_dict
from approve_exact.executor import Executor


def _proposed(tmp_path: Path, name: str = "e"):
    db, store, adapter, ex = make_env(tmp_path, name)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    return db, store, adapter, ex, eid


def test_execute_not_found(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path)
    out = ex.execute("missing-id")
    assert out.ok is False and adapter.creates == 0
    assert has_event(store, "missing-id", "execute refused: effect not found")
    store.close()


def test_execute_no_approval(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path)
    out = ex.execute(eid)
    assert out.status == "proposed" and adapter.creates == 0
    assert has_event(store, eid, "execute refused: no approval")
    store.close()


def test_execute_used_approval_while_approved(tmp_path: Path) -> None:
    db, store, adapter, ex, eid = _proposed(tmp_path)
    approve(ex, store, eid)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE approvals SET used_at=? WHERE effect_id=?", (T0.isoformat(), eid)
        )
    out = ex.execute(eid)
    assert out.status == "approved" and adapter.creates == 0
    assert has_event(store, eid, "approval already used")
    store.close()


def test_execute_claim_refused_used_at(tmp_path: Path) -> None:
    db, store, adapter, ex, eid = _proposed(tmp_path)
    approve(ex, store, eid)
    assert ex.execute(eid).ok
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE effects SET status='approved' WHERE id=?", (eid,))
    out = ex.execute(eid)
    assert out.ok is False and adapter.creates == 1
    assert has_event(store, eid, "approval already used")
    store.close()


def test_execute_hash_mismatch(tmp_path: Path) -> None:
    db, store, adapter, ex = make_env(tmp_path)
    effect = make_effect()
    eid = ex.propose(effect).effect_id
    assert eid
    approve(ex, store, eid)
    swapped = replace(effect, description="Swapped")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE effects SET effect_json=? WHERE id=?",
            (json.dumps(effect_to_dict(swapped)), eid),
        )
    out = ex.execute(eid)
    assert out.status == "refused" and adapter.creates == 0
    assert has_event(store, eid, "recomputed hash does not match approval")
    store.close()


def test_execute_bad_signature(tmp_path: Path) -> None:
    db, store, adapter, ex, eid = _proposed(tmp_path)
    approve(ex, store, eid)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE approvals SET signature=? WHERE effect_id=?", ("cd" * 32, eid)
        )
    out = ex.execute(eid)
    assert out.status == "refused" and adapter.creates == 0
    assert has_event(store, eid, "invalid approval signature")
    store.close()


def test_execute_expired(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path)
    approve(ex, store, eid)
    out = Executor(
        store, adapter, SECRET, clock=lambda: T0 + timedelta(hours=2)
    ).execute(eid)
    assert out.status == "refused" and adapter.creates == 0
    assert has_event(store, eid, "approval expired")
    store.close()


def test_execute_corrupt_float_field(tmp_path: Path) -> None:
    db, store, adapter, ex = make_env(tmp_path)
    effect = make_effect()
    eid = ex.propose(effect).effect_id
    assert eid
    approve(ex, store, eid)
    data = effect_to_dict(effect)
    data["amount_paise"] = 1.5
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE effects SET effect_json=? WHERE id=?", (json.dumps(data), eid)
        )
    out = ex.execute(eid)
    assert out.status == "refused" and adapter.creates == 0
    assert has_event(store, eid, "corrupt effect")
    store.close()


def test_execute_timeout(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path)
    approve(ex, store, eid)
    adapter.raise_on_create = ProviderTimeout()
    out = ex.execute(eid)
    assert out.status == "unknown" and adapter.creates == 0
    assert has_event(store, eid, "provider timeout")
    store.close()


def test_execute_already_exists_match(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path)
    effect = make_effect()
    eid = ex.propose(effect).effect_id
    assert eid
    approve(ex, store, eid)
    adapter.exist_record = existing(effect, provider_id="pre-1")
    adapter.raise_on_create = AlreadyExists()
    out = ex.execute(eid)
    row = store.get_effect(eid)
    assert out.status == "executed" and out.ok and adapter.creates == 0
    assert row is not None and row.provider_id == "pre-1"
    store.close()


def test_execute_already_exists_differ(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path)
    effect = make_effect()
    eid = ex.propose(effect).effect_id
    assert eid
    approve(ex, store, eid)
    adapter.exist_record = existing(
        effect, provider_id="pre-2", amount_paise=effect.amount_paise + 1
    )
    adapter.raise_on_create = AlreadyExists()
    out = ex.execute(eid)
    row = store.get_effect(eid)
    assert out.status == "mismatch" and adapter.creates == 0
    assert row is not None and row.provider_id == "pre-2"
    assert has_event(store, eid, "precheck find differs from effect")
    store.close()


def test_execute_already_exists_find_none(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path)
    approve(ex, store, eid)
    adapter.raise_on_create = AlreadyExists()
    out = ex.execute(eid)
    assert out.status == "unknown" and adapter.creates == 0
    assert has_event(store, eid, "already exists but find missed")
    store.close()


def test_execute_already_exists_find_raises(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path)
    approve(ex, store, eid)
    adapter.raise_on_create = AlreadyExists()
    adapter.raise_on_find = RuntimeError("find boom")
    with pytest.raises(RuntimeError, match="find boom"):
        ex.execute(eid)
    row = store.get_effect(eid)
    assert row is not None and row.status == "unknown" and adapter.creates == 0
    assert has_event(store, eid, "find precheck failed:")
    store.close()


def test_execute_already_exists_race_match(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path, name="race_m")
    effect = make_effect()
    eid = ex.propose(effect).effect_id
    assert eid
    approve(ex, store, eid)
    adapter.find_queue = [None, existing(effect, provider_id="race-1")]
    adapter.raise_on_create = AlreadyExists()
    out = ex.execute(eid)
    row = store.get_effect(eid)
    assert out.status == "executed" and out.ok and adapter.creates == 0
    assert row is not None and row.provider_id == "race-1"
    assert has_event(store, eid, "already exists matches effect")
    store.close()


def test_execute_already_exists_race_differ(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path, name="race_d")
    effect = make_effect()
    eid = ex.propose(effect).effect_id
    assert eid
    approve(ex, store, eid)
    adapter.find_queue = [
        None,
        existing(effect, provider_id="race-2", amount_paise=effect.amount_paise + 1),
    ]
    adapter.raise_on_create = AlreadyExists()
    out = ex.execute(eid)
    row = store.get_effect(eid)
    assert out.status == "mismatch" and adapter.creates == 0
    assert row is not None and row.provider_id == "race-2"
    assert has_event(store, eid, "already exists differs from effect")
    store.close()


def test_execute_already_exists_race_find_raises(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path, name="race_r")
    approve(ex, store, eid)
    adapter.find_queue = [None, RuntimeError("find boom")]
    adapter.raise_on_create = AlreadyExists()
    with pytest.raises(RuntimeError, match="find boom"):
        ex.execute(eid)
    row = store.get_effect(eid)
    assert row is not None and row.status == "unknown" and adapter.creates == 0
    assert has_event(store, eid, "find failed:")
    store.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount_paise", 1),
        ("currency", "USD"),
        ("customer_email", "x@y.com"),
        ("customer_name", "Other"),
        ("description", "other-desc"),
        ("idempotency_key", "other-key"),
        ("effect_hash", "0" * 64),
        ("provider_id", ""),
    ],
)
def test_execute_create_mismatched_record(
    tmp_path: Path, field: str, value: object
) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path, name=f"m_{field}")
    approve(ex, store, eid)
    adapter.create_overrides = {field: value}
    out = ex.execute(eid)
    row = store.get_effect(eid)
    assert out.status == "mismatch" and adapter.creates == 1 and row is not None
    if field == "provider_id" and value == "":
        assert row.provider_id is None and has_event(store, eid, "empty provider_id")
    else:
        assert row.provider_id == "stub_1"
        assert has_event(store, eid, "provider record does not match effect")
    store.close()


def test_execute_provider_error(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path)
    approve(ex, store, eid)
    adapter.raise_on_create = ProviderError("nope")
    out = ex.execute(eid)
    assert out.status == "refused" and adapter.creates == 0
    assert has_event(store, eid, "provider error")
    store.close()


def test_execute_unexpected_error(tmp_path: Path) -> None:
    _db, store, adapter, ex, eid = _proposed(tmp_path)
    approve(ex, store, eid)
    adapter.raise_on_create = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        ex.execute(eid)
    row = store.get_effect(eid)
    assert row is not None and row.status == "unknown" and adapter.creates == 0
    assert has_event(store, eid, "unexpected error")
    store.close()
