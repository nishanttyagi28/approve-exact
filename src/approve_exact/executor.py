"""Propose, approve, and execute effects under the exact-approval invariant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from approve_exact.adapters.base import (
    Adapter,
    AlreadyExists,
    ProviderError,
    ProviderTimeout,
)
from approve_exact.approval import Approval, verify
from approve_exact.effect import Effect, effect_hash
from approve_exact.store import Store


@dataclass(frozen=True)
class Outcome:
    """Result of an executor action."""

    ok: bool
    reason: str
    effect_id: str | None = None
    status: str | None = None


class Executor:
    """Runs propose / approve / execute against a store and adapter."""

    def __init__(self, store: Store, adapter: Adapter, secret: str) -> None:
        if not secret:
            raise ValueError("secret must be non-empty")
        self._store = store
        self._adapter = adapter
        self._secret = secret

    def propose(self, effect: Effect) -> Outcome:
        """Persist a new effect in proposed status."""
        effect_id = self._store.propose(effect)
        row = self._store.get_effect(effect_id)
        assert row is not None
        return Outcome(
            ok=True,
            reason="proposed",
            effect_id=effect_id,
            status=row.status,
        )

    def approve(self, effect_id: str, approval: Approval, now: datetime) -> Outcome:
        """Accept a signed approval for the exact stored effect hash."""
        row = self._store.get_effect(effect_id)
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
            return self._refuse(
                effect_id,
                "proposed",
                "stored effect does not match stored hash",
            )
        if approval.effect_hash != recomputed:
            return self._refuse(
                effect_id,
                "proposed",
                "approval hash does not match stored effect",
            )
        if not verify(approval, self._secret):
            return self._refuse(
                effect_id,
                "proposed",
                "invalid approval signature",
            )
        if approval.is_expired(now):
            return self._refuse(
                effect_id,
                "proposed",
                "approval already expired",
            )

        self._store.save_approval(effect_id, approval)
        if not self._store.transition(
            effect_id, "proposed", "approved", "human approved"
        ):
            return Outcome(
                ok=False,
                reason="could not claim proposed row",
                effect_id=effect_id,
            )
        return Outcome(
            ok=True,
            reason="approved",
            effect_id=effect_id,
            status="approved",
        )

    def execute(self, effect_id: str, now: datetime) -> Outcome:
        """Claim an approved row once and call the provider with its key."""
        row = self._store.get_effect(effect_id)
        if row is None:
            return Outcome(ok=False, reason="effect not found", effect_id=effect_id)

        approval = self._store.get_approval(effect_id)
        if approval is None:
            return self._refuse_execute(row.status, effect_id, "no approval on record")

        recomputed = effect_hash(row.effect)
        if recomputed != approval.effect_hash:
            return self._refuse(
                effect_id,
                row.status,
                "recomputed hash does not match approval",
                expect_from=row.status,
            )
        if recomputed != row.effect_hash:
            return self._refuse(
                effect_id,
                row.status,
                "recomputed hash does not match stored hash",
                expect_from=row.status,
            )
        if not verify(approval, self._secret):
            return self._refuse(
                effect_id,
                row.status,
                "invalid approval signature",
                expect_from=row.status,
            )
        if approval.is_expired(now):
            return self._refuse(
                effect_id,
                row.status,
                "approval expired",
                expect_from=row.status,
            )

        if not self._store.transition(
            effect_id, "approved", "executing", "claimed for execute"
        ):
            latest = self._store.get_effect(effect_id)
            status = latest.status if latest else row.status
            self._store.log_event(
                effect_id, status, status, "execute refused: could not claim"
            )
            return Outcome(
                ok=False,
                reason="could not claim approved row",
                effect_id=effect_id,
                status=status,
            )

        try:
            record = self._adapter.create(row.effect, recomputed)
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
            self._store.transition(
                effect_id, "executing", "refused", "provider already exists"
            )
            return Outcome(
                ok=False,
                reason="provider already exists",
                effect_id=effect_id,
                status="refused",
            )
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

        self._store.set_provider_id(effect_id, record.provider_id)
        if not self._store.transition(
            effect_id, "executing", "executed", "provider create ok"
        ):
            return Outcome(
                ok=False,
                reason="could not mark executed",
                effect_id=effect_id,
                status="executing",
            )
        return Outcome(
            ok=True,
            reason="executed",
            effect_id=effect_id,
            status="executed",
        )

    def _refuse(
        self,
        effect_id: str,
        current: str,
        reason: str,
        *,
        expect_from: str | None = None,
    ) -> Outcome:
        from_status = expect_from or current
        if from_status == "proposed":
            self._store.log_event(
                effect_id, from_status, from_status, f"approve refused: {reason}"
            )
            return Outcome(
                ok=False,
                reason=reason,
                effect_id=effect_id,
                status=from_status,
            )
        if from_status == "approved":
            if self._store.transition(
                effect_id, "approved", "refused", f"execute refused: {reason}"
            ):
                return Outcome(
                    ok=False,
                    reason=reason,
                    effect_id=effect_id,
                    status="refused",
                )
        self._store.log_event(
            effect_id, from_status, from_status, f"execute refused: {reason}"
        )
        return Outcome(
            ok=False,
            reason=reason,
            effect_id=effect_id,
            status=from_status,
        )

    def _refuse_execute(self, current: str, effect_id: str, reason: str) -> Outcome:
        self._store.log_event(effect_id, current, current, f"execute refused: {reason}")
        return Outcome(
            ok=False,
            reason=reason,
            effect_id=effect_id,
            status=current,
        )
