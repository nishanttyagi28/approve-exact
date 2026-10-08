"""Verify and reconcile follow-up paths against FakeAdapter."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from test_executor import SECRET, T0, approve, has_event, make_effect, make_env

from approve_exact.adapters.base import ProviderTimeout
from approve_exact.adapters.fake import FakeAdapter
from approve_exact.effect import effect_hash
from approve_exact.executor import Executor
from approve_exact.reconcile import FollowUp
from approve_exact.store import Store


def _env(
    tmp_path: Path, adapter: FakeAdapter, name: str = "r"
) -> tuple[Store, Executor, FollowUp]:
    store = Store(tmp_path / f"{name}.db")
    ex = Executor(store, adapter, SECRET, clock=lambda: T0)
    return store, ex, FollowUp(store, adapter)


def test_happy_path_verify(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    store, ex, follow = _env(tmp_path, adapter)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    approve(ex, store, eid)
    assert ex.execute(eid).status == "executed"
    out = follow.verify(eid)
    row = store.get_effect(eid)
    assert out.ok and out.status == "verified" and adapter.creates == 1
    assert row is not None and row.status == "verified"
    store.close()


def test_timeout_reconcile_one_create(tmp_path: Path) -> None:
    adapter = FakeAdapter(timeout_after_create=True)
    store, ex, follow = _env(tmp_path, adapter)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    approve(ex, store, eid)
    out = ex.execute(eid)
    assert out.status == "unknown" and adapter.creates == 1
    recon = follow.reconcile(eid)
    row = store.get_effect(eid)
    assert recon.ok and recon.status == "verified" and adapter.creates == 1
    assert row is not None and row.status == "verified" and row.provider_id == "fake_1"
    store.close()


def test_drift_verify_mismatch(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    store, ex, follow = _env(tmp_path, adapter)
    effect = make_effect()
    eid = ex.propose(effect).effect_id
    assert eid
    approve(ex, store, eid)
    assert ex.execute(eid).ok
    found = adapter.find(effect.idempotency_key)
    assert found is not None
    adapter._records[effect.idempotency_key] = replace(
        found, amount_paise=found.amount_paise + 1
    )
    out = follow.verify(eid)
    row = store.get_effect(eid)
    assert out.status == "mismatch" and adapter.creates == 1
    assert row is not None and row.status == "mismatch"
    assert has_event(store, eid, "provider record does not match effect")
    store.close()


def test_precheck_skips_second_create(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    store, ex, follow = _env(tmp_path, adapter)
    effect = make_effect()
    digest = effect_hash(effect)
    first = adapter.create(effect, digest)
    assert adapter.creates == 1
    eid = ex.propose(effect).effect_id
    assert eid
    approve(ex, store, eid)
    out = ex.execute(eid)
    row = store.get_effect(eid)
    assert out.ok and out.status == "executed" and adapter.creates == 1
    assert row is not None and row.provider_id == first.provider_id
    assert has_event(store, eid, "precheck find matches effect")
    assert follow.verify(eid).status == "verified"
    store.close()


def test_verify_refuses_wrong_status(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    store, ex, follow = _env(tmp_path, adapter)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    out = follow.verify(eid)
    assert out.ok is False and out.status == "proposed" and adapter.creates == 0
    assert has_event(store, eid, "verify refused: not executed")
    store.close()


def test_reconcile_refuses_wrong_status(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    store, ex, follow = _env(tmp_path, adapter)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    out = follow.reconcile(eid)
    assert out.ok is False and out.status == "proposed" and adapter.creates == 0
    assert has_event(store, eid, "reconcile refused: not unknown")
    store.close()


def test_reconcile_not_found_stays_unknown(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path, name="nf")
    follow = FollowUp(store, adapter)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    approve(ex, store, eid)
    adapter.raise_on_create = ProviderTimeout()
    assert ex.execute(eid).status == "unknown"
    out = follow.reconcile(eid)
    row = store.get_effect(eid)
    assert out.status == "unknown" and adapter.creates == 0
    assert row is not None and row.status == "unknown"
    assert has_event(store, eid, "reconcile: provider record not found")
    store.close()


def test_create_drift_mismatch(tmp_path: Path) -> None:
    adapter = FakeAdapter(drift_amount=True)
    store, ex, _follow = _env(tmp_path, adapter)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    approve(ex, store, eid)
    out = ex.execute(eid)
    row = store.get_effect(eid)
    assert out.status == "mismatch" and adapter.creates == 1
    assert row is not None and row.status == "mismatch"
    store.close()
