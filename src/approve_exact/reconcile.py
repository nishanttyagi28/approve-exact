"""Verify executed effects and reconcile unknown ones against the provider."""

from __future__ import annotations

from approve_exact.adapters.base import Adapter, record_matches
from approve_exact.effect import effect_hash
from approve_exact.executor import Outcome, _outcome
from approve_exact.store import CorruptEffectError, Store


class FollowUp:
    """Post-execute verify and reconcile against the provider."""

    def __init__(self, store: Store, adapter: Adapter) -> None:
        self._store = store
        self._adapter = adapter

    def verify(self, effect_id: str) -> Outcome:
        """Compare the provider record to the stored effect; only from executed."""
        try:
            row = self._store.get_effect(effect_id)
        except CorruptEffectError as exc:
            self._store.log_event(
                effect_id,
                exc.status,
                exc.status,
                f"verify refused: corrupt effect: {exc.reason}",
            )
            return _outcome(
                False, f"corrupt effect: {exc.reason}", effect_id, exc.status
            )
        if row is None:
            self._store.log_event(
                effect_id, "missing", "missing", "verify refused: effect not found"
            )
            return _outcome(False, "effect not found", effect_id)
        if row.status != "executed":
            self._store.log_event(
                effect_id, row.status, row.status, "verify refused: not executed"
            )
            return _outcome(False, "not in executed status", effect_id, row.status)

        digest = effect_hash(row.effect)
        try:
            record = self._adapter.find(row.effect.idempotency_key)
        except Exception as exc:
            self._store.log_event(
                effect_id,
                "executed",
                "executed",
                f"verify find failed: {type(exc).__name__}",
            )
            raise
        if record is None:
            self._store.log_event(
                effect_id, "executed", "executed", "verify: provider record missing"
            )
            return _outcome(False, "provider record missing", effect_id, "executed")

        if row.provider_id and row.provider_id != record.provider_id:
            reason = (
                f"verify: stored provider_id {row.provider_id!r} "
                f"differs from record {record.provider_id!r}"
            )
            if self._store.transition(effect_id, "executed", "mismatch", reason):
                return _outcome(False, reason, effect_id, "mismatch")
            return _outcome(False, "could not mark mismatch", effect_id, "executed")

        if not record_matches(row.effect, digest, record):
            if self._store.transition(
                effect_id,
                "executed",
                "mismatch",
                "verify: provider record does not match effect",
                provider_id=record.provider_id or None,
            ):
                return _outcome(
                    False,
                    "provider record does not match effect",
                    effect_id,
                    "mismatch",
                )
            return _outcome(False, "could not mark mismatch", effect_id, "executed")

        if not self._store.transition(
            effect_id,
            "executed",
            "verified",
            "verify: provider record matches",
            provider_id=record.provider_id or row.provider_id,
        ):
            return _outcome(False, "could not mark verified", effect_id, "executed")
        return _outcome(True, "verified", effect_id, "verified")

    def reconcile(self, effect_id: str) -> Outcome:
        """Resolve an unknown effect via find only; never creates."""
        try:
            row = self._store.get_effect(effect_id)
        except CorruptEffectError as exc:
            self._store.log_event(
                effect_id,
                exc.status,
                exc.status,
                f"reconcile refused: corrupt effect: {exc.reason}",
            )
            return _outcome(
                False, f"corrupt effect: {exc.reason}", effect_id, exc.status
            )
        if row is None:
            self._store.log_event(
                effect_id, "missing", "missing", "reconcile refused: effect not found"
            )
            return _outcome(False, "effect not found", effect_id)
        if row.status != "unknown":
            self._store.log_event(
                effect_id, row.status, row.status, "reconcile refused: not unknown"
            )
            return _outcome(False, "not in unknown status", effect_id, row.status)

        try:
            record = self._adapter.find(row.effect.idempotency_key)
        except Exception as exc:
            self._store.log_event(
                effect_id,
                "unknown",
                "unknown",
                f"reconcile find failed: {type(exc).__name__}",
            )
            raise
        if record is None:
            self._store.log_event(
                effect_id, "unknown", "unknown", "reconcile: provider record not found"
            )
            return _outcome(False, "provider record not found", effect_id, "unknown")

        if not record.provider_id:
            if self._store.transition(
                effect_id, "unknown", "mismatch", "reconcile: empty provider_id"
            ):
                return _outcome(False, "empty provider_id", effect_id, "mismatch")
            return _outcome(False, "could not mark mismatch", effect_id, "unknown")

        if not self._store.mark_reconciled(effect_id, record.provider_id):
            return _outcome(False, "could not mark executed", effect_id, "unknown")
        return self.verify(effect_id)
