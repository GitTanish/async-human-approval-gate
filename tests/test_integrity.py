from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from approval_gate.clock import (
    FakeAsyncSleeper,
    FakeSyncSleeper,
    ManualClock,
    SeqIds,
)
from approval_gate.errors import IntegrityError
from approval_gate.gate import ApprovalGate
from approval_gate.stores import SqliteStore
from approval_gate.state import Event

from support import StoreHarness


@pytest.fixture
def corruptible(tmp_path):
    clock = ManualClock(1_000_000)
    path = tmp_path / "integrity.sqlite3"
    store = SqliteStore(path, clock=clock)
    gate = ApprovalGate(
        store=store,
        clock=clock,
        ids=SeqIds(),
        sync_sleeper=FakeSyncSleeper(clock),
        async_sleeper=FakeAsyncSleeper(clock),
    )
    try:
        yield store, gate, path, StoreHarness(store=store, clock=clock)
    finally:
        store.close()


@contextmanager
def raw_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA recursive_triggers = ON")
        yield connection
    finally:
        connection.close()


def tamper_request(
    path: Path,
    request_id: str,
    *,
    action_json: str | None = None,
    action_digest: str | None = None,
) -> None:
    assignments: list[str] = []
    parameters: list[object] = []
    if action_json is not None:
        assignments.append("action_json = ?")
        parameters.append(action_json)
    if action_digest is not None:
        assignments.append("action_digest = ?")
        parameters.append(action_digest)
    assert assignments

    with raw_connection(path) as connection:
        connection.execute("DROP TRIGGER requests_immutable_fields")
        connection.execute(
            f"UPDATE requests SET {', '.join(assignments)} WHERE id = ?",
            (*parameters, request_id),
        )


def test_corrupted_action_bytes_are_detected_on_read(
    corruptible,
) -> None:
    store, _gate, path, harness = corruptible
    request = harness.propose(action={"tool": "send_email"}).request
    harness.respond(request.id, Event.APPROVE)

    tamper_request(path, request.id, action_json='{"tool":"wire_money"}')

    with pytest.raises(IntegrityError, match="digest"):
        store.get(request.id)
    with pytest.raises(IntegrityError, match="digest"):
        store.outcome(request.id)
    with pytest.raises(IntegrityError, match="digest"):
        store.list()


def test_corrupted_digest_is_detected_on_read(corruptible) -> None:
    store, _gate, path, harness = corruptible
    request = harness.propose().request

    tamper_request(path, request.id, action_digest="a" * 64)

    with pytest.raises(IntegrityError, match="digest"):
        store.get(request.id)


def test_noncanonical_bytes_with_a_matching_digest_are_detected(
    corruptible,
) -> None:
    store, _gate, path, harness = corruptible
    request = harness.propose().request

    noncanonical = '{"tool": "send_email"}'
    digest = hashlib.sha256(noncanonical.encode("utf-8")).hexdigest()
    tamper_request(
        path,
        request.id,
        action_json=noncanonical,
        action_digest=digest,
    )

    with pytest.raises(IntegrityError, match="canonical"):
        store.get(request.id)


def test_verified_action_rejects_database_tampering(corruptible) -> None:
    store, gate, path, harness = corruptible
    request = harness.propose(action={"tool": "send_email"}).request
    harness.respond(request.id, Event.APPROVE)
    outcome = store.outcome(request.id)
    assert outcome is not None

    tamper_request(path, request.id, action_json='{"tool":"wire_money"}')

    with pytest.raises(IntegrityError, match="digest"):
        gate.verified_action(outcome)
