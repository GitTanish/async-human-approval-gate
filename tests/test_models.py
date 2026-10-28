from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from approval_gate.models import (
    TERMINAL,
    Outcome,
    PendingPage,
    Proposal,
    Request,
    Status,
    Transition,
    VerifiedAction,
)

from support import make_request


def test_status_values() -> None:
    assert [status.value for status in Status] == [
        "pending",
        "approved",
        "rejected",
        "expired",
        "cancelled",
    ]


def test_terminal_statuses() -> None:
    assert TERMINAL == {
        Status.APPROVED,
        Status.REJECTED,
        Status.EXPIRED,
        Status.CANCELLED,
    }
    assert Status.PENDING not in TERMINAL
    assert Status.PENDING.is_terminal is False
    assert all(status.is_terminal for status in TERMINAL)


@pytest.mark.parametrize(
    ("status", "now_ms", "expected"),
    [
        (Status.PENDING, 9_999, False),
        (Status.PENDING, 10_000, True),
        (Status.PENDING, 10_001, True),
        (Status.APPROVED, 10_001, False),
        (Status.REJECTED, 10_001, False),
        (Status.EXPIRED, 10_001, False),
        (Status.CANCELLED, 10_001, False),
    ],
)
def test_request_overdue_at(
    status: Status,
    now_ms: int,
    expected: bool,
) -> None:
    request = make_request(status=status, expires_at_ms=10_000)
    assert request.overdue_at(now_ms) is expected


def test_request_is_frozen() -> None:
    request = make_request()
    with pytest.raises(FrozenInstanceError):
        request.status = Status.APPROVED  # type: ignore[misc]


def test_request_action_is_a_read_only_mapping() -> None:
    request = make_request()
    assert isinstance(request.action, MappingProxyType)
    with pytest.raises(TypeError):
        request.action["new"] = 1  # type: ignore[index]


def test_outcome_and_transition_models() -> None:
    outcome = Outcome(
        request_id="req_1",
        status=Status.APPROVED,
        action_digest="a" * 64,
        actor="alice@corp",
        reason="copy reviewed",
        at_ms=1_234,
    )
    transition = Transition(
        seq=1,
        request_id="req_1",
        from_status=Status.PENDING,
        to_status=Status.APPROVED,
        actor="alice@corp",
        reason="copy reviewed",
        at_ms=1_234,
        idempotency_key="key-1",
        request_fingerprint="b" * 64,
    )

    assert outcome.replayed is False
    assert transition.seq == 1
    with pytest.raises(FrozenInstanceError):
        outcome.replayed = True  # type: ignore[misc]


def test_proposal_pending_page_and_verified_action_models() -> None:
    request = make_request()
    proposal = Proposal(request=request, deduplicated=True)
    page = PendingPage(requests=[request], swept=2)
    verified = VerifiedAction(
        request_id=request.id,
        action=request.action,
        canonical_json=request.canonical_json,
        action_digest=request.action_digest,
    )

    assert proposal.deduplicated
    assert page.requests == [request]
    assert page.swept == 2
    assert verified.canonical_json == b"{}"
    assert len(verified.action_digest) == 64


def test_pending_page_requests_are_domain_requests() -> None:
    request: Request = make_request()
    page = PendingPage(requests=[request], swept=0)
    assert page.requests[0].id == "req_1"
