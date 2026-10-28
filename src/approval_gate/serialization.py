"""JSON-ready serialization helpers for the CLI and demo adapters."""

from __future__ import annotations

from typing import Any

from .models import Outcome, Request, Transition
from .payload import thaw_json

__all__ = ["request_to_dict", "outcome_to_dict", "transition_to_dict"]


def request_to_dict(request: Request) -> dict[str, Any]:
    return {
        "id": request.id,
        "workflow_id": request.workflow_id,
        "dedupe_key": request.dedupe_key,
        "status": request.status.value,
        "action": thaw_json(request.action),
        "action_digest": request.action_digest,
        "summary": request.summary,
        "requested_by": request.requested_by,
        "created_at_ms": request.created_at_ms,
        "expires_at_ms": request.expires_at_ms,
        "version": request.version,
        "overdue": request.overdue,
    }


def outcome_to_dict(outcome: Outcome) -> dict[str, Any]:
    return {
        "request_id": outcome.request_id,
        "status": outcome.status.value,
        "action_digest": outcome.action_digest,
        "actor": outcome.actor,
        "reason": outcome.reason,
        "at_ms": outcome.at_ms,
        "replayed": outcome.replayed,
    }


def transition_to_dict(transition: Transition) -> dict[str, Any]:
    return {
        "seq": transition.seq,
        "request_id": transition.request_id,
        "from_status": transition.from_status.value,
        "to_status": transition.to_status.value,
        "actor": transition.actor,
        "reason": transition.reason,
        "at_ms": transition.at_ms,
        "idempotency_key": transition.idempotency_key,
        "request_fingerprint": transition.request_fingerprint,
    }
