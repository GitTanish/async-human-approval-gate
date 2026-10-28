"""The pure state machine reducer.

The reducer answers one question: given a request snapshot, an event, and the
current time, which transition is legal?  It never reads a clock, opens a
transaction, or looks at idempotency keys.  Stores apply its decision
conditionally; the database decides which competing legal transition wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import Request, Status

__all__ = [
    "Event",
    "Decision",
    "EXPIRY_ACTOR",
    "EXPIRY_REASON",
    "decide",
]


class Event(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    CANCEL = "cancel"
    EXPIRE = "expire"


EXPIRY_ACTOR = "system:expiry"
EXPIRY_REASON = "deadline_reached"

_ACTION_TARGETS: dict[Event, Status] = {
    Event.APPROVE: Status.APPROVED,
    Event.REJECT: Status.REJECTED,
    Event.CANCEL: Status.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class Decision:
    """A pure reducer result; ``to_status is None`` means no transition."""

    from_status: Status
    to_status: Status | None

    @property
    def applied(self) -> bool:
        return self.to_status is not None


def decide(request: Request, event: Event, now_ms: int) -> Decision:
    """Decide the legal transition for ``event`` at ``now_ms``.

    Deadline equality expires.  A pending request at or past its deadline
    expires regardless of which event arrives, so a late approve cannot race
    the deadline.  Terminal requests never change state.
    """

    try:
        event = Event(event)
    except ValueError:
        raise ValueError(f"unknown event: {event!r}") from None

    current = request.status
    if current is not Status.PENDING:
        return Decision(from_status=current, to_status=None)

    if now_ms >= request.expires_at_ms:
        return Decision(from_status=current, to_status=Status.EXPIRED)

    if event is Event.EXPIRE:
        return Decision(from_status=current, to_status=None)

    return Decision(from_status=current, to_status=_ACTION_TARGETS[event])
