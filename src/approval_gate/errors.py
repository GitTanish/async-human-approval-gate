"""Typed errors raised by the approval gate."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from .models import Outcome

__all__ = [
    "ApprovalError",
    "RequestNotFound",
    "InvalidRequest",
    "IdempotencyConflict",
    "AlreadyDecided",
    "RequestExpired",
    "RequestCancelled",
    "DigestMismatch",
    "RequestNotApproved",
    "IntegrityError",
    "StillPending",
    "StoreBusy",
]


class ApprovalError(Exception):
    """Base class for every error raised by the approval gate."""

    code: ClassVar[str] = "approval_error"


class RequestNotFound(ApprovalError):
    """The requested approval does not exist."""

    code: ClassVar[str] = "request_not_found"


class InvalidRequest(ApprovalError):
    """Input violates the public contract and was rejected before storage."""

    code: ClassVar[str] = "invalid_request"


class IdempotencyConflict(ApprovalError):
    """An idempotency key was reused for a different operation."""

    code: ClassVar[str] = "idempotency_conflict"


class _OutcomeError(ApprovalError):
    """Base for terminal errors that carry the current outcome."""

    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome
        super().__init__(
            f"request {outcome.request_id} is {outcome.status.value}",
        )


class AlreadyDecided(_OutcomeError):
    """The request already has a different terminal decision."""

    code: ClassVar[str] = "already_decided"


class RequestExpired(_OutcomeError):
    """The request deadline passed and expiry was recorded."""

    code: ClassVar[str] = "request_expired"


class RequestCancelled(_OutcomeError):
    """The request was cancelled before this operation."""

    code: ClassVar[str] = "request_cancelled"


class DigestMismatch(ApprovalError):
    """The submitted digest does not match the stored action digest."""

    code: ClassVar[str] = "digest_mismatch"


class RequestNotApproved(ApprovalError):
    """An execution payload was requested for a request that is not approved."""

    code: ClassVar[str] = "request_not_approved"


class IntegrityError(ApprovalError):
    """Stored payload bytes or audit history are inconsistent."""

    code: ClassVar[str] = "integrity_error"


class StillPending(ApprovalError):
    """The caller's wait timeout elapsed while the request is still pending."""

    code: ClassVar[str] = "still_pending"


class StoreBusy(ApprovalError):
    """The store could not acquire its writer lock within the busy timeout."""

    code: ClassVar[str] = "store_busy"
