"""Tests for HMAC-signed approval records."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from approve_exact.approval import Approval, sign, verify

SECRETS = st.text(min_size=8, max_size=64)
HASHES = st.from_regex(r"[0-9a-f]{64}", fullmatch=True)
APPROVERS = st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != "")


def _times() -> tuple[datetime, datetime]:
    approved_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    expires_at = approved_at + timedelta(hours=1)
    return approved_at, expires_at


@given(HASHES, APPROVERS, SECRETS)
def test_sign_then_verify(effect_hash: str, approver: str, secret: str) -> None:
    approved_at, expires_at = _times()
    approval = sign(effect_hash, approver, approved_at, expires_at, secret)
    assert verify(approval, secret) is True


@given(HASHES, APPROVERS, SECRETS)
def test_wrong_secret_fails(effect_hash: str, approver: str, secret: str) -> None:
    approved_at, expires_at = _times()
    approval = sign(effect_hash, approver, approved_at, expires_at, secret)
    assert verify(approval, secret + "nope") is False


@given(HASHES, APPROVERS, SECRETS)
def test_tampered_signature_fails(effect_hash: str, approver: str, secret: str) -> None:
    approved_at, expires_at = _times()
    approval = sign(effect_hash, approver, approved_at, expires_at, secret)
    tweaked = "a" if approval.signature[0] != "a" else "b"
    bad = replace(approval, signature=tweaked + approval.signature[1:])
    assert verify(bad, secret) is False


@given(HASHES, APPROVERS, SECRETS)
def test_tampered_field_fails(effect_hash: str, approver: str, secret: str) -> None:
    approved_at, expires_at = _times()
    approval = sign(effect_hash, approver, approved_at, expires_at, secret)
    bad = replace(approval, approver=approver + "x")
    assert verify(bad, secret) is False


def test_is_expired() -> None:
    approved_at, expires_at = _times()
    approval = sign("a" * 64, "human", approved_at, expires_at, "secret")
    assert approval.is_expired(expires_at - timedelta(seconds=1)) is False
    assert approval.is_expired(expires_at) is True
    assert approval.is_expired(expires_at + timedelta(seconds=1)) is True


def test_empty_secret_rejected_on_sign() -> None:
    approved_at, expires_at = _times()
    with pytest.raises(ValueError):
        sign("a" * 64, "human", approved_at, expires_at, "")


def test_expires_before_approved_rejected() -> None:
    approved_at, expires_at = _times()
    with pytest.raises(ValueError):
        sign("a" * 64, "human", expires_at, approved_at, "secret")


def test_verify_empty_secret_false() -> None:
    approved_at, expires_at = _times()
    approval = Approval(
        effect_hash="a" * 64,
        approver="human",
        approved_at=approved_at,
        expires_at=expires_at,
        signature="00",
    )
    assert verify(approval, "") is False
