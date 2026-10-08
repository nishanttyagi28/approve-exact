"""Happy-path and concurrency tests for the executor."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from approve_exact.adapters.base import ProviderRecord
from approve_exact.approval import Approval, sign
from approve_exact.effect import Effect, effect_hash, new_idempotency_key
from approve_exact.executor import Executor
from approve_exact.store import Store

SECRET = "test-secret-not-for-production"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class StubAdapter:
    """In-memory adapter with injectable create/find failures."""

    def __init__(self) -> None:
        self.creates = 0
        self._lock = threading.Lock()
        self._records: dict[str, ProviderRecord] = {}
        self.raise_on_create: Exception | None = None
        self.raise_on_find: Exception | None = None
        self.exist_record: ProviderRecord | None = None
        self.create_overrides: dict[str, object] = {}

    def find(self, key: str) -> ProviderRecord | None:
        if self.raise_on_find is not None:
            raise self.raise_on_find
        if self.exist_record and self.exist_record.idempotency_key == key:
            return self.exist_record
        return self._records.get(key)

    def create(self, effect: Effect, digest: str) -> ProviderRecord:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        with self._lock:
            self.creates += 1
            fields: dict[str, object] = {
                "provider_id": f"stub_{self.creates}",
                "idempotency_key": effect.idempotency_key,
                "amount_paise": effect.amount_paise,
                "currency": effect.currency,
                "customer_email": effect.customer_email,
                "customer_name": effect.customer_name,
                "description": effect.description,
                "effect_hash": digest,
            }
            fields.update(self.create_overrides)
            rec = ProviderRecord(**fields)  # type: ignore[arg-type]
            self._records[effect.idempotency_key] = rec
            return rec


def make_effect(amount: int = 49900, **kw: str) -> Effect:
    return Effect(
        kind="payment_link",
        amount_paise=amount,
        currency="INR",
        customer_email=kw.get("customer_email", "buyer@example.com"),
        customer_name=kw.get("customer_name", "Buyer"),
        description=kw.get("description", "Demo"),
        idempotency_key=kw.get("idempotency_key") or new_idempotency_key(),
    )


def make_env(
    tmp_path: Path, name: str = "e", now: datetime = T0
) -> tuple[Path, Store, StubAdapter, Executor]:
    db = tmp_path / f"{name}.db"
    store = Store(db)
    adapter = StubAdapter()
    return db, store, adapter, Executor(store, adapter, SECRET, clock=lambda: now)


def signed(store: Store, eid: str, at: datetime = T0) -> Approval:
    row = store.get_effect(eid)
    assert row is not None
    return sign(row.effect_hash, "human", at, at + timedelta(hours=1), SECRET)


def approve(ex: Executor, store: Store, eid: str, at: datetime = T0) -> None:
    assert ex.approve(eid, signed(store, eid, at)).ok


def has_event(store: Store, eid: str, needle: str) -> bool:
    return any(needle in e[3] for e in store.list_events(eid))


def existing(effect: Effect, **overrides: object) -> ProviderRecord:
    fields: dict[str, object] = {
        "provider_id": "pre",
        "idempotency_key": effect.idempotency_key,
        "amount_paise": effect.amount_paise,
        "currency": effect.currency,
        "customer_email": effect.customer_email,
        "customer_name": effect.customer_name,
        "description": effect.description,
        "effect_hash": effect_hash(effect),
    }
    fields.update(overrides)
    return ProviderRecord(**fields)  # type: ignore[arg-type]


def test_happy_path(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    approve(ex, store, eid)
    out = ex.execute(eid)
    row = store.get_effect(eid)
    assert out.ok and out.status == "executed" and adapter.creates == 1
    assert row is not None and row.provider_id == "stub_1"
    store.close()


def test_concurrent_claim_one_create(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    adapter = StubAdapter()
    for _ in range(50):
        store = Store(db)
        ex = Executor(store, adapter, SECRET, clock=lambda: T0)
        eid = ex.propose(make_effect()).effect_id
        assert eid
        approve(ex, store, eid)
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
        assert len(results) == 4
        assert all(not t.is_alive() for t in threads)
        assert results.count(True) == 1 and adapter.creates == before + 1
