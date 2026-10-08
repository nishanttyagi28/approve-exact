"""Tests for propose / approve / execute refusal and claim paths."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from approve_exact.adapters.base import ProviderRecord
from approve_exact.approval import sign
from approve_exact.effect import (
    Effect,
    effect_hash,
    effect_to_dict,
    new_idempotency_key,
)
from approve_exact.executor import Executor
from approve_exact.store import Store

SECRET = "test-secret-not-for-production"


class StubAdapter:
    """Minimal in-memory adapter for executor tests."""

    def __init__(self) -> None:
        self.creates = 0
        self._lock = threading.Lock()
        self._records: dict[str, ProviderRecord] = {}
        self.create_started = threading.Event()
        self.release_create = threading.Event()
        self.block_create = False

    def find(self, key: str) -> ProviderRecord | None:
        return self._records.get(key)

    def create(self, effect: Effect, effect_hash: str) -> ProviderRecord:
        if self.block_create:
            self.create_started.set()
            self.release_create.wait(timeout=5)
        with self._lock:
            self.creates += 1
            record = ProviderRecord(
                provider_id=f"stub_{self.creates}",
                idempotency_key=effect.idempotency_key,
                amount_paise=effect.amount_paise,
                currency=effect.currency,
                customer_email=effect.customer_email,
                customer_name=effect.customer_name,
                description=effect.description,
                effect_hash=effect_hash,
            )
            self._records[effect.idempotency_key] = record
            return record


def _effect(amount: int = 49900, key: str | None = None) -> Effect:
    return Effect(
        kind="payment_link",
        amount_paise=amount,
        currency="INR",
        customer_email="buyer@example.com",
        customer_name="Buyer",
        description="Demo",
        idempotency_key=key or new_idempotency_key(),
    )


def _setup(tmp_path: Path) -> tuple[Store, StubAdapter, Executor]:
    store = Store(tmp_path / "e.db")
    adapter = StubAdapter()
    executor = Executor(store, adapter, SECRET)
    return store, adapter, executor


def _approve_row(
    executor: Executor,
    store: Store,
    effect_id: str,
    *,
    expires_in: timedelta = timedelta(hours=1),
    now: datetime | None = None,
) -> None:
    row = store.get_effect(effect_id)
    assert row is not None
    approved_at = now or datetime.now(UTC)
    approval = sign(
        row.effect_hash,
        "human",
        approved_at,
        approved_at + expires_in,
        SECRET,
    )
    out = executor.approve(effect_id, approval, approved_at)
    assert out.ok, out.reason


def test_happy_path_execute(tmp_path: Path) -> None:
    store, adapter, executor = _setup(tmp_path)
    effect = _effect()
    proposed = executor.propose(effect)
    assert proposed.ok
    assert proposed.effect_id is not None
    _approve_row(executor, store, proposed.effect_id)
    now = datetime.now(UTC)
    out = executor.execute(proposed.effect_id, now)
    assert out.ok
    assert out.status == "executed"
    assert adapter.creates == 1
    store.close()


def test_parameter_swap_after_approval_refused(tmp_path: Path) -> None:
    store, adapter, executor = _setup(tmp_path)
    effect = _effect(49900)
    effect_id = executor.propose(effect).effect_id
    assert effect_id is not None
    _approve_row(executor, store, effect_id)

    swapped = replace(effect, amount_paise=499900)
    conn = sqlite3.connect(tmp_path / "e.db")
    conn.execute(
        "UPDATE effects SET effect_json = ? WHERE id = ?",
        (json.dumps(effect_to_dict(swapped)), effect_id),
    )
    conn.commit()
    conn.close()

    out = executor.execute(effect_id, datetime.now(UTC))
    assert out.ok is False
    assert "hash" in out.reason
    assert out.status == "refused"
    assert adapter.creates == 0
    store.close()


def test_db_edit_amount_after_approval_refused(tmp_path: Path) -> None:
    store, adapter, executor = _setup(tmp_path)
    effect = _effect(100)
    effect_id = executor.propose(effect).effect_id
    assert effect_id is not None
    _approve_row(executor, store, effect_id)

    conn = sqlite3.connect(tmp_path / "e.db")
    row = conn.execute(
        "SELECT effect_json FROM effects WHERE id = ?", (effect_id,)
    ).fetchone()
    assert row is not None
    data = json.loads(row[0])
    data["amount_paise"] = 99999
    conn.execute(
        "UPDATE effects SET effect_json = ? WHERE id = ?",
        (json.dumps(data), effect_id),
    )
    conn.commit()
    conn.close()

    out = executor.execute(effect_id, datetime.now(UTC))
    assert out.ok is False
    assert out.status == "refused"
    assert adapter.creates == 0
    store.close()


def test_expired_approval_refused(tmp_path: Path) -> None:
    store, adapter, executor = _setup(tmp_path)
    effect_id = executor.propose(_effect()).effect_id
    assert effect_id is not None
    approved_at = datetime(2026, 1, 1, tzinfo=UTC)
    row = store.get_effect(effect_id)
    assert row is not None
    approval = sign(
        row.effect_hash,
        "human",
        approved_at,
        approved_at + timedelta(minutes=5),
        SECRET,
    )
    assert executor.approve(effect_id, approval, approved_at).ok
    out = executor.execute(effect_id, approved_at + timedelta(minutes=10))
    assert out.ok is False
    assert "expired" in out.reason
    assert out.status == "refused"
    assert adapter.creates == 0
    store.close()


def test_second_execute_refused(tmp_path: Path) -> None:
    store, adapter, executor = _setup(tmp_path)
    effect_id = executor.propose(_effect()).effect_id
    assert effect_id is not None
    _approve_row(executor, store, effect_id)
    now = datetime.now(UTC)
    first = executor.execute(effect_id, now)
    assert first.ok
    second = executor.execute(effect_id, now)
    assert second.ok is False
    assert "claim" in second.reason
    assert adapter.creates == 1
    store.close()


def test_two_threads_one_provider_call(tmp_path: Path) -> None:
    store, adapter, executor = _setup(tmp_path)
    adapter.block_create = True
    effect_id = executor.propose(_effect()).effect_id
    assert effect_id is not None
    _approve_row(executor, store, effect_id)
    now = datetime.now(UTC)
    results: list[bool] = []

    def run() -> None:
        results.append(executor.execute(effect_id, now).ok)

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    assert adapter.create_started.wait(timeout=5)
    t2.start()
    # Let the second thread attempt claim while first is inside create.
    threading.Event().wait(0.2)
    adapter.release_create.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert sorted(results) == [False, True]
    assert adapter.creates == 1
    store.close()


def test_approve_wrong_hash_refused(tmp_path: Path) -> None:
    store, _adapter, executor = _setup(tmp_path)
    effect_id = executor.propose(_effect(100)).effect_id
    assert effect_id is not None
    other = _effect(200)
    now = datetime.now(UTC)
    approval = sign(
        effect_hash(other),
        "human",
        now,
        now + timedelta(hours=1),
        SECRET,
    )
    out = executor.approve(effect_id, approval, now)
    assert out.ok is False
    assert "hash" in out.reason
    row = store.get_effect(effect_id)
    assert row is not None
    assert row.status == "proposed"
    store.close()
