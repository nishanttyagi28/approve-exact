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
    record_matches,
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


def _outcome(
    ok: bool, reason: str, effect_id: str | None = None, status: str | None = None
) -> Outcome:
    return Outcome(ok=ok, reason=reason, effect_id=effect_id, status=status)


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
            return _outcome(False, "proposed effect missing after insert", effect_id)
        return _outcome(True, "proposed", effect_id, row.status)

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
            return _outcome(
                False, f"corrupt effect: {exc.reason}", effect_id, exc.status
            )
        if row is None:
            self._store.log_event(
                effect_id, "missing", "missing", "approve refused: effect not found"
            )
            return _outcome(False, "effect not found", effect_id)
        if row.status != "proposed":
            self._store.log_event(
                effect_id, row.status, row.status, "approve refused: not proposed"
            )
            return _outcome(False, "not in proposed status", effect_id, row.status)
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
        now = self._clock()
        if approval.approved_at > now:
            return self._refuse_approve(effect_id, "approved_at is in the future")
        if approval.is_expired(now):
            return self._refuse_approve(effect_id, "approval already expired")
        if not self._store.approve(effect_id, approval):
            return _outcome(False, "could not claim proposed row", effect_id)
        return _outcome(True, "approved", effect_id, "approved")

    def execute(self, effect_id: str) -> Outcome:
        """Claim an approved row once and call the provider with its key."""
        try:
            row = self._store.get_effect(effect_id)
        except CorruptEffectError as exc:
            return self._refuse_corrupt(exc)
        if row is None:
            self._store.log_event(
                effect_id, "missing", "missing", "execute refused: effect not found"
            )
            return _outcome(False, "effect not found", effect_id)
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
            preexisting = self._adapter.find(effect.idempotency_key)
        except Exception as exc:
            self._store.transition(
                effect_id, "executing", "unknown", f"find precheck failed: {exc}"
            )
            raise
        if preexisting is not None:
            return self._apply_found(
                effect_id,
                effect,
                recomputed,
                preexisting,
                matched="precheck find matches effect",
                differed="precheck find differs from effect",
            )
        try:
            record = self._adapter.create(effect, recomputed)
        except ProviderTimeout:
            self._store.transition(
                effect_id, "executing", "unknown", "provider timeout"
            )
            return _outcome(False, "provider timeout", effect_id, "unknown")
        except AlreadyExists:
            return self._reconcile_existing(effect_id, effect, recomputed)
        except ProviderError as exc:
            self._store.transition(
                effect_id, "executing", "refused", f"provider error: {exc}"
            )
            return _outcome(False, f"provider error: {exc}", effect_id, "refused")
        except Exception as exc:
            self._store.transition(
                effect_id, "executing", "unknown", f"unexpected error: {exc}"
            )
            raise
        if not record.provider_id:
            return self._mismatch(effect_id, "empty provider_id", None)
        if not record_matches(effect, recomputed, record):
            return self._mismatch(
                effect_id, "provider record does not match effect", record.provider_id
            )
        if not self._store.transition(
            effect_id,
            "executing",
            "executed",
            "provider create ok",
            provider_id=record.provider_id,
        ):
            return _outcome(False, "could not mark executed", effect_id, "executing")
        return _outcome(True, "executed", effect_id, "executed")

    def _reconcile_existing(
        self, effect_id: str, effect: Effect, recomputed: str
    ) -> Outcome:
        try:
            found = self._adapter.find(effect.idempotency_key)
        except Exception as exc:
            self._store.transition(
                effect_id, "executing", "unknown", f"find failed: {exc}"
            )
            raise
        if found is None:
            self._store.transition(
                effect_id, "executing", "unknown", "already exists but find missed"
            )
            return _outcome(
                False, "already exists but find missed", effect_id, "unknown"
            )
        return self._apply_found(
            effect_id,
            effect,
            recomputed,
            found,
            matched="already exists matches effect",
            differed="already exists differs from effect",
        )

    def _apply_found(
        self,
        effect_id: str,
        effect: Effect,
        recomputed: str,
        found: ProviderRecord,
        *,
        matched: str,
        differed: str,
    ) -> Outcome:
        if not found.provider_id:
            return self._mismatch(effect_id, "empty provider_id", None)
        if record_matches(effect, recomputed, found):
            self._store.transition(
                effect_id,
                "executing",
                "executed",
                matched,
                provider_id=found.provider_id,
            )
            return _outcome(True, "executed", effect_id, "executed")
        return self._mismatch(effect_id, differed, found.provider_id)

    def _mismatch(
        self, effect_id: str, reason: str, provider_id: str | None
    ) -> Outcome:
        self._store.transition(
            effect_id, "executing", "mismatch", reason, provider_id=provider_id
        )
        return _outcome(False, reason, effect_id, "mismatch")

    def _refuse_approve(self, effect_id: str, reason: str) -> Outcome:
        self._store.log_event(
            effect_id, "proposed", "proposed", f"approve refused: {reason}"
        )
        return _outcome(False, reason, effect_id, "proposed")

    def _refuse_execute(self, effect_id: str, status: str, reason: str) -> Outcome:
        if status == "approved" and self._store.transition(
            effect_id, "approved", "refused", f"execute refused: {reason}"
        ):
            return _outcome(False, reason, effect_id, "refused")
        return self._log_execute_refuse(status, effect_id, reason)

    def _refuse_corrupt(self, exc: CorruptEffectError) -> Outcome:
        reason = f"corrupt effect: {exc.reason}"
        if exc.status == "approved" and self._store.transition(
            exc.effect_id, "approved", "refused", f"execute refused: {reason}"
        ):
            return _outcome(False, reason, exc.effect_id, "refused")
        return self._log_execute_refuse(exc.status, exc.effect_id, reason)

    def _log_execute_refuse(self, status: str, effect_id: str, reason: str) -> Outcome:
        self._store.log_event(effect_id, status, status, f"execute refused: {reason}")
        return _outcome(False, reason, effect_id, status)
