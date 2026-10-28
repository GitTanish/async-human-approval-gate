from __future__ import annotations

from itertools import product
from typing import cast

import pytest

from approval_gate.models import Status
from approval_gate.state import Decision, Event, decide

from support import make_request


STATUSES = list(Status)
EVENTS = list(Event)
OFFSETS = (-1, 0, 1)


def expected_target(status: Status, event: Event, offset: int) -> Status | None:
    if status is not Status.PENDING:
        return None
    if offset >= 0:
        return Status.EXPIRED
    return {
        Event.APPROVE: Status.APPROVED,
        Event.REJECT: Status.REJECTED,
        Event.CANCEL: Status.CANCELLED,
        Event.EXPIRE: None,
    }[event]


@pytest.mark.parametrize(
    ("status", "event", "offset"),
    list(product(STATUSES, EVENTS, OFFSETS)),
    ids=lambda value: value.value if isinstance(value, Status | Event) else str(value),
)
def test_every_status_event_deadline_combination(
    status: Status,
    event: Event,
    offset: int,
) -> None:
    expires_at_ms = 10_000
    request = make_request(status=status, expires_at_ms=expires_at_ms)

    decision = decide(request, event, expires_at_ms + offset)

    assert decision == Decision(
        from_status=status,
        to_status=expected_target(status, event, offset),
    )


def test_late_user_action_produces_expiry_not_the_requested_outcome() -> None:
    expires_at_ms = 10_000
    request = make_request(expires_at_ms=expires_at_ms)

    for event in (Event.APPROVE, Event.REJECT, Event.CANCEL):
        decision = decide(request, event, expires_at_ms)
        assert decision.to_status is Status.EXPIRED
        assert decision.applied


def test_terminal_statuses_reject_every_event_at_every_offset() -> None:
    for status, event, offset in product(Status, Event, OFFSETS):
        decision = decide(
            make_request(status=status, expires_at_ms=10_000),
            event,
            10_000 + offset,
        )
        if status is Status.PENDING:
            continue
        assert decision.to_status is None
        assert not decision.applied
        assert decision.from_status is status


@pytest.mark.parametrize(
    ("now_ms", "expected"),
    [
        (9_999, Status.APPROVED),
        (10_000, Status.EXPIRED),
        (10_001, Status.EXPIRED),
    ],
)
def test_approval_boundary_is_exclusive(now_ms: int, expected: Status) -> None:
    decision = decide(make_request(expires_at_ms=10_000), Event.APPROVE, now_ms)
    assert decision.to_status is expected


def test_expiry_before_deadline_is_a_noop() -> None:
    decision = decide(make_request(expires_at_ms=10_000), Event.EXPIRE, 9_999)
    assert decision.to_status is None


def test_unknown_event_raises() -> None:
    request = make_request()
    with pytest.raises(ValueError, match="unknown event"):
        decide(request, cast(Event, "dance"), 0)
