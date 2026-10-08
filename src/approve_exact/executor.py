"""Propose, approve, and execute effects under the exact-approval invariant."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from approve_exact.adapters.base import (
    Adapter,
    AlreadyExists,
    ProviderError,
    ProviderRecord,
    ProviderTimeout,
)
from approve_exact.approval import Approval, verify
from approve_exact.effect import Effect, effect_hash
from approve_exact.store import CorruptEffectError, Store


@dataclass(frozen=True)
class Outcome:
    """Result of an executor action."""

    ok: bool
    reason: str
    effect_id: str | None = None
    status: str | None = None


def _default_clock() -> datetime:
    return datetime.now(UTC)


class Executor:
    """Runs propose / approve / execute against a store and adapter."""

    def __init__(
        self,
        store: Store,
        adapter: Adapter,
        secret: str,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        if not secret:
            raise ValueError("secret must be non-empty")
        self._store = store
        self._adapter = adapter
        self._secret = secret
        self._clock = clock

    def propose(self, effect: Effect) -> Outcome:
        """Persist a new effect in proposed status."""
        effect_id = self._store.propose(effect)
        row = self._store.get_effect(effect_id)
        if row is None:
            return Outcome(
                ok=False,
                reason="proposed effect missing after insert",
                effect_id=effect_id,
            )
        return Outcome(
            ok=True, reason="proposed", effect_id=effect_id, status=row.status
        )

    def approve(self, effect_id: str, approval: Approval) -> Outcome:
        """Accept a signed approval for the exact stored effect hash."""
        try:
            row = self._store.get_effect(effect_id)
        except CorruptEffectError as exc:
            self._store.log_event(
                effect_id,
                exc.status,
                exc.status,
                f"approve refused: corrupt effect: {exc.reason}",
            )
            return Outcome(
                ok=False,
                reason=f"corrupt effect: {exc.reason}",
                effect_id=effect_id,
                status=exc.status,
            )
        if row is None:
            return Outcome(ok=False, reason="effect not found", effect_id=effect_id)
        if row.status != "proposed":
            self._store.log_event(
                effect_id, row.status, row.status, "approve refused: not proposed"
            )
            return Outcome(
                ok=False,
                reason="not in proposed status",
                effect_id=effect_id,
                status=row.status,
            )
        recomputed = effect_hash(row.effect)
        if recomputed != row.effect_hash:
            return self._refuse_approve(
                effect_id, "stored effect does not match stored hash"
            )
        if approval.effect_hash != recomputed:
            return self._refuse_approve(
                effect_id, "approval hash does not match stored effect"
            )
        if not verify(approval, self._secret):
            return self._refuse_approve(effect_id, "invalid approval signature")
        if approval.is_expired(self._clock()):
            return self._refuse_approve(effect_id, "approval already expired")
        if not self._store.approve(effect_id, approval):
            return Outcome(
                ok=False, reason="could not claim proposed row", effect_id=effect_id
            )
        return Outcome(
            ok=True, reason="approved", effect_id=effect_id, status="approved"
        )

    def execute(self, effect_id: str) -> Outcome:
        """Claim an approved row once and call the provider with its key."""
        try:
            row = self._store.get_effect(effect_id)
        except CorruptEffectError as exc:
            return self._refuse_corrupt(exc)
        if row is None:
            return Outcome(ok=False, reason="effect not found", effect_id=effect_id)
        stored = self._store.get_approval(effect_id)
        if stored is None:
            return self._log_execute_refuse(
                row.status, effect_id, "no approval on record"
            )
        if stored.used_at is not None:
            return self._log_execute_refuse(
                row.status, effect_id, "approval already used"
            )
        approval = stored.approval
        recomputed = effect_hash(row.effect)
        if recomputed != approval.effect_hash:
            return self._refuse_execute(
                effect_id, row.status, "recomputed hash does not match approval"
            )
        if recomputed != row.effect_hash:
            return self._refuse_execute(
                effect_id, row.status, "recomputed hash does not match stored hash"
            )
        if not verify(approval, self._secret):
            return self._refuse_execute(
                effect_id, row.status, "invalid approval signature"
            )
        if approval.is_expired(self._clock()):
            return self._refuse_execute(effect_id, row.status, "approval expired")
        if not self._store.claim_for_execute(effect_id):
            latest = self._store.get_effect(effect_id)
            status = latest.status if latest else row.status
            return self._log_execute_refuse(
                status, effect_id, "could not claim approved row"
            )
        return self._provider_create(effect_id, row.effect, recomputed)

    def _provider_create(
        self, effect_id: str, effect: Effect, recomputed: str
    ) -> Outcome:
        try:
            record = self._adapter.create(effect, recomputed)
        except ProviderTimeout:
            self._store.transition(
                effect_id, "executing", "unknown", "provider timeout"
            )
            return Outcome(
                ok=False,
                reason="provider timeout",
                effect_id=effect_id,
                status="unknown",
            )
        except AlreadyExists:
            return self._reconcile_existing(effect_id, effect, recomputed)
        except ProviderError as exc:
            self._store.transition(
                effect_id, "executing", "refused", f"provider error: {exc}"
            )
            return Outcome(
                ok=False,
                reason=f"provider error: {exc}",
                effect_id=effect_id,
                status="refused",
            )
        except Exception as exc:
            self._store.transition(
                effect_id, "executing", "unknown", f"unexpected error: {exc}"
            )
            raise
        if not self._record_matches(effect, recomputed, record):
            self._store.transition(
                effect_id,
                "executing",
                "mismatch",
                "provider record does not match effect",
            )
            return Outcome(
                ok=False,
                reason="provider record does not match effect",
                effect_id=effect_id,
                status="mismatch",
            )
        if not self._store.transition(
            effect_id,
            "executing",
            "executed",
            "provider create ok",
            provider_id=record.provider_id,
        ):
            return Outcome(
                ok=False,
                reason="could not mark executed",
                effect_id=effect_id,
                status="executing",
            )
        return Outcome(
            ok=True, reason="executed", effect_id=effect_id, status="executed"
        )

    def _reconcile_existing(
        self, effect_id: str, effect: Effect, recomputed: str
    ) -> Outcome:
        found = self._adapter.find(effect.idempotency_key)
        if found is None:
            self._store.transition(
                effect_id, "executing", "unknown", "already exists but find missed"
            )
            return Outcome(
                ok=False,
                reason="already exists but find missed",
                effect_id=effect_id,
                status="unknown",
            )
        if self._record_matches(effect, recomputed, found):
            self._store.transition(
                effect_id,
                "executing",
                "executed",
                "already exists matches effect",
                provider_id=found.provider_id,
            )
            return Outcome(
                ok=True, reason="executed", effect_id=effect_id, status="executed"
            )
        self._store.transition(
            effect_id, "executing", "mismatch", "already exists differs from effect"
        )
        return Outcome(
            ok=False,
            reason="already exists differs from effect",
            effect_id=effect_id,
            status="mismatch",
        )

    @staticmethod
    def _record_matches(
        effect: Effect, recomputed: str, record: ProviderRecord
    ) -> bool:
        return (
            record.idempotency_key == effect.idempotency_key
            and record.effect_hash == recomputed
            and record.amount_paise == effect.amount_paise
            and record.currency == effect.currency
            and record.customer_email == effect.customer_email
            and record.customer_name == effect.customer_name
            and record.description == effect.description
        )

    def _refuse_approve(self, effect_id: str, reason: str) -> Outcome:
        self._store.log_event(
            effect_id, "proposed", "proposed", f"approve refused: {reason}"
        )
        return Outcome(ok=False, reason=reason, effect_id=effect_id, status="proposed")

    def _refuse_execute(self, effect_id: str, status: str, reason: str) -> Outcome:
        if status == "approved" and self._store.transition(
            effect_id, "approved", "refused", f"execute refused: {reason}"
        ):
            return Outcome(
                ok=False, reason=reason, effect_id=effect_id, status="refused"
            )
        return self._log_execute_refuse(status, effect_id, reason)

    def _refuse_corrupt(self, exc: CorruptEffectError) -> Outcome:
        reason = f"corrupt effect: {exc.reason}"
        if exc.status == "approved" and self._store.transition(
            exc.effect_id, "approved", "refused", f"execute refused: {reason}"
        ):
            return Outcome(
                ok=False, reason=reason, effect_id=exc.effect_id, status="refused"
            )
        return self._log_execute_refuse(exc.status, exc.effect_id, reason)

    def _log_execute_refuse(self, status: str, effect_id: str, reason: str) -> Outcome:
        self._store.log_event(effect_id, status, status, f"execute refused: {reason}")
        return Outcome(ok=False, reason=reason, effect_id=effect_id, status=status)
