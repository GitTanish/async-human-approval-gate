"""Domain models shared by the reducer, stores, and facade."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .payload import FrozenJSON

__all__ = [
    "Status",
    "TERMINAL",
    "Request",
    "Transition",
    "Outcome",
    "VerifiedAction",
    "Proposal",
    "PendingPage",
]


class Status(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self is not Status.PENDING


TERMINAL: frozenset[Status] = frozenset(
    {
        Status.APPROVED,
        Status.REJECTED,
        Status.EXPIRED,
        Status.CANCELLED,
    },
)


@dataclass(frozen=True, slots=True)
class Request:
    """The stored operational state plus an immutable action snapshot."""

    id: str
    workflow_id: str
    dedupe_key: str | None
    status: Status
    action: FrozenJSON
    canonical_json: bytes
    action_digest: str
    summary: str
    requested_by: str
    created_at_ms: int
    expires_at_ms: int
    version: int
    overdue: bool = False

    def overdue_at(self, now_ms: int) -> bool:
        """Whether this request is pending and at or past its deadline."""

        return self.status is Status.PENDING and now_ms >= self.expires_at_ms


@dataclass(frozen=True, slots=True)
class Transition:
    """One append-only audit row, including idempotency evidence."""

    seq: int
    request_id: str
    from_status: Status
    to_status: Status
    actor: str
    reason: str | None
    at_ms: int
    idempotency_key: str | None
    request_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class Outcome:
    """The terminal result of a request."""

    request_id: str
    status: Status
    action_digest: str
    actor: str | None
    reason: str | None
    at_ms: int | None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class VerifiedAction:
    """The exact immutable payload an executor is authorized to run."""

    request_id: str
    action: FrozenJSON
    canonical_json: bytes
    action_digest: str


@dataclass(frozen=True, slots=True)
class Proposal:
    request: Request
    deduplicated: bool


@dataclass(frozen=True, slots=True)
class PendingPage:
    requests: list[Request]
    swept: int
