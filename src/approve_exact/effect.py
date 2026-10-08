"""Canonical payment effect and its content hash."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

KIND_PAYMENT_LINK = "payment_link"
CURRENCY_INR = "INR"
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
HASH_PREFIX = "approve-exact/v1\n"


@dataclass(frozen=True)
class Effect:
    """Exact payment effect that must be approved before execution."""

    kind: str
    amount_paise: int
    currency: str
    customer_email: str
    customer_name: str
    description: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for name in (
            "kind",
            "currency",
            "customer_email",
            "customer_name",
            "description",
            "idempotency_key",
        ):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a str")
        if self.kind != KIND_PAYMENT_LINK:
            raise ValueError(f"kind must be {KIND_PAYMENT_LINK!r}")
        if isinstance(self.amount_paise, bool) or not isinstance(
            self.amount_paise, int
        ):
            raise TypeError("amount_paise must be an int, not bool or float")
        if self.amount_paise <= 0:
            raise ValueError("amount_paise must be > 0")
        if self.currency != CURRENCY_INR:
            raise ValueError(f"currency must be {CURRENCY_INR!r}")
        if not IDEMPOTENCY_KEY_RE.fullmatch(self.idempotency_key):
            raise ValueError("idempotency_key must be 1–40 chars of [A-Za-z0-9_-]")


def new_idempotency_key() -> str:
    """Return a unique idempotency key within the length and charset limits."""
    # token_urlsafe(24) is 32 chars of [A-Za-z0-9_-]
    return secrets.token_urlsafe(24)


def _reject_bad_numbers(value: Any, path: str) -> None:
    if isinstance(value, bool):
        raise TypeError(f"{path}: bool is not allowed (bool-as-int rejected)")
    if isinstance(value, float):
        raise TypeError(f"{path}: floats are not allowed")


def _normalize(value: Any, path: str = "$") -> Any:
    _reject_bad_numbers(value, path)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, int):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path}: keys must be strings")
            norm_key = unicodedata.normalize("NFC", key)
            out[norm_key] = _normalize(item, f"{path}.{norm_key}")
        return out
    if isinstance(value, list):
        return [_normalize(item, f"{path}[{i}]") for i, item in enumerate(value)]
    raise TypeError(f"{path}: unsupported type {type(value).__name__}")


def canonical_json(data: Mapping[str, Any]) -> str:
    """Serialize with sorted keys, tight separators, NFC strings, no floats."""
    if not isinstance(data, Mapping):
        raise TypeError("canonical_json expects a mapping")
    normalized = _normalize(data)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def effect_to_dict(effect: Effect) -> dict[str, Any]:
    """Return effect fields as a plain dict."""
    return asdict(effect)


def effect_hash(effect: Effect) -> str:
    """SHA-256 of the version prefix plus canonical JSON of the effect."""
    payload = HASH_PREFIX + canonical_json(effect_to_dict(effect))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
