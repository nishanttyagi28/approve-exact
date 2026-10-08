"""Provider adapter protocol and shared error types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from approve_exact.effect import Effect


class ProviderError(Exception):
    """Provider returned a non-timeout failure."""


class ProviderTimeout(Exception):
    """Provider call timed out; outcome unknown."""


class AlreadyExists(Exception):
    """Provider already has an object for this idempotency key."""


@dataclass(frozen=True)
class ProviderRecord:
    """Provider-side view of a created payment effect."""

    provider_id: str
    idempotency_key: str
    amount_paise: int
    currency: str
    customer_email: str
    customer_name: str
    description: str
    effect_hash: str


def record_matches(effect: Effect, digest: str, record: ProviderRecord) -> bool:
    """True when the provider record matches the effect field by field."""
    return (
        record.idempotency_key == effect.idempotency_key
        and record.effect_hash == digest
        and record.amount_paise == effect.amount_paise
        and record.currency == effect.currency
        and record.customer_email == effect.customer_email
        and record.customer_name == effect.customer_name
        and record.description == effect.description
    )


class Adapter(Protocol):
    """Creates and looks up provider objects by idempotency key."""

    def find(self, key: str) -> ProviderRecord | None:
        """Return the record for key, or None if missing."""

    def create(self, effect: Effect, effect_hash: str) -> ProviderRecord:
        """Create the provider object for this exact effect."""
