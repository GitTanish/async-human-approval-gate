"""Store protocol and shared result types.

Stores own transactions, dedupe races, idempotency lookup, expiry recording,
and result classification.  They do not canonicalize actions or validate
public inputs; the facade does that before building commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..errors import IntegrityError
from ..models import Outcome, Request, Status, Transition
from ..state import Event

__all__ = [
    "Store",
    "NewRequest",
    "ProposeResult",
    "SweepResult",
    "ResponseCommand",
    "ResponseKind",
    "ResponseResult",
    "outcome_from_transition",
    "target_status",
    "response_kind_for_terminal",
]


@dataclass(frozen=True, slots=True)
class NewRequest:
    """Validated input to a store proposal."""

    id: str
    workflow_id: str
    dedupe_key: str | None
    canonical_json: bytes
    action_digest: str
    summary: str
    requested_by: str
    ttl_ms: int


@dataclass(frozen=True, slots=True)
class ProposeResult:
    request: Request
    deduplicated: bool
    transitions: tuple[Transition, ...] = ()


@dataclass(frozen=True, slots=True)
class SweepResult:
    transitions: tuple[Transition, ...] = ()

    @property
    def count(self) -> int:
        return len(self.transitions)


@dataclass(frozen=True, slots=True)
class ResponseCommand:
    """A user decision or cancellation, with precomputed idempotency data."""

    request_id: str
    operation: Event
    actor: str
    reason: str | None
    expected_digest: str | None
    idempotency_key: str
    fingerprint: str


class ResponseKind(StrEnum):
    APPLIED = "applied"
    REPLAYED = "replayed"
    NOT_FOUND = "not_found"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    ALREADY_DECIDED = "already_decided"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    DIGEST_MISMATCH = "digest_mismatch"


@dataclass(frozen=True, slots=True)
class ResponseResult:
    kind: ResponseKind
    outcome: Outcome | None = None
    transition: Transition | None = None


class Store(Protocol):
    """The storage contract shared by memory and SQLite implementations."""

    def propose(self, command: NewRequest) -> ProposeResult: ...

    def get(self, request_id: str) -> Request | None: ...

    def list(
        self,
        *,
        status: Status | None = None,
        workflow_id: str | None = None,
        limit: int = 100,
        expires_after_ms: int | None = None,
    ) -> list[Request]: ...

    def outcome(self, request_id: str) -> Outcome | None: ...

    def get_transition(self, request_id: str) -> Transition | None: ...

    def sweep(self, *, request_id: str | None = None) -> SweepResult: ...

    def respond(self, command: ResponseCommand) -> ResponseResult: ...

    def close(self) -> None: ...


def target_status(operation: Event) -> Status:
    """Map a user operation to its terminal status."""

    try:
        return {
            Event.APPROVE: Status.APPROVED,
            Event.REJECT: Status.REJECTED,
            Event.CANCEL: Status.CANCELLED,
        }[operation]
    except KeyError:
        raise ValueError(f"not a user operation: {operation!r}") from None


def response_kind_for_terminal(status: Status) -> ResponseKind:
    if status is Status.APPROVED or status is Status.REJECTED:
        return ResponseKind.ALREADY_DECIDED
    if status is Status.EXPIRED:
        return ResponseKind.EXPIRED
    if status is Status.CANCELLED:
        return ResponseKind.CANCELLED
    raise ValueError(f"not a terminal status: {status!r}")


def outcome_from_transition(
    request: Request,
    transition: Transition,
    *,
    replayed: bool = False,
) -> Outcome:
    """Build an outcome while checking that status and audit history agree."""

    if transition.from_status is not Status.PENDING:
        raise IntegrityError("terminal transition does not start from pending")
    if transition.to_status is Status.PENDING:
        raise IntegrityError("terminal transition has a non-terminal target")
    if request.status is not transition.to_status:
        raise IntegrityError(
            "request status does not match its terminal transition",
        )
    return Outcome(
        request_id=request.id,
        status=transition.to_status,
        action_digest=request.action_digest,
        actor=transition.actor,
        reason=transition.reason,
        at_ms=transition.at_ms,
        replayed=replayed,
    )
