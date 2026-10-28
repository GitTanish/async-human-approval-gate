from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta
from typing import Any, Callable

import pytest

from approval_gate.clock import SeqIds
from approval_gate.errors import (
    AlreadyDecided,
    DigestMismatch,
    IdempotencyConflict,
    IntegrityError,
    InvalidRequest,
    RequestCancelled,
    RequestExpired,
    RequestNotFound,
    RequestNotApproved,
)
from approval_gate.gate import ApprovalGate
from approval_gate.models import Outcome, Status
from approval_gate.payload import MAX_ACTION_BYTES, canonicalize_action, thaw_json

from support import GateEnv


def approve(env: GateEnv, request_id: str, *, key: str = "key-1", **changes: Any):
    request = env.store.get(request_id)
    assert request is not None
    arguments = {
        "responder": "alice@corp",
        "expected_digest": request.action_digest,
        "idempotency_key": key,
        "reason": "reviewed",
    }
    arguments.update(changes)
    return env.gate.approve(request_id, **arguments)


def test_propose_returns_a_pending_request_with_a_server_digest(
    gate_env: GateEnv,
) -> None:
    proposal = gate_env.propose(
        action={"b": [1, 2], "a": True},
        summary="Deploy release 42",
        requested_by="agent:release",
        ttl=timedelta(seconds=30),
        dedupe_key="release-42",
    )

    request = proposal.request
    assert proposal.deduplicated is False
    assert request.status is Status.PENDING
    assert request.workflow_id == "wf_1"
    assert request.dedupe_key == "release-42"
    assert request.summary == "Deploy release 42"
    assert request.requested_by == "agent:release"
    assert request.created_at_ms == gate_env.clock.now_ms()
    assert request.expires_at_ms == gate_env.clock.now_ms() + 30_000
    assert request.action_digest == hashlib.sha256(
        request.canonical_json,
    ).hexdigest()
    assert thaw_json(request.action) == {"a": True, "b": [1, 2]}
    assert gate_env.gate.get(request.id) == request
    assert gate_env.gate.outcome(request.id) is None


def test_get_reports_overdue_without_changing_status(
    gate_env: GateEnv,
) -> None:
    proposal = gate_env.propose(ttl=timedelta(seconds=1))
    request_id = proposal.request.id

    assert gate_env.gate.get(request_id).overdue is False

    gate_env.clock.advance_ms(1_000)

    overdue = gate_env.gate.get(request_id)
    assert overdue is not None
    assert overdue.status is Status.PENDING
    assert overdue.overdue is True
    assert overdue.version == 0
    assert gate_env.gate.outcome(request_id) is None
    assert gate_env.gate.list()[0].overdue is True
    assert gate_env.store.get_transition(request_id) is None

    assert gate_env.gate.sweep() == 1
    expired = gate_env.gate.get(request_id)
    assert expired is not None
    assert expired.status is Status.EXPIRED
    assert expired.overdue is False


def test_list_filters_and_marks_overdue(gate_env: GateEnv) -> None:
    first = gate_env.propose(workflow_id="wf_a").request
    gate_env.clock.advance_ms(1)
    second = gate_env.propose(workflow_id="wf_b").request
    gate_env.clock.advance_ms(1)
    third = gate_env.propose(workflow_id="wf_a").request

    approve(gate_env, third.id)

    assert [request.id for request in gate_env.gate.list()] == [
        third.id,
        second.id,
        first.id,
    ]
    assert [request.id for request in gate_env.gate.list(workflow_id="wf_a")] == [
        third.id,
        first.id,
    ]
    assert [
        request.id
        for request in gate_env.gate.list(status=Status.APPROVED)
    ] == [third.id]
    assert [
        request.id
        for request in gate_env.gate.list(status="pending")
    ] == [second.id, first.id]
    assert [request.id for request in gate_env.gate.list(limit=1)] == [
        third.id,
    ]


def test_pending_sweeps_then_returns_only_unexpired_rows(
    gate_env: GateEnv,
) -> None:
    first = gate_env.propose(ttl=timedelta(seconds=1)).request
    gate_env.clock.advance_ms(500)
    second = gate_env.propose(ttl=timedelta(seconds=1)).request
    gate_env.clock.advance_ms(500)

    page = gate_env.gate.pending()

    assert page.swept == 1
    assert [request.id for request in page.requests] == [second.id]
    assert gate_env.store.get(first.id).status is Status.EXPIRED
    assert gate_env.store.get(second.id).status is Status.PENDING
    assert gate_env.gate.pending().swept == 0


def test_pending_filters_by_workflow_and_limit(gate_env: GateEnv) -> None:
    gate_env.clock.advance_ms(0)
    first = gate_env.propose(workflow_id="wf_a").request
    gate_env.clock.advance_ms(1)
    second = gate_env.propose(workflow_id="wf_b").request
    gate_env.clock.advance_ms(1)
    third = gate_env.propose(workflow_id="wf_a").request

    page = gate_env.gate.pending(workflow_id="wf_a")
    assert [request.id for request in page.requests] == [third.id, first.id]
    assert page.swept == 0

    limited = gate_env.gate.pending(limit=1)
    assert [request.id for request in limited.requests] == [third.id]
    assert second.id in {
        request.id for request in gate_env.gate.pending().requests
    }


def test_propose_dedupe_and_rededupe_after_expiry(gate_env: GateEnv) -> None:
    first = gate_env.propose(
        action={"v": 1},
        dedupe_key="same",
        ttl=timedelta(seconds=1),
    )
    second = gate_env.propose(
        action={"v": 2},
        dedupe_key="same",
        ttl=timedelta(seconds=1),
    )
    assert second.deduplicated is True
    assert second.request.id == first.request.id
    assert thaw_json(second.request.action) == {"v": 1}

    gate_env.clock.advance_ms(1_000)
    third = gate_env.propose(
        action={"v": 3},
        dedupe_key="same",
        ttl=timedelta(seconds=5),
    )

    assert third.deduplicated is False
    assert third.request.id != first.request.id
    old_outcome = gate_env.gate.outcome(first.request.id)
    assert old_outcome is not None
    assert old_outcome.status is Status.EXPIRED
    assert old_outcome.actor == "system:expiry"


def test_approve_reject_and_cancel_return_terminal_outcomes(
    gate_env: GateEnv,
) -> None:
    first = gate_env.propose().request
    approved = approve(gate_env, first.id, key="a")
    assert approved.status is Status.APPROVED
    assert approved.actor == "alice@corp"
    assert approved.reason == "reviewed"
    assert approved.at_ms == gate_env.clock.now_ms()
    assert approved.replayed is False
    assert gate_env.gate.get(first.id).version == 1

    second = gate_env.propose().request
    rejected = gate_env.gate.reject(
        second.id,
        responder="bob@corp",
        expected_digest=second.action_digest,
        idempotency_key="r",
        reason="copy is wrong",
    )
    assert rejected.status is Status.REJECTED
    assert rejected.actor == "bob@corp"
    assert rejected.reason == "copy is wrong"

    third = gate_env.propose().request
    cancelled = gate_env.gate.cancel(
        third.id,
        actor="agent:planner",
        idempotency_key="c",
        reason="superseded",
    )
    assert cancelled.status is Status.CANCELLED
    assert cancelled.actor == "agent:planner"
    assert cancelled.reason == "superseded"


def test_replay_returns_replayed_outcome_without_new_transition(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose().request
    first = approve(gate_env, request.id, key="same")
    transition = gate_env.store.get_transition(request.id)

    replay = approve(gate_env, request.id, key="same")

    assert replay.replayed is True
    assert replay.status is Status.APPROVED
    assert replay.actor == first.actor
    assert replay.at_ms == first.at_ms
    assert gate_env.store.get_transition(request.id) == transition
    assert gate_env.gate.get(request.id).version == 1


def test_idempotency_conflict_uses_the_same_key(gate_env: GateEnv) -> None:
    request = gate_env.propose().request
    approve(gate_env, request.id, key="same", reason="first")

    with pytest.raises(IdempotencyConflict):
        approve(gate_env, request.id, key="same", reason="changed")

    assert gate_env.gate.get(request.id).version == 1


def test_already_decided_carries_the_current_outcome(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose().request
    approve(gate_env, request.id, key="a")

    with pytest.raises(AlreadyDecided) as raised:
        gate_env.gate.reject(
            request.id,
            responder="bob@corp",
            expected_digest=request.action_digest,
            idempotency_key="b",
            reason="no",
        )

    assert raised.value.outcome.status is Status.APPROVED
    assert raised.value.outcome.actor == "alice@corp"


def test_expired_and_cancelled_errors_carry_outcomes(
    gate_env: GateEnv,
) -> None:
    expired_request = gate_env.propose(ttl=timedelta(seconds=1)).request
    gate_env.clock.advance_ms(1_000)
    with pytest.raises(RequestExpired) as expired:
        approve(gate_env, expired_request.id, key="expired")
    assert expired.value.outcome.status is Status.EXPIRED
    assert expired.value.outcome.actor == "system:expiry"

    cancelled_request = gate_env.propose().request
    gate_env.gate.cancel(
        cancelled_request.id,
        actor="agent:planner",
        idempotency_key="cancel",
    )
    with pytest.raises(RequestCancelled) as cancelled:
        approve(gate_env, cancelled_request.id, key="late")
    assert cancelled.value.outcome.status is Status.CANCELLED
    assert cancelled.value.outcome.actor == "agent:planner"


def test_digest_mismatch_and_missing_request(gate_env: GateEnv) -> None:
    request = gate_env.propose().request
    wrong = "f" * 64
    if wrong == request.action_digest:
        wrong = "e" * 64

    with pytest.raises(DigestMismatch):
        gate_env.gate.approve(
            request.id,
            responder="alice@corp",
            expected_digest=wrong,
            idempotency_key="wrong",
        )
    assert gate_env.gate.get(request.id).status is Status.PENDING

    with pytest.raises(RequestNotFound):
        gate_env.gate.approve(
            "missing",
            responder="alice@corp",
            expected_digest="0" * 64,
            idempotency_key="missing",
        )


def test_failed_digest_attempt_does_not_burn_the_key(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose().request
    with pytest.raises(DigestMismatch):
        gate_env.gate.approve(
            request.id,
            responder="alice@corp",
            expected_digest="0" * 64,
            idempotency_key="retry",
        )

    outcome = gate_env.gate.approve(
        request.id,
        responder="alice@corp",
        expected_digest=request.action_digest,
        idempotency_key="retry",
    )
    assert outcome.status is Status.APPROVED


def test_mutating_the_input_after_propose_has_no_effect(
    gate_env: GateEnv,
) -> None:
    action = {"nested": {"value": 1}, "items": [1, 2]}
    proposal = gate_env.propose(action=action)
    request_id = proposal.request.id
    canonical = proposal.request.canonical_json
    digest = proposal.request.action_digest

    action["nested"]["value"] = 999
    action["new"] = True
    action["items"].append(3)

    stored = gate_env.gate.get(request_id)
    assert stored is not None
    assert thaw_json(stored.action) == {
        "nested": {"value": 1},
        "items": [1, 2],
    }
    assert stored.canonical_json == canonical
    assert stored.action_digest == digest

    outcome = approve(gate_env, request_id)
    verified = gate_env.gate.verified_action(outcome)
    assert verified.canonical_json == canonical
    assert verified.action_digest == digest
    assert thaw_json(verified.action) == {
        "nested": {"value": 1},
        "items": [1, 2],
    }


def test_verified_action_returns_immutable_verified_bytes(
    gate_env: GateEnv,
) -> None:
    proposal = gate_env.propose(action={"tool": "send_email", "to": "a@b.c"})
    outcome = approve(gate_env, proposal.request.id)

    verified = gate_env.gate.verified_action(outcome)
    request = gate_env.gate.get(proposal.request.id)
    assert request is not None
    assert verified.request_id == request.id
    assert verified.canonical_json == request.canonical_json
    assert verified.action_digest == request.action_digest
    assert thaw_json(verified.action) == {
        "to": "a@b.c",
        "tool": "send_email",
    }
    with pytest.raises(TypeError):
        verified.action["tool"] = "delete_database"  # type: ignore[index]


def test_verified_action_rejects_non_approved_outcomes(
    gate_env: GateEnv,
) -> None:
    rejected_request = gate_env.propose().request
    rejected = gate_env.gate.reject(
        rejected_request.id,
        responder="bob@corp",
        expected_digest=rejected_request.action_digest,
        idempotency_key="reject",
        reason="no",
    )

    with pytest.raises(RequestNotApproved):
        gate_env.gate.verified_action(rejected)
    with pytest.raises(RequestNotApproved):
        gate_env.gate.verified_action(
            replace(rejected, status=Status.PENDING),
        )

    fake_approval = Outcome(
        request_id=rejected.request_id,
        status=Status.APPROVED,
        action_digest=rejected.action_digest,
        actor="mallory",
        reason=None,
        at_ms=gate_env.clock.now_ms(),
    )
    with pytest.raises(RequestNotApproved):
        gate_env.gate.verified_action(fake_approval)


def test_verified_action_rejects_mismatched_outcome_digest(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose().request
    outcome = approve(gate_env, request.id)

    with pytest.raises(IntegrityError):
        gate_env.gate.verified_action(
            replace(outcome, action_digest="0" * 64),
        )


def test_verified_action_missing_request(gate_env: GateEnv) -> None:
    fake = Outcome(
        request_id="missing",
        status=Status.APPROVED,
        action_digest="0" * 64,
        actor="alice",
        reason=None,
        at_ms=0,
    )
    with pytest.raises(RequestNotFound):
        gate_env.gate.verified_action(fake)


def test_verified_action_detects_corrupted_canonical_bytes(
    gate_env: GateEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = gate_env.propose()
    outcome = approve(gate_env, proposal.request.id)
    stored = gate_env.store.get(proposal.request.id)
    assert stored is not None

    noncanonical = b'{"tampered": true}'
    tampered = replace(
        stored,
        canonical_json=noncanonical,
        action_digest=hashlib.sha256(noncanonical).hexdigest(),
    )
    real_get = gate_env.store.get
    monkeypatch.setattr(
        gate_env.store,
        "get",
        lambda request_id: (
            tampered if request_id == stored.id else real_get(request_id)
        ),
    )

    with pytest.raises(IntegrityError):
        gate_env.gate.verified_action(
            replace(outcome, action_digest=tampered.action_digest),
        )


def test_on_transition_calls_only_after_applied_commits(
    gate_env: GateEnv,
) -> None:
    seen: list[tuple[str, Status]] = []

    def callback(transition) -> None:
        request = gate_env.store.get(transition.request_id)
        assert request is not None
        seen.append((transition.to_status.value, request.status))

    gate = ApprovalGate(
        store=gate_env.store,
        clock=gate_env.clock,
        ids=SeqIds(),
        sync_sleeper=gate_env.sync_sleeper,
        async_sleeper=gate_env.async_sleeper,
        on_transition=callback,
    )
    proposal = gate.propose(
        workflow_id="wf",
        action={},
        summary="summary",
        requested_by="agent",
        ttl=timedelta(seconds=1),
    )
    assert seen == []

    outcome = gate.approve(
        proposal.request.id,
        responder="alice",
        expected_digest=proposal.request.action_digest,
        idempotency_key="key",
    )
    assert outcome.status is Status.APPROVED
    assert seen == [("approved", Status.APPROVED)]

    gate.approve(
        proposal.request.id,
        responder="alice",
        expected_digest=proposal.request.action_digest,
        idempotency_key="key",
    )
    assert seen == [("approved", Status.APPROVED)]

    pending = gate.propose(
        workflow_id="wf",
        action={},
        summary="summary",
        requested_by="agent",
        ttl=timedelta(seconds=1),
        dedupe_key="dedupe",
    )
    gate_env.clock.advance_ms(1_000)
    gate.propose(
        workflow_id="wf",
        action={},
        summary="summary",
        requested_by="agent",
        ttl=timedelta(seconds=1),
        dedupe_key="dedupe",
    )
    assert seen[-1] == ("expired", Status.EXPIRED)
    assert pending.request.id != proposal.request.id


def test_on_transition_failure_is_swallowed(
    gate_env: GateEnv,
) -> None:
    def callback(_transition) -> None:
        raise RuntimeError("notifications are down")

    gate = ApprovalGate(
        store=gate_env.store,
        clock=gate_env.clock,
        ids=SeqIds(),
        sync_sleeper=gate_env.sync_sleeper,
        async_sleeper=gate_env.async_sleeper,
        on_transition=callback,
    )
    proposal = gate.propose(
        workflow_id="wf",
        action={},
        summary="summary",
        requested_by="agent",
        ttl=timedelta(seconds=60),
    )

    outcome = gate.approve(
        proposal.request.id,
        responder="alice",
        expected_digest=proposal.request.action_digest,
        idempotency_key="key",
    )

    assert outcome.status is Status.APPROVED
    stored = gate_env.store.get(proposal.request.id)
    assert stored is not None
    assert stored.status is Status.APPROVED


@pytest.mark.parametrize(
    "call",
    [
        lambda gate: gate.propose(
            workflow_id="",
            action={},
            summary="s",
            requested_by="agent",
            ttl=timedelta(seconds=1),
        ),
        lambda gate: gate.propose(
            workflow_id="wf",
            action={},
            summary="",
            requested_by="agent",
            ttl=timedelta(seconds=1),
        ),
        lambda gate: gate.propose(
            workflow_id="wf",
            action={},
            summary="s",
            requested_by="",
            ttl=timedelta(seconds=1),
        ),
        lambda gate: gate.propose(
            workflow_id="wf",
            action={},
            summary="s",
            requested_by="agent",
            ttl=timedelta(seconds=1),
            dedupe_key="",
        ),
        lambda gate: gate.propose(
            workflow_id="wf",
            action={},
            summary="s",
            requested_by="agent",
            ttl=timedelta(0),
        ),
        lambda gate: gate.propose(
            workflow_id="wf",
            action={},
            summary="s",
            requested_by="agent",
            ttl=timedelta(seconds=-1),
        ),
        lambda gate: gate.propose(
            workflow_id="wf",
            action={},
            summary="s",
            requested_by="agent",
            ttl=60,  # type: ignore[arg-type]
        ),
        lambda gate: gate.propose(
            workflow_id="wf",
            action=[1, 2],  # type: ignore[arg-type]
            summary="s",
            requested_by="agent",
            ttl=timedelta(seconds=1),
        ),
        lambda gate: gate.propose(
            workflow_id="wf",
            action={"s": "a" * MAX_ACTION_BYTES},
            summary="s",
            requested_by="agent",
            ttl=timedelta(seconds=1),
        ),
        lambda gate: gate.propose(
            workflow_id="wf",
            action={},
            summary="s" * 2_001,
            requested_by="agent",
            ttl=timedelta(seconds=1),
        ),
    ],
    ids=[
        "empty-workflow",
        "empty-summary",
        "empty-requester",
        "empty-dedupe-key",
        "zero-ttl",
        "negative-ttl",
        "ttl-not-timedelta",
        "action-not-object",
        "action-too-large",
        "summary-too-long",
    ],
)
def test_propose_rejects_invalid_inputs(
    gate_env: GateEnv,
    call: Callable[[ApprovalGate], object],
) -> None:
    with pytest.raises(InvalidRequest):
        call(gate_env.gate)
    assert gate_env.store.list() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"responder": ""},
        {"expected_digest": "0" * 63},
        {"expected_digest": "0" * 65},
        {"expected_digest": "A" * 64},
        {"expected_digest": "not-a-digest"},
        {"idempotency_key": ""},
        {"idempotency_key": "k" * 201},
        {"reason": "r" * 2_001},
    ],
)
def test_approve_rejects_invalid_inputs(
    gate_env: GateEnv,
    changes: dict[str, str],
) -> None:
    request = gate_env.propose().request
    arguments: dict[str, Any] = {
        "responder": "alice",
        "expected_digest": request.action_digest,
        "idempotency_key": "key",
    }
    arguments.update(changes)

    with pytest.raises(InvalidRequest):
        gate_env.gate.approve(request.id, **arguments)
    assert gate_env.gate.get(request.id).status is Status.PENDING


def test_cancel_rejects_invalid_inputs(gate_env: GateEnv) -> None:
    request = gate_env.propose().request
    with pytest.raises(InvalidRequest):
        gate_env.gate.cancel(
            request.id,
            actor="",
            idempotency_key="key",
        )
    with pytest.raises(InvalidRequest):
        gate_env.gate.cancel(
            request.id,
            actor="agent",
            idempotency_key="",
        )
    with pytest.raises(InvalidRequest):
        gate_env.gate.cancel(
            request.id,
            actor="agent",
            idempotency_key="key",
            reason="r" * 2_001,
        )


def test_read_and_wait_arguments_are_validated(gate_env: GateEnv) -> None:
    with pytest.raises(InvalidRequest):
        gate_env.gate.list(status="unknown")
    with pytest.raises(InvalidRequest):
        gate_env.gate.list(limit=-1)
    with pytest.raises(InvalidRequest):
        gate_env.gate.pending(limit=-1)
    with pytest.raises(InvalidRequest):
        gate_env.gate.sweep(request_id="")
    with pytest.raises(InvalidRequest):
        gate_env.gate.wait("any", timeout=-0.1)
    with pytest.raises(InvalidRequest):
        gate_env.gate.wait("any", poll_interval=0)
    with pytest.raises(InvalidRequest):
        gate_env.gate.wait("any", poll_interval=float("inf"))
