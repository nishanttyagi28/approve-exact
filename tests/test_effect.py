"""Tests for canonical effect JSON and hashing."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy

import pytest
from hypothesis import given
from hypothesis import strategies as st

from approve_exact.effect import (
    HASH_PREFIX,
    Effect,
    canonical_json,
    effect_hash,
    effect_to_dict,
    new_idempotency_key,
)

EMAILS = st.emails()
NAMES = st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != "")
DESCRIPTIONS = st.text(min_size=1, max_size=80).filter(lambda s: s.strip() != "")
KEYS = st.from_regex(r"[A-Za-z0-9_-]{1,40}", fullmatch=True)
AMOUNTS = st.integers(min_value=1, max_value=10_000_000)


@st.composite
def effects(draw: st.DrawFn) -> Effect:
    return Effect(
        kind="payment_link",
        amount_paise=draw(AMOUNTS),
        currency="INR",
        customer_email=draw(EMAILS),
        customer_name=draw(NAMES),
        description=draw(DESCRIPTIONS),
        idempotency_key=draw(KEYS),
    )


def test_new_idempotency_key_charset_and_length() -> None:
    key = new_idempotency_key()
    assert 1 <= len(key) <= 40
    assert all(c.isalnum() or c in "_-" for c in key)


def test_effect_rejects_non_positive_amount() -> None:
    with pytest.raises(ValueError):
        Effect(
            kind="payment_link",
            amount_paise=0,
            currency="INR",
            customer_email="a@b.com",
            customer_name="A",
            description="x",
            idempotency_key="k1",
        )


def test_effect_rejects_bool_amount() -> None:
    with pytest.raises(TypeError):
        Effect(
            kind="payment_link",
            amount_paise=True,  # type: ignore[arg-type]
            currency="INR",
            customer_email="a@b.com",
            customer_name="A",
            description="x",
            idempotency_key="k1",
        )


@given(effects())
def test_key_order_does_not_change_hash(effect: Effect) -> None:
    data = effect_to_dict(effect)
    keys = list(data.keys())
    reversed_data = {k: data[k] for k in reversed(keys)}
    assert canonical_json(data) == canonical_json(reversed_data)
    expected = hashlib.sha256(
        (HASH_PREFIX + canonical_json(reversed_data)).encode("utf-8")
    ).hexdigest()
    assert effect_hash(effect) == expected


@given(
    effects(),
    st.sampled_from(
        [
            "amount_paise",
            "customer_email",
            "customer_name",
            "description",
            "idempotency_key",
        ]
    ),
)
def test_changing_any_field_changes_hash(effect: Effect, field: str) -> None:
    original = effect_hash(effect)
    data = effect_to_dict(effect)
    if field == "amount_paise":
        data[field] = effect.amount_paise + 1
    elif field == "idempotency_key":
        data[field] = (effect.idempotency_key + "x")[:40]
        if data[field] == effect.idempotency_key:
            data[field] = "alt_" + effect.idempotency_key
            data[field] = data[field][:40]
    else:
        data[field] = effect_to_dict(effect)[field] + "_changed"
    changed = Effect(**data)
    assert effect_hash(changed) != original


@given(effects())
def test_canonical_json_round_trips(effect: Effect) -> None:
    data = effect_to_dict(effect)
    text = canonical_json(data)
    loaded = json.loads(text)
    assert loaded == json.loads(canonical_json(loaded))
    assert canonical_json(loaded) == text


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_floats_rejected(value: float) -> None:
    with pytest.raises(TypeError):
        canonical_json({"amount_paise": value})


def test_nan_rejected() -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json({"amount_paise": float("nan")})


def test_bool_as_int_rejected_in_canonical_json() -> None:
    with pytest.raises(TypeError):
        canonical_json({"amount_paise": True})


def test_deepcopy_same_hash() -> None:
    effect = Effect(
        kind="payment_link",
        amount_paise=49900,
        currency="INR",
        customer_email="buyer@example.com",
        customer_name="Buyer",
        description="Test",
        idempotency_key="demo-key-1",
    )
    assert effect_hash(effect) == effect_hash(deepcopy(effect))


def test_math_nan_constant_rejected() -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json({"x": math.nan})
