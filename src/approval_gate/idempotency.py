"""Deterministic request-idempotency fingerprints."""

from __future__ import annotations

from typing import Any

from .payload import canonicalize_action, sha256_hex
from .state import Event

__all__ = ["operation_fingerprint"]


def operation_fingerprint(
    *,
    request_id: str,
    operation: Event,
    actor: str,
    reason: str | None,
    expected_digest: str | None,
) -> str:
    """Hash the exact user operation for replay-vs-conflict decisions.

    The version field lets the format evolve.  ``cancel`` omits the digest;
    approve and reject require one.
    """

    if operation is Event.EXPIRE:
        raise ValueError("expiry is not a user operation")

    payload: dict[str, Any] = {
        "v": 1,
        "request_id": request_id,
        "operation": operation.value,
        "actor": actor,
        "reason": reason,
    }
    if operation is Event.CANCEL:
        if expected_digest is not None:
            raise ValueError("cancel must not include an expected digest")
    else:
        if not expected_digest:
            raise ValueError("approve and reject require an expected digest")
        payload["expected_digest"] = expected_digest

    return sha256_hex(canonicalize_action(payload))
