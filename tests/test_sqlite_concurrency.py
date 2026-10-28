from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from approval_gate.clock import ManualClock
from approval_gate.idempotency import operation_fingerprint
from approval_gate.models import Status
from approval_gate.stores import (
    ResponseCommand,
    ResponseKind,
    SqliteStore,
)
from approval_gate.state import Event

from support import StoreHarness


@pytest.fixture
def sqlite_harness(tmp_path) -> StoreHarness:
    clock = ManualClock(1_000_000)
    store = SqliteStore(tmp_path / "concurrency.sqlite3", clock=clock)
    try:
        yield StoreHarness(store=store, clock=clock)
    finally:
        store.close()


def test_twenty_threads_approve_and_reject_exactly_one_wins(
    sqlite_harness: StoreHarness,
) -> None:
    request = sqlite_harness.propose(ttl_ms=3_600_000).request
    barrier = Barrier(20)

    def worker(index: int):
        barrier.wait()
        return sqlite_harness.respond(
            request.id,
            Event.APPROVE if index % 2 == 0 else Event.REJECT,
            actor=f"user-{index}@corp",
            reason=f"decision-{index}",
            idempotency_key=f"key-{index}",
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(worker, range(20)))

    kinds = Counter(result.kind for result in results)
    assert kinds[ResponseKind.APPLIED] == 1
    assert kinds[ResponseKind.ALREADY_DECIDED] == 19

    winner = next(
        result for result in results if result.kind is ResponseKind.APPLIED
    )
    stored = sqlite_harness.store.get(request.id)
    assert stored is not None
    assert stored.status is winner.outcome.status
    assert stored.version == 1
    transition = sqlite_harness.store.get_transition(request.id)
    assert transition is not None
    assert transition.to_status is stored.status
    assert all(
        result.outcome is not None
        and result.outcome.status is stored.status
        for result in results
    )


def test_same_key_concurrently_applies_once_and_replays(
    sqlite_harness: StoreHarness,
) -> None:
    request = sqlite_harness.propose(ttl_ms=3_600_000).request
    fingerprint = operation_fingerprint(
        request_id=request.id,
        operation=Event.APPROVE,
        actor="alice@corp",
        reason="reviewed",
        expected_digest=request.action_digest,
    )
    command = ResponseCommand(
        request_id=request.id,
        operation=Event.APPROVE,
        actor="alice@corp",
        reason="reviewed",
        expected_digest=request.action_digest,
        idempotency_key="shared-key",
        fingerprint=fingerprint,
    )
    barrier = Barrier(20)

    def worker(_index: int):
        barrier.wait()
        return sqlite_harness.store.respond(command)

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(worker, range(20)))

    kinds = Counter(result.kind for result in results)
    assert kinds[ResponseKind.APPLIED] == 1
    assert kinds[ResponseKind.REPLAYED] == 19
    assert sqlite_harness.store.get(request.id).version == 1
    assert sqlite_harness.store.get_transition(request.id) is not None
    for result in results:
        assert result.outcome is not None
        assert result.outcome.status is Status.APPROVED
        assert result.outcome.replayed is (
            result.kind is ResponseKind.REPLAYED
        )


def test_concurrent_propose_with_the_same_dedupe_key_creates_one_request(
    sqlite_harness: StoreHarness,
) -> None:
    barrier = Barrier(20)

    def worker(index: int):
        barrier.wait()
        return sqlite_harness.propose(
            action={"attempt": index},
            workflow_id="wf_shared",
            dedupe_key="shared",
            request_id=f"req_attempt_{index}",
            ttl_ms=3_600_000,
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(worker, range(20)))

    assert sum(not result.deduplicated for result in results) == 1
    assert sum(result.deduplicated for result in results) == 19
    assert len({result.request.id for result in results}) == 1
    pending = sqlite_harness.store.list(
        status=Status.PENDING,
        workflow_id="wf_shared",
    )
    assert len(pending) == 1


def test_approve_races_sweep_at_the_exact_deadline(
    sqlite_harness: StoreHarness,
) -> None:
    request = sqlite_harness.propose(ttl_ms=1_000).request
    sqlite_harness.clock.advance_ms(1_000)
    barrier = Barrier(20)

    def worker(index: int):
        barrier.wait()
        if index % 2 == 0:
            return (
                "response",
                sqlite_harness.respond(
                    request.id,
                    Event.APPROVE,
                    idempotency_key=f"key-{index}",
                ),
            )
        return "sweep", sqlite_harness.store.sweep()

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(worker, range(20)))

    responses = [
        result for kind, result in results if kind == "response"
    ]
    sweeps = [result for kind, result in results if kind == "sweep"]

    assert all(
        response.kind is ResponseKind.EXPIRED for response in responses
    )
    transitions = sum(
        response.transition is not None for response in responses
    ) + sum(sweep.count for sweep in sweeps)
    assert transitions == 1

    stored = sqlite_harness.store.get(request.id)
    assert stored is not None
    assert stored.status is Status.EXPIRED
    assert stored.version == 1
    assert sqlite_harness.store.get_transition(request.id).to_status is (
        Status.EXPIRED
    )


def test_reopening_the_database_preserves_pending_state(tmp_path) -> None:
    path = tmp_path / "restart.sqlite3"
    clock = ManualClock(1_000_000)
    first = SqliteStore(path, clock=clock)
    harness = StoreHarness(store=first, clock=clock)
    request = harness.propose(ttl_ms=60_000).request
    first.close()

    second = SqliteStore(path, clock=clock)
    try:
        loaded = second.get(request.id)
        assert loaded is not None
        assert loaded.canonical_json == request.canonical_json
        assert loaded.status is Status.PENDING
    finally:
        second.close()


def test_separate_processes_propose_respond_and_read(tmp_path) -> None:
    path = tmp_path / "process.sqlite3"
    propose_script = """
import json
import sys
from approval_gate.clock import ManualClock
from approval_gate.payload import canonicalize_action, sha256_hex
from approval_gate.stores import NewRequest, SqliteStore

path = sys.argv[1]
canonical = canonicalize_action({"from": "child-process"})
store = SqliteStore(path, clock=ManualClock(1_000_000))
result = store.propose(NewRequest(
    id="req_child",
    workflow_id="wf_process",
    dedupe_key=None,
    canonical_json=canonical,
    action_digest=sha256_hex(canonical),
    summary="Child proposal",
    requested_by="child-agent",
    ttl_ms=3_600_000,
))
print(json.dumps({
    "id": result.request.id,
    "digest": result.request.action_digest,
    "created_at_ms": result.request.created_at_ms,
}))
store.close()
"""
    read_script = """
import sys
from approval_gate.clock import ManualClock
from approval_gate.stores import SqliteStore

store = SqliteStore(sys.argv[1], clock=ManualClock(1_000_000))
outcome = store.outcome("req_child")
print("none" if outcome is None else outcome.status.value)
store.close()
"""

    proposed = subprocess.run(
        [sys.executable, "-c", propose_script, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    payload = json.loads(proposed.stdout.strip().splitlines()[-1])
    assert payload["id"] == "req_child"

    store = SqliteStore(path, clock=ManualClock(1_000_000))
    try:
        request = store.get(payload["id"])
        assert request is not None
        assert store.outcome(payload["id"]) is None

        fingerprint = operation_fingerprint(
            request_id=request.id,
            operation=Event.APPROVE,
            actor="parent-approver",
            reason="cross-process",
            expected_digest=request.action_digest,
        )
        result = store.respond(
            ResponseCommand(
                request_id=request.id,
                operation=Event.APPROVE,
                actor="parent-approver",
                reason="cross-process",
                expected_digest=request.action_digest,
                idempotency_key="parent-key",
                fingerprint=fingerprint,
            ),
        )
        assert result.kind is ResponseKind.APPLIED
        assert store.outcome(request.id).status is Status.APPROVED
    finally:
        store.close()

    read = subprocess.run(
        [sys.executable, "-c", read_script, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert read.stdout.strip().splitlines()[-1] == "approved"
