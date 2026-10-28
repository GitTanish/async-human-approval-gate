from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from approval_gate.clock import ManualClock
from approval_gate.stores import SqliteStore
from approval_gate.state import Event

from support import StoreHarness


@pytest.fixture
def trigger_db(tmp_path) -> tuple[StoreHarness, Path]:
    clock = ManualClock(1_000_000)
    path = tmp_path / "triggers.sqlite3"
    store = SqliteStore(path, clock=clock)
    try:
        yield StoreHarness(store=store, clock=clock), path
    finally:
        store.close()


@contextmanager
def raw_connection(path: Path):
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    try:
        yield connection
    finally:
        connection.close()


def assert_aborts(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
    match: str | None = None,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=match):
        connection.execute(sql, parameters)


def test_transitions_reject_update_and_delete(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request
    harness.respond(request.id, Event.APPROVE)

    with raw_connection(path) as connection:
        assert_aborts(
            connection,
            "UPDATE transitions SET actor = 'mallory' WHERE request_id = ?",
            (request.id,),
            "append-only",
        )
        assert_aborts(
            connection,
            "DELETE FROM transitions WHERE request_id = ?",
            (request.id,),
            "append-only",
        )


def test_transition_cannot_be_replaced(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request
    harness.respond(request.id, Event.APPROVE)

    with raw_connection(path) as connection:
        row = connection.execute(
            "SELECT * FROM transitions WHERE request_id = ?",
            (request.id,),
        ).fetchone()
        assert row is not None
        assert_aborts(
            connection,
            """
            INSERT OR REPLACE INTO transitions (
                seq, request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, ?, 'pending', 'rejected', 'mallory', NULL, ?, 'k2', ?)
            """,
            (
                row["seq"],
                request.id,
                harness.clock.now_ms(),
                "b" * 64,
            ),
        )
        still = connection.execute(
            "SELECT * FROM transitions WHERE request_id = ?",
            (request.id,),
        ).fetchone()
        assert still is not None
        assert still["to_status"] == "approved"
        assert still["actor"] == "alice@corp"


def test_requests_cannot_be_deleted(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        assert_aborts(
            connection,
            "DELETE FROM requests WHERE id = ?",
            (request.id,),
            "cannot be deleted",
        )


def test_requests_cannot_be_replaced_by_id(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        assert_aborts(
            connection,
            """
            INSERT OR REPLACE INTO requests (
                id, workflow_id, dedupe_key, status, action_json,
                action_digest, summary, requested_by, created_at_ms,
                expires_at_ms, version
            )
            SELECT id, workflow_id, dedupe_key, status, action_json,
                   action_digest, summary, requested_by, created_at_ms,
                   expires_at_ms, version
            FROM requests WHERE id = ?
            """,
            (request.id,),
            "cannot be replaced",
        )


def test_request_replacement_cannot_bypass_pending_dedupe_uniqueness(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose(dedupe_key="same").request

    with raw_connection(path) as connection:
        assert_aborts(
            connection,
            """
            INSERT OR REPLACE INTO requests (
                id, workflow_id, dedupe_key, status, action_json,
                action_digest, summary, requested_by, created_at_ms,
                expires_at_ms, version
            )
            SELECT 'req_other', workflow_id, dedupe_key, status, action_json,
                   action_digest, summary, requested_by, created_at_ms,
                   expires_at_ms, version
            FROM requests WHERE id = ?
            """,
            (request.id,),
        )


@pytest.mark.parametrize(
    ("column", "expression"),
    [
        ("id", "'req_other'"),
        ("workflow_id", "'wf_other'"),
        ("dedupe_key", "'other'"),
        ("action_json", "'{\"tampered\":true}'"),
        ("action_digest", "'a' * 64"),
        ("summary", "'tampered'"),
        ("requested_by", "'mallory'"),
        ("created_at_ms", "created_at_ms + 1"),
        ("expires_at_ms", "expires_at_ms + 1"),
    ],
)
def test_immutable_request_fields_reject_updates(
    trigger_db: tuple[StoreHarness, Path],
    column: str,
    expression: str,
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        assert_aborts(
            connection,
            f"UPDATE requests SET {column} = {expression} WHERE id = ?",
            (request.id,),
            "immutable request field",
        )


def test_status_update_requires_exactly_one_version_increment(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        assert_aborts(
            connection,
            "UPDATE requests SET status = 'approved' WHERE id = ?",
            (request.id,),
            "invalid request status transition",
        )
        assert_aborts(
            connection,
            """
            UPDATE requests SET status = 'approved', version = version + 2
            WHERE id = ?
            """,
            (request.id,),
            "invalid request status transition",
        )
        assert_aborts(
            connection,
            """
            UPDATE requests SET status = 'pending', version = version + 1
            WHERE id = ?
            """,
            (request.id,),
        )


def test_version_cannot_change_without_a_status_change(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        assert_aborts(
            connection,
            "UPDATE requests SET version = version + 1 WHERE id = ?",
            (request.id,),
            "version changes only with a status transition",
        )


def test_transition_insert_requires_matching_request_status(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        assert_aborts(
            connection,
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'pending', 'approved', 'alice@corp', NULL, ?,
                      'key-1', ?)
            """,
            (request.id, harness.clock.now_ms(), "a" * 64),
            "request status must match transition",
        )


@pytest.mark.parametrize("at_offset", [0, 1])
def test_direct_user_transition_at_or_after_deadline_aborts(
    trigger_db: tuple[StoreHarness, Path],
    at_offset: int,
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        connection.execute(
            """
            UPDATE requests SET status = 'approved', version = version + 1
            WHERE id = ?
            """,
            (request.id,),
        )
        assert_aborts(
            connection,
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'pending', 'approved', 'alice@corp', NULL, ?,
                      'key-1', ?)
            """,
            (
                request.id,
                request.expires_at_ms + at_offset,
                "a" * 64,
            ),
            "at or after deadline",
        )


def test_direct_expiry_before_deadline_aborts(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        connection.execute(
            """
            UPDATE requests SET status = 'expired', version = version + 1
            WHERE id = ?
            """,
            (request.id,),
        )
        assert_aborts(
            connection,
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'pending', 'expired', 'system:expiry',
                      'deadline_reached', ?, NULL, NULL)
            """,
            (request.id, request.expires_at_ms - 1),
            "expiry before deadline",
        )


def test_direct_expiry_must_be_system_generated(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        connection.execute(
            """
            UPDATE requests SET status = 'expired', version = version + 1
            WHERE id = ?
            """,
            (request.id,),
        )
        assert_aborts(
            connection,
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'pending', 'expired', 'alice@corp',
                      'deadline_reached', ?, NULL, NULL)
            """,
            (request.id, request.expires_at_ms),
            "expiry must be system-generated",
        )
        assert_aborts(
            connection,
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'pending', 'expired', 'system:expiry',
                      'deadline_reached', ?, 'key-1', ?)
            """,
            (request.id, request.expires_at_ms, "a" * 64),
        )


def test_valid_direct_expiry_transition_is_accepted(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        connection.execute(
            """
            UPDATE requests SET status = 'expired', version = version + 1
            WHERE id = ?
            """,
            (request.id,),
        )
        connection.execute(
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'pending', 'expired', 'system:expiry',
                      'deadline_reached', ?, NULL, NULL)
            """,
            (request.id, request.expires_at_ms),
        )
        assert (
            connection.execute(
                "SELECT to_status FROM transitions WHERE request_id = ?",
                (request.id,),
            ).fetchone()["to_status"]
            == "expired"
        )


@pytest.mark.parametrize(
    ("key", "fingerprint"),
    [
        (None, "a" * 64),
        ("key-1", None),
        ("key-1", "a" * 63),
        ("key-1", "A" * 64),
        ("k" * 201, "a" * 64),
    ],
)
def test_invalid_user_transition_evidence_aborts(
    trigger_db: tuple[StoreHarness, Path],
    key: str | None,
    fingerprint: str | None,
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        connection.execute(
            """
            UPDATE requests SET status = 'approved', version = version + 1
            WHERE id = ?
            """,
            (request.id,),
        )
        assert_aborts(
            connection,
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'pending', 'approved', 'alice@corp', NULL, ?, ?, ?)
            """,
            (request.id, harness.clock.now_ms(), key, fingerprint),
        )


def test_user_transition_cannot_use_the_expiry_actor(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        connection.execute(
            """
            UPDATE requests SET status = 'approved', version = version + 1
            WHERE id = ?
            """,
            (request.id,),
        )
        assert_aborts(
            connection,
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'pending', 'approved', 'system:expiry', NULL, ?,
                      'key-1', ?)
            """,
            (request.id, harness.clock.now_ms(), "a" * 64),
            "invalid actor",
        )


def test_only_one_terminal_transition_per_request(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        connection.execute(
            """
            UPDATE requests SET status = 'approved', version = version + 1
            WHERE id = ?
            """,
            (request.id,),
        )
        connection.execute(
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'pending', 'approved', 'alice@corp', NULL, ?,
                      'key-1', ?)
            """,
            (request.id, harness.clock.now_ms(), "a" * 64),
        )
        assert_aborts(
            connection,
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'pending', 'approved', 'alice@corp', NULL, ?,
                      'key-2', ?)
            """,
            (request.id, harness.clock.now_ms(), "b" * 64),
            "transition already exists",
        )


def test_from_status_must_be_pending(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db
    request = harness.propose().request

    with raw_connection(path) as connection:
        connection.execute(
            """
            UPDATE requests SET status = 'approved', version = version + 1
            WHERE id = ?
            """,
            (request.id,),
        )
        assert_aborts(
            connection,
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'approved', 'approved', 'alice@corp', NULL, ?,
                      'key-1', ?)
            """,
            (request.id, harness.clock.now_ms(), "a" * 64),
        )


def test_new_requests_must_start_pending(
    trigger_db: tuple[StoreHarness, Path],
) -> None:
    harness, path = trigger_db

    with raw_connection(path) as connection:
        assert_aborts(
            connection,
            """
            INSERT INTO requests (
                id, workflow_id, dedupe_key, status, action_json,
                action_digest, summary, requested_by, created_at_ms,
                expires_at_ms, version
            ) VALUES ('req_new', 'wf', NULL, 'approved', '{}', ?, 'summary',
                      'agent', 1000, 2000, 0)
            """,
            ("a" * 64,),
            "must be pending",
        )


@pytest.mark.parametrize(
    ("summary", "digest", "created", "expires"),
    [
        ("", "a" * 64, 1_000, 2_000),
        ("summary", "A" * 64, 1_000, 2_000),
        ("summary", "a" * 63, 1_000, 2_000),
        ("summary", "a" * 64, 1_000, 1_000),
        ("summary", "a" * 64, 2_000, 1_000),
    ],
)
def test_request_checks_reject_invalid_rows(
    trigger_db: tuple[StoreHarness, Path],
    summary: str,
    digest: str,
    created: int,
    expires: int,
) -> None:
    _harness, path = trigger_db

    with raw_connection(path) as connection:
        assert_aborts(
            connection,
            """
            INSERT INTO requests (
                id, workflow_id, dedupe_key, status, action_json,
                action_digest, summary, requested_by, created_at_ms,
                expires_at_ms, version
            ) VALUES ('req_bad', 'wf', NULL, 'pending', '{}', ?, ?, 'agent',
                      ?, ?, 0)
            """,
            (digest, summary, created, expires),
        )
