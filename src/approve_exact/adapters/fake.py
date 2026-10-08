"""In-memory provider adapter for tests and demos."""

from __future__ import annotations

from approve_exact.adapters.base import (
    AlreadyExists,
    ProviderError,
    ProviderRecord,
    ProviderTimeout,
)
from approve_exact.effect import Effect


class FakeAdapter:
    """In-memory adapter that enforces unique idempotency keys."""

    def __init__(
        self,
        *,
        timeout_after_create: bool = False,
        drift_amount: bool = False,
        fail: bool = False,
    ) -> None:
        self.timeout_after_create = timeout_after_create
        self.drift_amount = drift_amount
        self.fail = fail
        self.creates = 0
        self._records: dict[str, ProviderRecord] = {}

    def find(self, key: str) -> ProviderRecord | None:
        """Return the record for key, or None if missing."""
        return self._records.get(key)

    def create(self, effect: Effect, effect_hash: str) -> ProviderRecord:
        """Create a provider object; optional switches alter the outcome."""
        if self.fail:
            raise ProviderError("fake adapter fail")
        if effect.idempotency_key in self._records:
            raise AlreadyExists()
        self.creates += 1
        amount = effect.amount_paise + (1 if self.drift_amount else 0)
        record = ProviderRecord(
            provider_id=f"fake_{self.creates}",
            idempotency_key=effect.idempotency_key,
            amount_paise=amount,
            currency=effect.currency,
            customer_email=effect.customer_email,
            customer_name=effect.customer_name,
            description=effect.description,
            effect_hash=effect_hash,
        )
        self._records[effect.idempotency_key] = record
        if self.timeout_after_create:
            raise ProviderTimeout()
        return record
