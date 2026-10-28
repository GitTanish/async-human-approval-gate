from __future__ import annotations

import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Iterator

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from approval_gate.clock import (
    FakeAsyncSleeper,
    FakeSyncSleeper,
    ManualClock,
    SeqIds,
)
from approval_gate.errors import (
    AlreadyDecided,
    DigestMismatch,
    IdempotencyConflict,
    RequestCancelled,
    RequestExpired,
    RequestNotFound,
)
from approval_gate.gate import ApprovalGate
from approval_gate.models import Request, Status
from approval_gate.payload import canonicalize_action, sha256_hex, thaw_json
from approval_gate.state import EXPIRY_ACTOR
from approval_gate.stores import MemoryStore, SqliteStore, Store

KINDS = [
    "propose",
    "approve",
    "reject",
    "cancel",
    "sweep",
    "advance",
    "pending",
    "read",
]
EXPECTED_ERRORS = (
    AlreadyDecided,
    DigestMismatch,
    IdempotencyConflict,
    RequestCancelled,
    RequestExpired,
    RequestNotFound,
)


@dataclass(frozen=True)
class Operation:
    kind: str
    target: int
    advance_ms: int
    correct_digest: bool
    key: str
    reason: str | None
    dedupe: str | None
    workflow: str
    ttl_ms: int


@dataclass
class Expected:
    ids: list[str] = field(default_factory=list)
    canonical: dict[str, bytes] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)
    terminal: dict[str, Status] = field(default_factory=dict)

    def record(self, request: Request) -> None:
        if request.id not in self.ids:
            self.ids.append(request.id)
        previous_canonical = self.canonical.get(request.id)
        previous_digest = self.digests.get(request.id)
        if previous_canonical is not None:
            assert request.canonical_json == previous_canonical
            assert request.action_digest == previous_digest
        self.canonical[request.id] = request.canonical_json
        self.digests[request.id] = request.action_digest


@st.composite
def operation_sequences(draw: st.DrawFn) -> list[Operation]:
    length = draw(st.integers(min_value=0, max_value=18))
    return [
        Operation(
            kind=draw(st.sampled_from(KINDS)),
            target=draw(st.integers(min_value=0, max_value=5)),
            advance_ms=draw(st.integers(min_value=0, max_value=3_000)),
            correct_digest=draw(st.booleans()),
            key=draw(st.sampled_from(["key-0", "key-1", "key-2"])),
            reason=draw(st.sampled_from([None, "reason-0", "reason-1"])),
            dedupe=draw(st.sampled_from([None, "dedupe-0", "dedupe-1"])),
            workflow=draw(st.sampled_from(["wf-0", "wf-1"])),
            ttl_ms=draw(st.integers(min_value=1, max_value=4_000)),
        )
        for _ in range(length)
    ]


@contextmanager
def open_store(backend: str, clock: ManualClock) -> Iterator[Store]:
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if backend == "memory":
        store: Store = MemoryStore(clock=clock)
    else:
        temporary = tempfile.TemporaryDirectory()
        store = SqliteStore(Path(temporary.name) / "property.sqlite3", clock=clock)
    try:
        yield store
    finally:
        store.close()
        if temporary is not None:
            temporary.cleanup()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(operations=operation_sequences())
def test_random_operation_sequences_preserve_invariants(
    backend: str,
    operations: list[Operation],
) -> None:
    clock = ManualClock(1_000_000)
    with open_store(backend, clock) as store:
        gate = ApprovalGate(
            store=store,
            clock=clock,
            ids=SeqIds(),
            sync_sleeper=FakeSyncSleeper(clock),
            async_sleeper=FakeAsyncSleeper(clock),
        )
        expected = Expected()

        for operation in operations:
            _apply(operation, gate, store, clock, expected)
            _assert_invariants(store, expected)

        _assert_final_state(gate, store, clock, expected)


def _apply(
    operation: Operation,
    gate: ApprovalGate,
    store: Store,
    clock: ManualClock,
    expected: Expected,
) -> None:
    if operation.kind == "advance":
        clock.advance_ms(operation.advance_ms)
        return

    if operation.kind == "propose":
        action = {
            "sequence": len(expected.ids),
            "nested": {"items": [1, 2]},
        }
        proposal = gate.propose(
            workflow_id=operation.workflow,
            action=action,
            summary="A generated proposal",
            requested_by="agent:planner",
            ttl=timedelta(milliseconds=operation.ttl_ms),
            dedupe_key=operation.dedupe,
        )
        if proposal.deduplicated:
            # Invariant 18: a deduplicated request is never overdue.
            assert proposal.request.expires_at_ms > clock.now_ms()
        expected.record(proposal.request)

        # Invariant 14: caller mutation cannot reach storage.
        action["sequence"] = -1
        action["nested"]["items"].append(3)
        action["mutated"] = True
        return

    if operation.kind in {"approve", "reject", "cancel"}:
        if not expected.ids:
            return
        request_id = expected.ids[operation.target % len(expected.ids)]
        before = store.get(request_id)
        assert before is not None
        before_transition = store.get_transition(request_id)
        digest = _digest_for(before, operation.correct_digest)

        try:
            if operation.kind == "approve":
                outcome = gate.approve(
                    request_id,
                    responder="alice@corp",
                    expected_digest=digest,
                    idempotency_key=operation.key,
                    reason=operation.reason,
                )
            elif operation.kind == "reject":
                outcome = gate.reject(
                    request_id,
                    responder="alice@corp",
                    expected_digest=digest,
                    idempotency_key=operation.key,
                    reason=operation.reason,
                )
            else:
                outcome = gate.cancel(
                    request_id,
                    actor="agent:planner",
                    idempotency_key=operation.key,
                    reason=operation.reason,
                )
        except EXPECTED_ERRORS:
            return

        after = store.get(request_id)
        assert after is not None
        assert after.status is outcome.status
        transition = store.get_transition(request_id)
        assert transition is not None
        if outcome.replayed:
            # Invariant 5: replay keeps version and audit history unchanged.
            assert after.version == before.version
            assert transition == before_transition
        else:
            # Invariant 9: exactly one version increment per transition.
            assert after.version == before.version + 1
            assert transition != before_transition
        return

    if operation.kind == "sweep":
        gate.sweep()
        return

    if operation.kind == "pending":
        captured_now = clock.now_ms()
        page = gate.pending(limit=50)
        # Invariant 10: pending() never returns an overdue request.
        assert all(
            request.status is Status.PENDING
            and request.expires_at_ms > captured_now
            for request in page.requests
        )
        assert page.swept >= 0
        return

    if operation.kind == "read":
        _assert_reads_are_pure(store, expected.ids)
        return

    raise AssertionError(f"unknown operation {operation.kind!r}")


def _digest_for(request: Request, correct: bool) -> str:
    if correct:
        return request.action_digest
    return "e" * 64 if request.action_digest != "e" * 64 else "f" * 64


def _snapshot(
    store: Store,
    ids: list[str],
) -> tuple[list[tuple], list[tuple]]:
    rows = [
        (
            request.id,
            request.status,
            request.version,
            request.expires_at_ms,
            request.action_digest,
        )
        for request in store.list(limit=1_000)
    ]
    transitions = [
        (request_id, store.get_transition(request_id)) for request_id in ids
    ]
    return rows, transitions


def _assert_reads_are_pure(store: Store, ids: list[str]) -> None:
    before = _snapshot(store, ids)
    for request_id in ids:
        store.get(request_id)
        store.outcome(request_id)
    store.list()
    store.list(status=Status.PENDING)
    store.list(workflow_id="wf-0")
    # Invariant 11: pure reads never write.
    assert _snapshot(store, ids) == before


def _assert_invariants(store: Store, expected: Expected) -> None:
    rows = store.list(limit=1_000)
    assert {request.id for request in rows} >= set(expected.ids)

    for request in rows:
        assert request.action_digest == expected.digests[request.id]
        # Invariant 12: the digest covers the exact stored canonical bytes.
        assert sha256_hex(request.canonical_json) == request.action_digest
        assert (
            canonicalize_action(thaw_json(request.action))
            == request.canonical_json
        )

        transition = store.get_transition(request.id)
        if request.status is Status.PENDING:
            assert request.id not in expected.terminal
            assert request.version == 0
            assert transition is None
            continue

        # Invariant 1: terminal requests never change again.
        assert request.id not in expected.terminal or (
            expected.terminal[request.id] is request.status
        )
        expected.terminal[request.id] = request.status

        # Invariants 2, 3, 9, 17: one matching terminal transition, one bump.
        assert request.version == 1
        assert transition is not None
        assert transition.request_id == request.id
        assert transition.from_status is Status.PENDING
        assert transition.to_status is request.status

        outcome = store.outcome(request.id)
        assert outcome is not None
        assert outcome.status is request.status
        assert outcome.action_digest == request.action_digest
        # Invariant 13: the transition is the source of terminal details.
        assert outcome.actor == transition.actor
        assert outcome.reason == transition.reason
        assert outcome.at_ms == transition.at_ms

        if request.status is Status.EXPIRED:
            # Invariant 7: expiry is recorded at or after the deadline.
            assert transition.at_ms >= request.expires_at_ms
            assert transition.actor == EXPIRY_ACTOR
            assert transition.idempotency_key is None
            assert transition.request_fingerprint is None
        else:
            # Invariant 6: user decisions happen strictly before the deadline.
            assert transition.at_ms < request.expires_at_ms
            assert transition.actor != EXPIRY_ACTOR
            assert transition.idempotency_key is not None
            fingerprint = transition.request_fingerprint
            assert fingerprint is not None
            assert len(fingerprint) == 64
            assert set(fingerprint) <= set("0123456789abcdef")


def _assert_final_state(
    gate: ApprovalGate,
    store: Store,
    clock: ManualClock,
    expected: Expected,
) -> None:
    captured_now = clock.now_ms()
    page = gate.pending(limit=1_000)
    assert all(
        request.expires_at_ms > captured_now for request in page.requests
    )

    for request in store.list(status=Status.APPROVED, limit=1_000):
        outcome = store.outcome(request.id)
        assert outcome is not None
        verified = gate.verified_action(outcome)
        # Invariant 16: verification returns the bytes it verified.
        assert verified.canonical_json == expected.canonical[request.id]
        assert verified.action_digest == expected.digests[request.id]
        assert canonicalize_action(thaw_json(verified.action)) == (
            verified.canonical_json
        )
