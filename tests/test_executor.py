"""Tests for propose / approve / execute refusal and claim paths."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from approve_exact.adapters.base import (
    AlreadyExists,
    ProviderError,
    ProviderRecord,
    ProviderTimeout,
)
from approve_exact.approval import Approval, sign
from approve_exact.effect import (
    Effect,
    effect_hash,
    effect_to_dict,
    new_idempotency_key,
)
from approve_exact.executor import Executor
from approve_exact.store import Store

SECRET = "test-secret-not-for-production"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class StubAdapter:
    def __init__(self) -> None:
        self.creates = 0
        self._lock = threading.Lock()
        self._records: dict[str, ProviderRecord] = {}
        self.raise_on_create: Exception | None = None
        self.exist_record: ProviderRecord | None = None

    def find(self, key: str) -> ProviderRecord | None:
        if self.exist_record and self.exist_record.idempotency_key == key:
            return self.exist_record
        return self._records.get(key)

    def create(self, effect: Effect, digest: str) -> ProviderRecord:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        with self._lock:
            self.creates += 1
            rec = ProviderRecord(
                f"stub_{self.creates}",
                effect.idempotency_key,
                effect.amount_paise,
                effect.currency,
                effect.customer_email,
                effect.customer_name,
                effect.description,
                digest,
            )
            self._records[effect.idempotency_key] = rec
            return rec


def _effect(amount: int = 49900, **kw: str) -> Effect:
    return Effect(
        kind="payment_link",
        amount_paise=amount,
        currency="INR",
        customer_email=kw.get("customer_email", "buyer@example.com"),
        customer_name=kw.get("customer_name", "Buyer"),
        description=kw.get("description", "Demo"),
        idempotency_key=kw.get("idempotency_key") or new_idempotency_key(),
    )


def _env(
    tmp_path: Path, name: str = "e", now: datetime = T0
) -> tuple[Path, Store, StubAdapter, Executor]:
    db = tmp_path / f"{name}.db"
    store = Store(db)
    adapter = StubAdapter()
    return db, store, adapter, Executor(store, adapter, SECRET, clock=lambda: now)


def _signed(store: Store, eid: str, at: datetime = T0) -> Approval:
    row = store.get_effect(eid)
    assert row is not None
    return sign(row.effect_hash, "human", at, at + timedelta(hours=1), SECRET)


def _approve(ex: Executor, store: Store, eid: str, at: datetime = T0) -> None:
    assert ex.approve(eid, _signed(store, eid, at)).ok


def _has(store: Store, eid: str, needle: str) -> bool:
    return any(needle in e[3] for e in store.list_events(eid))


def _existing(effect: Effect, *, amount: int | None = None) -> ProviderRecord:
    return ProviderRecord(
        "pre",
        effect.idempotency_key,
        effect.amount_paise if amount is None else amount,
        effect.currency,
        effect.customer_email,
        effect.customer_name,
        effect.description,
        effect_hash(effect),
    )


def test_happy_path(tmp_path: Path) -> None:
    _db, store, adapter, ex = _env(tmp_path)
    eid = ex.propose(_effect()).effect_id
    assert eid
    _approve(ex, store, eid)
    out = ex.execute(eid)
    assert out.ok and out.status == "executed" and adapter.creates == 1
    store.close()


def test_swaps_edits_and_foreign_approval(tmp_path: Path) -> None:
    db, store, adapter, ex = _env(tmp_path)
    effect = _effect()
    eid = ex.propose(effect).effect_id
    assert eid
    _approve(ex, store, eid)
    swapped = replace(effect, customer_email="o@e.com", description="Swapped")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE effects SET effect_json=? WHERE id=?",
            (json.dumps(effect_to_dict(swapped)), eid),
        )
    out = ex.execute(eid)
    assert out.status == "refused" and adapter.creates == 0
    assert _has(store, eid, "execute refused")
    store.close()

    _db2, store2, adapter2, ex2 = _env(tmp_path, "b")
    a, b = ex2.propose(_effect(100)).effect_id, ex2.propose(_effect(200)).effect_id
    assert a and b
    out2 = ex2.approve(a, _signed(store2, b))
    assert out2.status == "proposed" and adapter2.creates == 0
    assert _has(store2, a, "approve refused")
    store2.close()

    db3, store3, adapter3, ex3 = _env(tmp_path, "amt")
    eid3 = ex3.propose(_effect(100)).effect_id
    assert eid3
    _approve(ex3, store3, eid3)
    with sqlite3.connect(db3) as conn:
        data = json.loads(
            conn.execute(
                "SELECT effect_json FROM effects WHERE id=?", (eid3,)
            ).fetchone()[0]
        )
        data["amount_paise"] = 99999
        conn.execute(
            "UPDATE effects SET effect_json=? WHERE id=?", (json.dumps(data), eid3)
        )
    assert ex3.execute(eid3).status == "refused" and adapter3.creates == 0
    store3.close()


def test_expiry_signature_corrupt_no_approval(tmp_path: Path) -> None:
    _db, store, adapter, ex = _env(tmp_path)
    eid = ex.propose(_effect()).effect_id
    assert eid
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
    assert (
        out.status == "proposed"
        and adapter.creates == 0
        and _has(store, eid, "expired")
    )
    bad = replace(_signed(store, eid), signature="ab" * 32)
    assert ex.approve(eid, bad).status == "proposed" and _has(store, eid, "signature")
    assert ex.execute(eid).status == "proposed" and _has(store, eid, "no approval")
    _approve(ex, store, eid)
    assert ex.approve(eid, _signed(store, eid)).status == "approved"
    assert _has(store, eid, "not proposed")
    store.close()

    _db2, store2, adapter2, ex2 = _env(tmp_path, "late")
    eid2 = ex2.propose(_effect()).effect_id
    assert eid2
    _approve(ex2, store2, eid2)
    out2 = Executor(
        store2, adapter2, SECRET, clock=lambda: T0 + timedelta(hours=2)
    ).execute(eid2)
    assert out2.status == "refused" and adapter2.creates == 0
    assert _has(store2, eid2, "expired")
    store2.close()

    db3, store3, adapter3, ex3 = _env(tmp_path, "sig")
    eid3 = ex3.propose(_effect()).effect_id
    assert eid3
    _approve(ex3, store3, eid3)
    with sqlite3.connect(db3) as conn:
        conn.execute(
            "UPDATE approvals SET signature=? WHERE effect_id=?", ("cd" * 32, eid3)
        )
    assert ex3.execute(eid3).status == "refused" and adapter3.creates == 0
    store3.close()

    db4, store4, adapter4, ex4 = _env(tmp_path, "badjson")
    eid4 = ex4.propose(_effect()).effect_id
    assert eid4
    _approve(ex4, store4, eid4)
    with sqlite3.connect(db4) as conn:
        conn.execute("UPDATE effects SET effect_json='{' WHERE id=?", (eid4,))
    assert ex4.execute(eid4).status == "refused" and adapter4.creates == 0
    assert _has(store4, eid4, "corrupt")
    store4.close()


def test_used_timeout_exists_errors(tmp_path: Path) -> None:
    _db, store, adapter, ex = _env(tmp_path)
    eid = ex.propose(_effect()).effect_id
    assert eid
    _approve(ex, store, eid)
    assert ex.execute(eid).ok
    second = ex.execute(eid)
    assert (
        second.ok is False
        and adapter.creates == 1
        and _has(store, eid, "execute refused")
    )
    store.close()

    _db2, store2, adapter2, ex2 = _env(tmp_path, "to")
    eid2 = ex2.propose(_effect()).effect_id
    assert eid2
    _approve(ex2, store2, eid2)
    adapter2.raise_on_create = ProviderTimeout()
    assert ex2.execute(eid2).status == "unknown"
    adapter2.raise_on_create = None
    assert ex2.execute(eid2).ok is False and adapter2.creates == 0
    store2.close()

    effect = _effect()
    _db3, store3, adapter3, ex3 = _env(tmp_path, "ae")
    eid3 = ex3.propose(effect).effect_id
    assert eid3
    _approve(ex3, store3, eid3)
    adapter3.exist_record = _existing(effect)
    adapter3.raise_on_create = AlreadyExists()
    assert ex3.execute(eid3).status == "executed" and adapter3.creates == 0
    store3.close()

    effect4 = _effect()
    _db4, store4, adapter4, ex4 = _env(tmp_path, "ad")
    eid4 = ex4.propose(effect4).effect_id
    assert eid4
    _approve(ex4, store4, eid4)
    adapter4.exist_record = _existing(effect4, amount=effect4.amount_paise + 1)
    adapter4.raise_on_create = AlreadyExists()
    assert ex4.execute(eid4).status == "mismatch" and adapter4.creates == 0
    store4.close()

    _db5, store5, adapter5, ex5 = _env(tmp_path, "pe")
    eid5 = ex5.propose(_effect()).effect_id
    assert eid5
    _approve(ex5, store5, eid5)
    adapter5.raise_on_create = ProviderError("nope")
    assert ex5.execute(eid5).status == "refused" and _has(
        store5, eid5, "provider error"
    )
    store5.close()

    _db6, store6, adapter6, ex6 = _env(tmp_path, "ue")
    eid6 = ex6.propose(_effect()).effect_id
    assert eid6
    _approve(ex6, store6, eid6)
    adapter6.raise_on_create = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        ex6.execute(eid6)
    row = store6.get_effect(eid6)
    assert row and row.status == "unknown" and _has(store6, eid6, "unexpected error")
    store6.close()


def test_concurrent_claim_one_create(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    adapter = StubAdapter()
    for _ in range(50):
        store = Store(db)
        ex = Executor(store, adapter, SECRET, clock=lambda: T0)
        eid = ex.propose(_effect()).effect_id
        assert eid
        _approve(ex, store, eid)
        store.close()
        before = adapter.creates
        barrier = threading.Barrier(4)
        results: list[bool] = []

        def worker(
            effect_id: str = eid,
            gate: threading.Barrier = barrier,
            out: list[bool] = results,
        ) -> None:
            local = Store(db)
            local_ex = Executor(local, adapter, SECRET, clock=lambda: T0)
            gate.wait(timeout=5)
            out.append(local_ex.execute(effect_id).ok)
            local.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert results.count(True) == 1 and adapter.creates == before + 1
