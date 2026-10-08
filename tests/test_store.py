"""Tests for guarded SQLite status transitions."""

from __future__ import annotations

from pathlib import Path

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
    row = store.get_effect(effect_id)
    assert row is not None
    assert row.status == "proposed"
    assert store.transition(effect_id, "proposed", "approved", "ok") is True
    row = store.get_effect(effect_id)
    assert row is not None
    assert row.status == "approved"
    events = store.list_events(effect_id)
    assert any(e[1] == "proposed" and e[2] == "approved" for e in events)
    store.close()


def test_transition_wrong_expected_fails(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    effect_id = store.propose(_effect())
    assert store.transition(effect_id, "approved", "executing", "no") is False
    row = store.get_effect(effect_id)
    assert row is not None
    assert row.status == "proposed"
    store.close()


def test_transition_is_at_most_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    effect_id = store.propose(_effect())
    assert store.transition(effect_id, "proposed", "approved", "one") is True
    assert store.transition(effect_id, "proposed", "approved", "two") is False
    store.close()
