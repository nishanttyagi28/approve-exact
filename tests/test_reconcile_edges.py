"""Edge-path tests for verify, reconcile, and FakeAdapter."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_executor import approve, existing, has_event, make_effect, make_env
from test_reconcile import _env

from approve_exact.adapters.base import AlreadyExists, ProviderError, ProviderTimeout
from approve_exact.adapters.fake import FakeAdapter
from approve_exact.effect import effect_hash
from approve_exact.executor import Executor
from approve_exact.reconcile import FollowUp
from approve_exact.store import Store


def _executed(
    tmp_path: Path, name: str = "e"
) -> tuple[Store, FakeAdapter, Executor, FollowUp, str]:
    adapter = FakeAdapter()
    store, ex, follow = _env(tmp_path, adapter, name)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    approve(ex, store, eid)
    assert ex.execute(eid).status == "executed"
    return store, adapter, ex, follow, eid


def test_verify_find_raises(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path, name="vf")
    follow = FollowUp(store, adapter)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    approve(ex, store, eid)
    assert ex.execute(eid).status == "executed"
    adapter.raise_on_find = RuntimeError("find boom")
    with pytest.raises(RuntimeError, match="find boom"):
        follow.verify(eid)
    row = store.get_effect(eid)
    assert row is not None and row.status == "executed" and adapter.creates == 1
    assert has_event(store, eid, "verify find failed: RuntimeError")
    store.close()


def test_verify_find_none(tmp_path: Path) -> None:
    store, adapter, _ex, follow, eid = _executed(tmp_path, "vn")
    effect = store.get_effect(eid)
    assert effect is not None
    del adapter._records[effect.effect.idempotency_key]
    out = follow.verify(eid)
    row = store.get_effect(eid)
    assert out.status == "executed" and adapter.creates == 1
    assert row is not None and row.status == "executed"
    assert has_event(store, eid, "verify: provider record missing")
    store.close()


def test_verify_provider_id_differs(tmp_path: Path) -> None:
    store, adapter, _ex, follow, eid = _executed(tmp_path, "vp")
    row = store.get_effect(eid)
    assert row is not None and row.provider_id
    found = adapter.find(row.effect.idempotency_key)
    assert found is not None
    adapter._records[row.effect.idempotency_key] = replace(
        found, provider_id="other-id"
    )
    out = follow.verify(eid)
    after = store.get_effect(eid)
    assert out.status == "mismatch" and adapter.creates == 1
    assert after is not None and after.status == "mismatch"
    assert after.provider_id == row.provider_id
    assert has_event(store, eid, "stored provider_id")
    assert has_event(store, eid, "other-id")
    store.close()


def test_verify_hash_differs(tmp_path: Path) -> None:
    store, adapter, _ex, follow, eid = _executed(tmp_path, "vh")
    row = store.get_effect(eid)
    assert row is not None
    found = adapter.find(row.effect.idempotency_key)
    assert found is not None
    adapter._records[row.effect.idempotency_key] = replace(found, effect_hash="0" * 64)
    out = follow.verify(eid)
    after = store.get_effect(eid)
    assert out.status == "mismatch" and adapter.creates == 1
    assert after is not None and after.status == "mismatch"
    assert has_event(store, eid, "provider record does not match effect")
    store.close()


def test_verify_description_differs(tmp_path: Path) -> None:
    store, adapter, _ex, follow, eid = _executed(tmp_path, "vd")
    row = store.get_effect(eid)
    assert row is not None
    found = adapter.find(row.effect.idempotency_key)
    assert found is not None
    adapter._records[row.effect.idempotency_key] = replace(found, description="changed")
    out = follow.verify(eid)
    after = store.get_effect(eid)
    assert out.status == "mismatch" and adapter.creates == 1
    assert after is not None and after.status == "mismatch"
    assert has_event(store, eid, "provider record does not match effect")
    store.close()


def test_reconcile_find_raises(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path, name="rf")
    follow = FollowUp(store, adapter)
    eid = ex.propose(make_effect()).effect_id
    assert eid
    approve(ex, store, eid)
    adapter.raise_on_create = ProviderTimeout()
    assert ex.execute(eid).status == "unknown"
    adapter.raise_on_find = RuntimeError("find boom")
    with pytest.raises(RuntimeError, match="find boom"):
        follow.reconcile(eid)
    row = store.get_effect(eid)
    assert row is not None and row.status == "unknown" and adapter.creates == 0
    assert has_event(store, eid, "reconcile find failed: RuntimeError")
    store.close()


def test_reconcile_found_differs_mismatch(tmp_path: Path) -> None:
    adapter = FakeAdapter(timeout_after_create=True)
    store, ex, follow = _env(tmp_path, adapter, "rd")
    effect = make_effect()
    eid = ex.propose(effect).effect_id
    assert eid
    approve(ex, store, eid)
    assert ex.execute(eid).status == "unknown" and adapter.creates == 1
    found = adapter.find(effect.idempotency_key)
    assert found is not None
    adapter._records[effect.idempotency_key] = replace(
        found, amount_paise=found.amount_paise + 1
    )
    out = follow.reconcile(eid)
    row = store.get_effect(eid)
    assert out.status == "mismatch" and adapter.creates == 1
    assert row is not None and row.status == "mismatch"
    assert row.provider_id == "fake_1"
    assert has_event(store, eid, "reconcile: provider record found")
    assert has_event(store, eid, "provider record does not match effect")
    store.close()


def test_reconcile_empty_provider_id(tmp_path: Path) -> None:
    _db, store, adapter, ex = make_env(tmp_path, name="re")
    follow = FollowUp(store, adapter)
    effect = make_effect()
    eid = ex.propose(effect).effect_id
    assert eid
    approve(ex, store, eid)
    adapter.raise_on_create = ProviderTimeout()
    assert ex.execute(eid).status == "unknown"
    adapter.raise_on_create = None
    adapter.exist_record = existing(effect, provider_id="")
    out = follow.reconcile(eid)
    row = store.get_effect(eid)
    assert out.status == "mismatch" and adapter.creates == 0
    assert row is not None and row.status == "mismatch"
    assert has_event(store, eid, "reconcile: empty provider_id")
    store.close()


def test_fake_duplicate_key_already_exists() -> None:
    adapter = FakeAdapter()
    effect = make_effect()
    digest = effect_hash(effect)
    adapter.create(effect, digest)
    with pytest.raises(AlreadyExists):
        adapter.create(effect, digest)
    assert adapter.creates == 1


def test_fake_fail_provider_error() -> None:
    adapter = FakeAdapter(fail=True)
    with pytest.raises(ProviderError):
        adapter.create(make_effect(), "0" * 64)
    assert adapter.creates == 0
