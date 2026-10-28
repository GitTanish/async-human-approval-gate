from __future__ import annotations

import pytest

from approval_gate.models import Status
from approval_gate.payload import canonicalize_action, sha256_hex, thaw_json
from approval_gate.state import Event
from approval_gate.stores import ResponseCommand, ResponseKind

from support import StoreHarness


def test_propose_creates_a_pending_version_zero_request(
    harness: StoreHarness,
) -> None:
    canonical = canonicalize_action({"b": 2, "a": [1]})
    result = harness.propose(
        action={"b": 2, "a": [1]},
        summary="Deploy the release",
        requested_by="agent:release",
        ttl_ms=10_000,
        dedupe_key="release-42",
    )

    assert result.deduplicated is False
    assert result.transitions == ()
    request = result.request
    assert request.id.startswith("req_")
    assert request.workflow_id == "wf_1"
    assert request.dedupe_key == "release-42"
    assert request.status is Status.PENDING
    assert request.canonical_json == canonical
    assert request.action_digest == sha256_hex(canonical)
    assert thaw_json(request.action) == {"a": [1], "b": 2}
    assert request.summary == "Deploy the release"
    assert request.requested_by == "agent:release"
    assert request.created_at_ms == harness.clock.now_ms()
    assert request.expires_at_ms == harness.clock.now_ms() + 10_000
    assert request.version == 0
    assert request.overdue is False

    assert harness.store.get(request.id) == request
    assert harness.store.outcome(request.id) is None
    assert harness.store.get_transition(request.id) is None


def test_dedupe_returns_the_existing_pending_request(
    harness: StoreHarness,
) -> None:
    first = harness.propose(
        action={"value": 1},
        workflow_id="wf",
        dedupe_key="same",
    )
    second = harness.propose(
        action={"value": 2},
        workflow_id="wf",
        dedupe_key="same",
    )

    assert second.deduplicated is True
    assert second.request.id == first.request.id
    assert thaw_json(second.request.action) == {"value": 1}
    assert second.transitions == ()


def test_dedupe_scope_is_workflow_and_key(harness: StoreHarness) -> None:
    first = harness.propose(workflow_id="wf_a", dedupe_key="same")
    other_workflow = harness.propose(workflow_id="wf_b", dedupe_key="same")
    other_key = harness.propose(workflow_id="wf_a", dedupe_key="other")
    undeduped = harness.propose(workflow_id="wf_a")

    ids = {
        first.request.id,
        other_workflow.request.id,
        other_key.request.id,
        undeduped.request.id,
    }
    assert len(ids) == 4


def test_dedupe_expires_an_overdue_request_and_creates_a_new_one(
    harness: StoreHarness,
) -> None:
    first = harness.propose(
        action={"value": 1},
        dedupe_key="same",
        ttl_ms=1_000,
    )
    harness.clock.advance_ms(1_000)

    second = harness.propose(
        action={"value": 2},
        dedupe_key="same",
        ttl_ms=5_000,
    )

    assert second.deduplicated is False
    assert second.request.id != first.request.id
    assert thaw_json(second.request.action) == {"value": 2}

    assert len(second.transitions) == 1
    transition = second.transitions[0]
    assert transition.request_id == first.request.id
    assert transition.to_status is Status.EXPIRED
    assert transition.actor == "system:expiry"
    assert transition.reason == "deadline_reached"
    assert transition.at_ms == first.request.expires_at_ms
    assert transition.idempotency_key is None
    assert transition.request_fingerprint is None

    old = harness.store.get(first.request.id)
    assert old is not None
    assert old.status is Status.EXPIRED
    assert old.version == 1
    old_outcome = harness.store.outcome(first.request.id)
    assert old_outcome is not None
    assert old_outcome.status is Status.EXPIRED
    assert old_outcome.actor == "system:expiry"
    assert harness.store.sweep().count == 0


def test_dedupe_key_can_be_reused_after_a_terminal_decision(
    harness: StoreHarness,
) -> None:
    first = harness.propose(dedupe_key="same")
    harness.respond(first.request.id, Event.APPROVE)

    second = harness.propose(dedupe_key="same")

    assert second.deduplicated is False
    assert second.request.id != first.request.id
    assert harness.store.get(first.request.id).status is Status.APPROVED


def test_reads_do_not_sweep_or_mutate(harness: StoreHarness) -> None:
    result = harness.propose(ttl_ms=1_000)
    request_id = result.request.id
    harness.clock.advance_ms(1_000)

    read = harness.store.get(request_id)
    assert read is not None
    assert read.status is Status.PENDING
    assert read.version == 0
    assert harness.store.outcome(request_id) is None
    assert harness.store.get_transition(request_id) is None
    assert harness.store.list()[0].status is Status.PENDING

    # Only the explicit sweep may expire it.
    assert harness.store.sweep().count == 1
    expired = harness.store.get(request_id)
    assert expired is not None
    assert expired.status is Status.EXPIRED
    assert expired.version == 1


def test_list_filters_orders_and_limits(harness: StoreHarness) -> None:
    a = harness.propose(workflow_id="wf_a").request
    harness.clock.advance_ms(1)
    b = harness.propose(workflow_id="wf_b").request
    harness.clock.advance_ms(1)
    c = harness.propose(workflow_id="wf_a").request
    harness.clock.advance_ms(1)
    d = harness.propose(workflow_id="wf_a").request

    harness.respond(c.id, Event.APPROVE)
    harness.respond(d.id, Event.CANCEL, actor="agent:planner")

    assert [item.id for item in harness.store.list()] == [
        d.id,
        c.id,
        b.id,
        a.id,
    ]
    assert [item.id for item in harness.store.list(workflow_id="wf_a")] == [
        d.id,
        c.id,
        a.id,
    ]
    assert [item.id for item in harness.store.list(status=Status.APPROVED)] == [
        c.id,
    ]
    assert [
        item.id
        for item in harness.store.list(
            status=Status.PENDING,
            workflow_id="wf_a",
        )
    ] == [a.id]
    assert [item.id for item in harness.store.list(limit=1)] == [d.id]
    assert harness.store.list(limit=0) == []


def test_list_expiry_filter_is_strictly_after_the_boundary(
    harness: StoreHarness,
) -> None:
    first = harness.propose(ttl_ms=1_000).request
    harness.clock.advance_ms(500)
    second = harness.propose(ttl_ms=1_000).request

    now_ms = harness.clock.now_ms()
    assert [
        request.id
        for request in harness.store.list(
            status=Status.PENDING,
            expires_after_ms=now_ms,
        )
    ] == [second.id, first.id]

    # A request expiring exactly at the boundary is excluded.
    assert [
        request.id
        for request in harness.store.list(
            expires_after_ms=first.expires_at_ms,
        )
    ] == [second.id]
    assert harness.store.list(
        expires_after_ms=second.expires_at_ms,
    ) == []


def test_sweep_expires_every_due_request_exactly_once(
    harness: StoreHarness,
) -> None:
    first = harness.propose(ttl_ms=1_000).request
    second = harness.propose(ttl_ms=1_000).request
    harness.clock.advance_ms(999)

    assert harness.store.sweep().count == 0
    harness.clock.advance_ms(1)

    swept = harness.store.sweep()
    assert swept.count == 2
    assert {transition.request_id for transition in swept.transitions} == {
        first.id,
        second.id,
    }
    for transition in swept.transitions:
        assert transition.to_status is Status.EXPIRED
        assert transition.at_ms == harness.clock.now_ms()
    for request_id in (first.id, second.id):
        request = harness.store.get(request_id)
        assert request is not None
        assert request.status is Status.EXPIRED
        assert request.version == 1
        outcome = harness.store.outcome(request_id)
        assert outcome is not None
        assert outcome.actor == "system:expiry"
        assert outcome.reason == "deadline_reached"
        assert outcome.at_ms == harness.clock.now_ms()

    assert harness.store.sweep().count == 0


def test_targeted_sweep_leaves_other_requests_pending(
    harness: StoreHarness,
) -> None:
    first = harness.propose(ttl_ms=1_000).request
    second = harness.propose(ttl_ms=1_000).request
    harness.clock.advance_ms(1_000)

    assert harness.store.sweep(request_id=first.id).count == 1
    assert harness.store.get(first.id).status is Status.EXPIRED
    assert harness.store.get(second.id).status is Status.PENDING
    assert harness.store.sweep(request_id=first.id).count == 0
    assert harness.store.sweep(request_id="missing").count == 0
    assert harness.store.sweep().count == 1


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (Event.APPROVE, Status.APPROVED),
        (Event.REJECT, Status.REJECTED),
        (Event.CANCEL, Status.CANCELLED),
    ],
)
def test_user_operation_applies_one_terminal_transition(
    harness: StoreHarness,
    operation: Event,
    expected: Status,
) -> None:
    request = harness.propose().request

    result = harness.respond(
        request.id,
        operation,
        actor="alice@corp",
        reason="reviewed",
        idempotency_key="key-1",
    )

    assert result.kind is ResponseKind.APPLIED
    assert result.transition is not None
    assert result.outcome is not None
    assert result.outcome.status is expected
    assert result.outcome.actor == "alice@corp"
    assert result.outcome.reason == "reviewed"
    assert result.outcome.at_ms == harness.clock.now_ms()
    assert result.outcome.action_digest == request.action_digest
    assert result.outcome.replayed is False

    transition = result.transition
    assert transition.request_id == request.id
    assert transition.from_status is Status.PENDING
    assert transition.to_status is expected
    assert transition.actor == "alice@corp"
    assert transition.reason == "reviewed"
    assert transition.at_ms == harness.clock.now_ms()
    assert transition.idempotency_key == "key-1"
    assert transition.request_fingerprint is not None
    assert len(transition.request_fingerprint) == 64

    stored = harness.store.get(request.id)
    assert stored is not None
    assert stored.status is expected
    assert stored.version == 1
    assert harness.store.get_transition(request.id) == transition
    assert harness.store.outcome(request.id) == result.outcome


def test_same_key_and_operation_replays_without_a_new_transition(
    harness: StoreHarness,
) -> None:
    request = harness.propose().request
    first = harness.respond(
        request.id,
        Event.APPROVE,
        actor="alice@corp",
        reason="reviewed",
        idempotency_key="key-1",
    )
    transition = first.transition

    harness.clock.advance_ms(10_000_000)
    replay = harness.respond(
        request.id,
        Event.APPROVE,
        actor="alice@corp",
        reason="reviewed",
        idempotency_key="key-1",
    )

    assert replay.kind is ResponseKind.REPLAYED
    assert replay.transition is None
    assert replay.outcome is not None
    assert replay.outcome.replayed is True
    assert replay.outcome.status is Status.APPROVED
    assert replay.outcome.actor == "alice@corp"
    assert replay.outcome.reason == "reviewed"
    assert replay.outcome.at_ms == first.outcome.at_ms
    assert harness.store.get(request.id).version == 1
    assert harness.store.get_transition(request.id) == transition


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": "a different reason"},
        {"actor": "bob@corp"},
        {"expected_digest": "0" * 64},
    ],
)
def test_same_key_with_different_operation_conflicts(
    harness: StoreHarness,
    changes: dict[str, str],
) -> None:
    request = harness.propose().request
    first = harness.respond(
        request.id,
        Event.APPROVE,
        actor="alice@corp",
        reason="reviewed",
        idempotency_key="key-1",
    )

    arguments = {
        "actor": "alice@corp",
        "reason": "reviewed",
        "expected_digest": request.action_digest,
        "idempotency_key": "key-1",
    }
    arguments.update(changes)

    conflict = harness.respond(
        request.id,
        Event.APPROVE,
        **arguments,
    )

    assert conflict.kind is ResponseKind.IDEMPOTENCY_CONFLICT
    assert conflict.outcome is None
    assert harness.store.get(request.id).version == 1
    assert harness.store.get_transition(request.id) == first.transition


def test_same_key_with_a_different_operation_conflicts(
    harness: StoreHarness,
) -> None:
    request = harness.propose().request
    harness.respond(
        request.id,
        Event.APPROVE,
        idempotency_key="shared",
    )

    conflict = harness.respond(
        request.id,
        Event.REJECT,
        idempotency_key="shared",
    )

    assert conflict.kind is ResponseKind.IDEMPOTENCY_CONFLICT
    assert harness.store.get(request.id).status is Status.APPROVED


def test_same_key_on_another_request_is_independent(
    harness: StoreHarness,
) -> None:
    first = harness.propose().request
    second = harness.propose().request

    harness.respond(first.id, Event.APPROVE, idempotency_key="shared")
    result = harness.respond(second.id, Event.APPROVE, idempotency_key="shared")

    assert result.kind is ResponseKind.APPLIED
    assert harness.store.get(second.id).status is Status.APPROVED


def test_failed_digest_attempt_does_not_burn_the_key(
    harness: StoreHarness,
) -> None:
    request = harness.propose().request

    failed = harness.respond(
        request.id,
        Event.APPROVE,
        expected_digest="0" * 64,
        idempotency_key="retry-me",
    )
    assert failed.kind is ResponseKind.DIGEST_MISMATCH
    assert harness.store.get(request.id).status is Status.PENDING
    assert harness.store.get_transition(request.id) is None

    applied = harness.respond(
        request.id,
        Event.APPROVE,
        idempotency_key="retry-me",
    )
    assert applied.kind is ResponseKind.APPLIED
    assert harness.store.get(request.id).status is Status.APPROVED


@pytest.mark.parametrize("operation", [Event.APPROVE, Event.REJECT])
def test_wrong_digest_never_decides(
    harness: StoreHarness,
    operation: Event,
) -> None:
    request = harness.propose().request

    result = harness.respond(
        request.id,
        operation,
        expected_digest=request.action_digest[:12],
    )

    assert result.kind is ResponseKind.DIGEST_MISMATCH
    assert harness.store.get(request.id).status is Status.PENDING
    assert harness.store.get(request.id).version == 0
    assert harness.store.get_transition(request.id) is None


def test_cancel_has_no_digest_precondition(harness: StoreHarness) -> None:
    request = harness.propose().request
    from approval_gate.idempotency import operation_fingerprint

    fingerprint = operation_fingerprint(
        request_id=request.id,
        operation=Event.CANCEL,
        actor="agent:planner",
        reason="obsolete",
        expected_digest=None,
    )
    result = harness.store.respond(
        ResponseCommand(
            request_id=request.id,
            operation=Event.CANCEL,
            actor="agent:planner",
            reason="obsolete",
            expected_digest="not-a-digest",
            idempotency_key="cancel-1",
            fingerprint=fingerprint,
        ),
    )

    assert result.kind is ResponseKind.APPLIED
    assert harness.store.get(request.id).status is Status.CANCELLED


@pytest.mark.parametrize(
    ("operation", "expected_digest"),
    [
        (Event.APPROVE, None),
        (Event.REJECT, None),
        (Event.CANCEL, None),
    ],
)
def test_response_at_the_exact_deadline_records_expiry(
    harness: StoreHarness,
    operation: Event,
    expected_digest: str | None,
) -> None:
    request = harness.propose(ttl_ms=1_000).request
    harness.clock.advance_ms(1_000)

    result = harness.respond(
        request.id,
        operation,
        expected_digest=expected_digest,
        idempotency_key="at-deadline",
    )

    assert result.kind is ResponseKind.EXPIRED
    assert result.outcome is not None
    assert result.outcome.status is Status.EXPIRED
    assert result.outcome.actor == "system:expiry"
    assert result.outcome.reason == "deadline_reached"
    assert result.outcome.at_ms == harness.clock.now_ms()
    assert result.transition is not None
    assert result.transition.idempotency_key is None
    assert result.transition.request_fingerprint is None

    stored = harness.store.get(request.id)
    assert stored is not None
    assert stored.status is Status.EXPIRED
    assert stored.version == 1
    assert harness.store.get_transition(request.id) == result.transition
    # The failed key was not burned.
    assert result.transition.idempotency_key != "at-deadline"


def test_response_after_a_sweep_classifies_without_a_new_transition(
    harness: StoreHarness,
) -> None:
    request = harness.propose(ttl_ms=1_000).request
    harness.clock.advance_ms(1_000)
    swept = harness.store.sweep()
    assert swept.count == 1

    result = harness.respond(request.id, Event.APPROVE)

    assert result.kind is ResponseKind.EXPIRED
    assert result.transition is None
    assert result.outcome is not None
    assert result.outcome.actor == "system:expiry"
    assert harness.store.get(request.id).version == 1


def test_already_decided_carries_the_current_outcome(
    harness: StoreHarness,
) -> None:
    request = harness.propose().request
    first = harness.respond(
        request.id,
        Event.APPROVE,
        actor="alice@corp",
        reason="good",
        idempotency_key="key-1",
    )
    transition = first.transition

    for operation in (Event.APPROVE, Event.REJECT, Event.CANCEL):
        result = harness.respond(
            request.id,
            operation,
            expected_digest=request.action_digest,
            idempotency_key=f"other-{operation.value}",
        )
        assert result.kind is ResponseKind.ALREADY_DECIDED
        assert result.outcome is not None
        assert result.outcome.status is Status.APPROVED
        assert result.outcome.actor == "alice@corp"
        assert result.outcome.reason == "good"
        assert result.outcome.replayed is False
        assert result.transition is None
        assert harness.store.get_transition(request.id) == transition
        assert harness.store.get(request.id).version == 1


def test_cancelled_request_returns_request_cancelled(
    harness: StoreHarness,
) -> None:
    request = harness.propose().request
    harness.respond(
        request.id,
        Event.CANCEL,
        actor="agent:planner",
        reason="no longer needed",
    )

    result = harness.respond(
        request.id,
        Event.APPROVE,
        idempotency_key="late-approve",
    )

    assert result.kind is ResponseKind.CANCELLED
    assert result.outcome is not None
    assert result.outcome.status is Status.CANCELLED
    assert result.outcome.actor == "agent:planner"
    assert result.outcome.reason == "no longer needed"
    assert result.transition is None


def test_missing_request_is_classified(harness: StoreHarness) -> None:
    from approval_gate.idempotency import operation_fingerprint

    fingerprint = operation_fingerprint(
        request_id="missing",
        operation=Event.APPROVE,
        actor="alice",
        reason=None,
        expected_digest="0" * 64,
    )
    result = harness.store.respond(
        ResponseCommand(
            request_id="missing",
            operation=Event.APPROVE,
            actor="alice",
            reason=None,
            expected_digest="0" * 64,
            idempotency_key="k",
            fingerprint=fingerprint,
        ),
    )

    assert result.kind is ResponseKind.NOT_FOUND
    assert result.outcome is None
    assert result.transition is None
    assert harness.store.get("missing") is None
    assert harness.store.outcome("missing") is None
    assert harness.store.get_transition("missing") is None


def test_approval_at_one_millisecond_before_deadline_succeeds(
    harness: StoreHarness,
) -> None:
    request = harness.propose(ttl_ms=1_000).request
    harness.clock.advance_ms(999)

    result = harness.respond(request.id, Event.APPROVE)

    assert result.kind is ResponseKind.APPLIED
    assert result.outcome is not None
    assert result.outcome.at_ms == request.expires_at_ms - 1
