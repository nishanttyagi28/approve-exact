"""HMAC-signed approval records for an effect hash."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime


def _sign_message(
    effect_hash: str,
    approver: str,
    approved_at: datetime,
    expires_at: datetime,
) -> bytes:
    return (
        f"{effect_hash}\n"
        f"{approver}\n"
        f"{approved_at.isoformat()}\n"
        f"{expires_at.isoformat()}"
    ).encode()


@dataclass(frozen=True)
class Approval:
    """Signed human approval of one exact effect hash."""

    effect_hash: str
    approver: str
    approved_at: datetime
    expires_at: datetime
    signature: str

    def is_expired(self, now: datetime) -> bool:
        """True when now is at or past expires_at."""
        return now >= self.expires_at


def sign(
    effect_hash: str,
    approver: str,
    approved_at: datetime,
    expires_at: datetime,
    secret: str,
) -> Approval:
    """Build an Approval with an HMAC-SHA256 signature."""
    if not secret:
        raise ValueError("secret must be non-empty")
    if expires_at <= approved_at:
        raise ValueError("expires_at must be after approved_at")
    digest = hmac.new(
        secret.encode("utf-8"),
        _sign_message(effect_hash, approver, approved_at, expires_at),
        hashlib.sha256,
    ).hexdigest()
    return Approval(
        effect_hash=effect_hash,
        approver=approver,
        approved_at=approved_at,
        expires_at=expires_at,
        signature=digest,
    )


def verify(approval: Approval, secret: str) -> bool:
    """Return True if the signature matches the secret and fields."""
    if not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        _sign_message(
            approval.effect_hash,
            approval.approver,
            approval.approved_at,
            approval.expires_at,
        ),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, approval.signature)
