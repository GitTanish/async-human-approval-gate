"""Durable SQLite store.

Every operation opens its own connection.  This is simple, safe for the
human-approval workload, and keeps SQLite connections out of shared threads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from importlib.resources import files
from pathlib import Path

from ..clock import Clock
from ..errors import IntegrityError, StoreBusy
from ..models import Outcome, Request, Status, Transition
from ..payload import freeze_canonical_action, sha256_hex
from ..state import EXPIRY_ACTOR, EXPIRY_REASON, Event
from .base import (
    NewRequest,
    ProposeResult,
    ResponseCommand,
    ResponseKind,
    ResponseResult,
    Store,
    SweepResult,
    outcome_from_transition,
    response_kind_for_terminal,
    target_status,
)

__all__ = ["SqliteStore", "SCHEMA_VERSION"]

SCHEMA_VERSION = 1
_SWEEP_BATCH = 1_000
_PROPOSAL_ATTEMPTS = 3


class SqliteStore(Store):
    """A SQLite-backed store with trigger-enforced audit integrity."""

    def __init__(self, path: str | Path, *, clock: Clock) -> None:
        self._path = str(path)
        if self._path == ":memory:":
            raise ValueError(
                "SqliteStore opens a connection per operation; use a file",
            )
        self._clock = clock
        self._initialize()

    def propose(self, command: NewRequest) -> ProposeResult:
        transitions: list[Transition] = []

        for _ in range(_PROPOSAL_ATTEMPTS):
            with self._write_tx() as connection:
                now_ms = self._clock.now_ms()

                if command.dedupe_key is not None:
                    existing = self._find_pending_dedupe(connection, command)
                    if existing is not None:
                        if existing["expires_at_ms"] > now_ms:
                            return ProposeResult(
                                request=self._row_to_request(existing),
                                deduplicated=True,
                                transitions=tuple(transitions),
                            )
                        transition = self._expire_row(
                            connection,
                            existing,
                            now_ms,
                        )
                        if transition is not None:
                            transitions.append(transition)

                request = self._request_from_command(command, now_ms)
                try:
                    self._insert_request(connection, request)
                except sqlite3.IntegrityError:
                    if command.dedupe_key is None:
                        raise
                    winner = self._find_pending_dedupe(connection, command)
                    if winner is None:
                        raise
                    if winner["expires_at_ms"] > now_ms:
                        return ProposeResult(
                            request=self._row_to_request(winner),
                            deduplicated=True,
                            transitions=tuple(transitions),
                        )
                    # The obsolete winner is expired at the top of the retry.
                    continue

                return ProposeResult(
                    request=request,
                    deduplicated=False,
                    transitions=tuple(transitions),
                )

        raise StoreBusy("could not resolve a dedupe race; retry")

    def get(self, request_id: str) -> Request | None:
        with self._read_tx() as connection:
            row = self._load_row(connection, request_id)
            return None if row is None else self._row_to_request(row)

    def list(
        self,
        *,
        status: Status | None = None,
        workflow_id: str | None = None,
        limit: int = 100,
        expires_after_ms: int | None = None,
    ) -> list[Request]:
        if limit <= 0:
            return []
        status_value = None if status is None else status.value
        with self._read_tx() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE (? IS NULL OR status = ?)
                  AND (? IS NULL OR workflow_id = ?)
                  AND (? IS NULL OR expires_at_ms > ?)
                ORDER BY created_at_ms DESC, id ASC
                LIMIT ?
                """,
                (
                    status_value,
                    status_value,
                    workflow_id,
                    workflow_id,
                    expires_after_ms,
                    expires_after_ms,
                    limit,
                ),
            ).fetchall()
            return [self._row_to_request(row) for row in rows]

    def outcome(self, request_id: str) -> Outcome | None:
        with self._read_tx() as connection:
            row = self._load_row(connection, request_id)
            if row is None or row["status"] == Status.PENDING.value:
                return None
            request = self._row_to_request(row)
            transition_row = self._load_transition_row(
                connection,
                request_id,
            )
            if transition_row is None:
                raise IntegrityError("terminal request has no transition")
            return outcome_from_transition(
                request,
                self._transition_from_row(transition_row),
            )

    def get_transition(self, request_id: str) -> Transition | None:
        with self._read_tx() as connection:
            row = self._load_transition_row(connection, request_id)
            return None if row is None else self._transition_from_row(row)

    def sweep(self, *, request_id: str | None = None) -> SweepResult:
        if request_id is not None:
            with self._write_tx() as connection:
                now_ms = self._clock.now_ms()
                row = self._load_row(connection, request_id)
                if (
                    row is None
                    or row["status"] != Status.PENDING.value
                    or row["expires_at_ms"] > now_ms
                ):
                    return SweepResult()
                transition = self._expire_row(connection, row, now_ms)
                transitions = () if transition is None else (transition,)
                return SweepResult(transitions=transitions)

        collected: list[Transition] = []
        while True:
            with self._write_tx() as connection:
                now_ms = self._clock.now_ms()
                rows = connection.execute(
                    """
                    SELECT * FROM requests
                    WHERE status = 'pending' AND expires_at_ms <= ?
                    ORDER BY created_at_ms ASC, id ASC
                    LIMIT ?
                    """,
                    (now_ms, _SWEEP_BATCH),
                ).fetchall()
                for row in rows:
                    transition = self._expire_row(connection, row, now_ms)
                    if transition is not None:
                        collected.append(transition)
            if len(rows) < _SWEEP_BATCH:
                break
        return SweepResult(transitions=tuple(collected))

    def respond(self, command: ResponseCommand) -> ResponseResult:
        with self._write_tx() as connection:
            now_ms = self._clock.now_ms()
            row = self._load_row(connection, command.request_id)
            if row is None:
                return ResponseResult(ResponseKind.NOT_FOUND)

            prior_row = connection.execute(
                """
                SELECT * FROM transitions
                WHERE request_id = ? AND idempotency_key = ?
                """,
                (command.request_id, command.idempotency_key),
            ).fetchone()
            if prior_row is not None:
                prior = self._transition_from_row(prior_row)
                if prior.request_fingerprint != command.fingerprint:
                    return ResponseResult(ResponseKind.IDEMPOTENCY_CONFLICT)
                request = self._row_to_request(row)
                return ResponseResult(
                    kind=ResponseKind.REPLAYED,
                    outcome=outcome_from_transition(
                        request,
                        prior,
                        replayed=True,
                    ),
                )

            if row["status"] != Status.PENDING.value:
                return self._classify(connection, row)

            if now_ms >= row["expires_at_ms"]:
                transition = self._expire_row(connection, row, now_ms)
                if transition is not None:
                    updated_row = self._load_row(connection, command.request_id)
                    if updated_row is None:
                        raise IntegrityError("request disappeared after expiry")
                    request = self._row_to_request(updated_row)
                    return ResponseResult(
                        kind=ResponseKind.EXPIRED,
                        outcome=outcome_from_transition(request, transition),
                        transition=transition,
                    )
                fresh = self._load_row(connection, command.request_id)
                if fresh is None:
                    return ResponseResult(ResponseKind.NOT_FOUND)
                return self._classify(connection, fresh)

            if (
                command.operation is not Event.CANCEL
                and command.expected_digest != row["action_digest"]
            ):
                return ResponseResult(ResponseKind.DIGEST_MISMATCH)

            target = target_status(command.operation)
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = ?, version = version + 1
                WHERE id = ?
                  AND status = 'pending'
                  AND expires_at_ms > ?
                """,
                (target.value, command.request_id, now_ms),
            )
            if cursor.rowcount != 1:
                fresh = self._load_row(connection, command.request_id)
                if fresh is None:
                    return ResponseResult(ResponseKind.NOT_FOUND)
                return self._classify(connection, fresh)

            transition = self._insert_transition(
                connection,
                request_id=command.request_id,
                to_status=target,
                actor=command.actor,
                reason=command.reason,
                at_ms=now_ms,
                idempotency_key=command.idempotency_key,
                request_fingerprint=command.fingerprint,
            )
            updated_row = self._load_row(connection, command.request_id)
            if updated_row is None:
                raise IntegrityError("request disappeared after update")
            request = self._row_to_request(updated_row)
            return ResponseResult(
                kind=ResponseKind.APPLIED,
                outcome=outcome_from_transition(request, transition),
                transition=transition,
            )

    def close(self) -> None:
        """No persistent connections are held."""

    def _initialize(self) -> None:
        with self._write_tx() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _schema_statements():
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported schema version {version}; "
                    f"expected {SCHEMA_VERSION}",
                )

    def _open_connection(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self._path,
                timeout=5.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL").fetchone()
            connection.execute("PRAGMA recursive_triggers = ON")
            return connection
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.close()
            if _is_busy(exc):
                raise StoreBusy("SQLite is busy; retry") from exc
            raise
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if _is_busy(exc):
                    raise StoreBusy("SQLite is busy; retry") from exc
                raise
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            try:
                connection.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                connection.rollback()
                if _is_busy(exc):
                    raise StoreBusy("SQLite is busy; retry") from exc
                raise
        finally:
            connection.close()

    @contextmanager
    def _read_tx(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            connection.execute("BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    def _request_from_command(
        self,
        command: NewRequest,
        now_ms: int,
    ) -> Request:
        if command.ttl_ms <= 0:
            raise IntegrityError("ttl must be positive")
        canonical = bytes(command.canonical_json)
        if sha256_hex(canonical) != command.action_digest:
            raise IntegrityError(
                "action digest does not match canonical bytes",
            )
        return Request(
            id=command.id,
            workflow_id=command.workflow_id,
            dedupe_key=command.dedupe_key,
            status=Status.PENDING,
            action=freeze_canonical_action(canonical),
            canonical_json=canonical,
            action_digest=command.action_digest,
            summary=command.summary,
            requested_by=command.requested_by,
            created_at_ms=now_ms,
            expires_at_ms=now_ms + command.ttl_ms,
            version=0,
        )

    def _insert_request(
        self,
        connection: sqlite3.Connection,
        request: Request,
    ) -> None:
        connection.execute(
            """
            INSERT INTO requests (
                id, workflow_id, dedupe_key, status, action_json,
                action_digest, summary, requested_by, created_at_ms,
                expires_at_ms, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.id,
                request.workflow_id,
                request.dedupe_key,
                request.status.value,
                request.canonical_json.decode("utf-8"),
                request.action_digest,
                request.summary,
                request.requested_by,
                request.created_at_ms,
                request.expires_at_ms,
                request.version,
            ),
        )

    def _find_pending_dedupe(
        self,
        connection: sqlite3.Connection,
        command: NewRequest,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM requests
            WHERE workflow_id = ?
              AND dedupe_key = ?
              AND status = 'pending'
            """,
            (command.workflow_id, command.dedupe_key),
        ).fetchone()

    def _load_row(
        self,
        connection: sqlite3.Connection,
        request_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM requests WHERE id = ?",
            (request_id,),
        ).fetchone()

    def _load_transition_row(
        self,
        connection: sqlite3.Connection,
        request_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM transitions WHERE request_id = ?",
            (request_id,),
        ).fetchone()

    def _row_to_request(self, row: sqlite3.Row) -> Request:
        canonical = row["action_json"].encode("utf-8")
        if sha256_hex(canonical) != row["action_digest"]:
            raise IntegrityError(
                "stored action digest does not match stored bytes",
            )
        try:
            status = Status(row["status"])
        except ValueError as exc:
            raise IntegrityError("stored request has an invalid status") from exc
        return Request(
            id=row["id"],
            workflow_id=row["workflow_id"],
            dedupe_key=row["dedupe_key"],
            status=status,
            action=freeze_canonical_action(canonical),
            canonical_json=canonical,
            action_digest=row["action_digest"],
            summary=row["summary"],
            requested_by=row["requested_by"],
            created_at_ms=row["created_at_ms"],
            expires_at_ms=row["expires_at_ms"],
            version=row["version"],
        )

    def _transition_from_row(self, row: sqlite3.Row) -> Transition:
        try:
            from_status = Status(row["from_status"])
            to_status = Status(row["to_status"])
        except ValueError as exc:
            raise IntegrityError("stored transition has an invalid status") from exc
        return Transition(
            seq=row["seq"],
            request_id=row["request_id"],
            from_status=from_status,
            to_status=to_status,
            actor=row["actor"],
            reason=row["reason"],
            at_ms=row["at_ms"],
            idempotency_key=row["idempotency_key"],
            request_fingerprint=row["request_fingerprint"],
        )

    def _expire_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now_ms: int,
    ) -> Transition | None:
        cursor = connection.execute(
            """
            UPDATE requests
            SET status = 'expired', version = version + 1
            WHERE id = ?
              AND status = 'pending'
              AND expires_at_ms <= ?
            """,
            (row["id"], now_ms),
        )
        if cursor.rowcount != 1:
            return None
        return self._insert_transition(
            connection,
            request_id=row["id"],
            to_status=Status.EXPIRED,
            actor=EXPIRY_ACTOR,
            reason=EXPIRY_REASON,
            at_ms=now_ms,
            idempotency_key=None,
            request_fingerprint=None,
        )

    def _insert_transition(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        to_status: Status,
        actor: str,
        reason: str | None,
        at_ms: int,
        idempotency_key: str | None,
        request_fingerprint: str | None,
    ) -> Transition:
        cursor = connection.execute(
            """
            INSERT INTO transitions (
                request_id, from_status, to_status, actor, reason, at_ms,
                idempotency_key, request_fingerprint
            ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                to_status.value,
                actor,
                reason,
                at_ms,
                idempotency_key,
                request_fingerprint,
            ),
        )
        seq = cursor.lastrowid
        if seq is None:
            raise IntegrityError("failed to read transition sequence")
        return Transition(
            seq=seq,
            request_id=request_id,
            from_status=Status.PENDING,
            to_status=to_status,
            actor=actor,
            reason=reason,
            at_ms=at_ms,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )

    def _classify(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ResponseResult:
        try:
            status = Status(row["status"])
        except ValueError as exc:
            raise IntegrityError("stored request has an invalid status") from exc
        if status is Status.PENDING:
            raise StoreBusy("request changed concurrently; retry")
        transition_row = self._load_transition_row(connection, row["id"])
        if transition_row is None:
            raise IntegrityError("terminal request has no transition")
        request = self._row_to_request(row)
        return ResponseResult(
            kind=response_kind_for_terminal(status),
            outcome=outcome_from_transition(
                request,
                self._transition_from_row(transition_row),
            ),
        )


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


@cache
def _schema_statements() -> tuple[str, ...]:
    text = files("approval_gate.stores").joinpath("schema.sql").read_text(
        encoding="utf-8",
    )
    statements: list[str] = []
    buffer = ""
    for line in text.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise RuntimeError("schema.sql ends with an incomplete statement")
    return tuple(statements)
