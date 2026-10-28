"""Thin command-line interface for the human side of the gate."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from .clock import (
    Clock,
    RealAsyncSleeper,
    RealSyncSleeper,
    SystemClock,
    UuidIds,
)
from .errors import (
    AlreadyDecided,
    ApprovalError,
    DigestMismatch,
    IdempotencyConflict,
    IntegrityError,
    InvalidRequest,
    RequestCancelled,
    RequestExpired,
    RequestNotFound,
    StoreBusy,
)
from .gate import ApprovalGate
from .models import Request
from .payload import thaw_json
from .stores.sqlite import SqliteStore

__all__ = ["main", "build_parser"]

_EXIT_OK = 0
_EXIT_NOT_FOUND = 2
_EXIT_CONFLICT = 3
_EXIT_EXPIRED = 4
_EXIT_DIGEST_MISMATCH = 5
_EXIT_INVALID = 6
_EXIT_ERROR = 1


class _Parser(argparse.ArgumentParser):
    """Use the documented invalid-input exit code for usage errors."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(_EXIT_INVALID, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="approval-gate",
        description=(
            "Inspect and decide durable human-approval requests. "
            "The 'list' command sweeps overdue requests before listing."
        ),
    )
    parser.add_argument(
        "--db",
        default="approvals.db",
        help="SQLite database file (default: approvals.db)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list",
        help="sweep overdue requests, then list pending ones",
        description=(
            "Sweep overdue pending requests, then list only requests that are "
            "still pending and unexpired. This command writes audit rows for "
            "expiry transitions."
        ),
    )
    list_parser.add_argument("--workflow", help="filter by workflow ID")
    list_parser.add_argument("--limit", type=int, default=100)

    show_parser = subparsers.add_parser(
        "show",
        help="show one request without sweeping",
        description="Show one request with a pure read. No expiry is written.",
    )
    show_parser.add_argument("request_id")

    approve_parser = subparsers.add_parser(
        "approve",
        help="approve a pending request",
        description="Approve the exact payload identified by its full digest.",
    )
    approve_parser.add_argument("request_id")
    approve_parser.add_argument("--responder", required=True)
    approve_parser.add_argument(
        "--digest",
        required=True,
        dest="expected_digest",
        help="full 64-character SHA-256 digest returned by propose()",
    )
    approve_parser.add_argument("--reason")
    _add_key_argument(approve_parser)

    reject_parser = subparsers.add_parser(
        "reject",
        help="reject a pending request",
        description="Reject a request. A non-empty reason is required.",
    )
    reject_parser.add_argument("request_id")
    reject_parser.add_argument("--responder", required=True)
    reject_parser.add_argument(
        "--digest",
        required=True,
        dest="expected_digest",
    )
    reject_parser.add_argument("--reason", required=True)
    _add_key_argument(reject_parser)

    cancel_parser = subparsers.add_parser(
        "cancel",
        help="cancel a pending request",
        description="Cancel a pending request. The actor is an assertion, not auth.",
    )
    cancel_parser.add_argument("request_id")
    cancel_parser.add_argument("--actor", required=True)
    cancel_parser.add_argument("--reason")
    _add_key_argument(cancel_parser)

    subparsers.add_parser(
        "sweep",
        help="expire every overdue pending request",
        description="Record an expiry transition for every overdue request.",
    )
    return parser


def _add_key_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--idempotency-key",
        help=(
            "retry-safe operation key; generated and printed when omitted so "
            "a failed invocation can be retried with the same key"
        ),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Clock | None = None,
) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "reject" and not arguments.reason.strip():
        print("error: reject requires a non-empty --reason", file=sys.stderr)
        return _EXIT_INVALID

    gate = _build_gate(arguments.db, clock=clock)

    if arguments.command == "list":
        return _list(gate, arguments.workflow, arguments.limit)
    if arguments.command == "show":
        return _show(gate, arguments.request_id)
    if arguments.command == "sweep":
        print(f"swept {gate.sweep()}")
        return _EXIT_OK
    if arguments.command == "approve":
        return _decide(
            parser,
            gate,
            arguments,
            operation="approve",
            supplied_key=arguments.idempotency_key,
        )
    if arguments.command == "reject":
        return _decide(
            parser,
            gate,
            arguments,
            operation="reject",
            supplied_key=arguments.idempotency_key,
        )
    if arguments.command == "cancel":
        return _decide(
            parser,
            gate,
            arguments,
            operation="cancel",
            supplied_key=arguments.idempotency_key,
        )
    parser.error(f"unknown command: {arguments.command}")
    return _EXIT_INVALID


def _build_gate(db_path: str, *, clock: Clock | None = None) -> ApprovalGate:
    resolved_clock = clock or SystemClock()
    store = SqliteStore(db_path, clock=resolved_clock)
    return ApprovalGate(
        store=store,
        clock=resolved_clock,
        ids=UuidIds(),
        sync_sleeper=RealSyncSleeper(),
        async_sleeper=RealAsyncSleeper(),
    )


def _list(gate: ApprovalGate, workflow: str | None, limit: int) -> int:
    try:
        page = gate.pending(workflow_id=workflow, limit=limit)
    except ApprovalError as exc:
        return _report_error(exc)
    print(f"swept {page.swept}")
    if not page.requests:
        print("no pending requests")
        return _EXIT_OK
    for request in page.requests:
        print(_format_request_line(request))
    return _EXIT_OK


def _show(gate: ApprovalGate, request_id: str) -> int:
    try:
        request = gate.get(request_id)
        if request is None:
            print(f"error: request {request_id!r} not found", file=sys.stderr)
            return _EXIT_NOT_FOUND
        outcome = gate.outcome(request_id)
    except ApprovalError as exc:
        return _report_error(exc)

    print(f"id: {request.id}")
    print(f"status: {request.status.value}{' (overdue)' if request.overdue else ''}")
    print(f"workflow_id: {request.workflow_id}")
    print(f"summary: {request.summary}")
    print(f"requested_by: {request.requested_by}")
    print(f"created_at: {_iso_time(request.created_at_ms)}")
    print(f"expires_at: {_iso_time(request.expires_at_ms)}")
    print(f"version: {request.version}")
    if request.dedupe_key is not None:
        print(f"dedupe_key: {request.dedupe_key}")
    print(f"action_digest: {request.action_digest}")
    print("action:")
    print(
        json.dumps(
            thaw_json(request.action),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    if outcome is not None:
        print("outcome:")
        print(f"  status: {outcome.status.value}")
        print(f"  actor: {outcome.actor}")
        print(f"  reason: {outcome.reason}")
        print(f"  at: {_iso_time(outcome.at_ms)}")
    return _EXIT_OK


def _decide(
    parser: argparse.ArgumentParser,
    gate: ApprovalGate,
    arguments: argparse.Namespace,
    *,
    operation: str,
    supplied_key: str | None,
) -> int:
    key = supplied_key
    if key is None:
        key = uuid.uuid4().hex
        print(f"idempotency_key={key}", file=sys.stderr)
    if not key:
        print("error: --idempotency-key must be non-empty", file=sys.stderr)
        return _EXIT_INVALID

    try:
        if operation == "approve":
            outcome = gate.approve(
                arguments.request_id,
                responder=arguments.responder,
                expected_digest=arguments.expected_digest,
                idempotency_key=key,
                reason=arguments.reason,
            )
        elif operation == "reject":
            outcome = gate.reject(
                arguments.request_id,
                responder=arguments.responder,
                expected_digest=arguments.expected_digest,
                idempotency_key=key,
                reason=arguments.reason,
            )
        elif operation == "cancel":
            outcome = gate.cancel(
                arguments.request_id,
                actor=arguments.actor,
                idempotency_key=key,
                reason=arguments.reason,
            )
        else:
            parser.error(f"unknown operation: {operation}")
            return _EXIT_INVALID
    except ApprovalError as exc:
        return _report_error(exc)

    suffix = " (replayed)" if outcome.replayed else ""
    print(f"{outcome.status.value} {outcome.request_id}{suffix}")
    return _EXIT_OK


def _report_error(exc: ApprovalError) -> int:
    print(f"error: {exc}", file=sys.stderr)
    if isinstance(exc, (AlreadyDecided, RequestExpired, RequestCancelled)):
        outcome = exc.outcome
        print(
            f"current: status={outcome.status.value} actor={outcome.actor} "
            f"at_ms={outcome.at_ms}",
            file=sys.stderr,
        )
    if isinstance(exc, RequestNotFound):
        return _EXIT_NOT_FOUND
    if isinstance(exc, (IdempotencyConflict, AlreadyDecided)):
        return _EXIT_CONFLICT
    if isinstance(exc, (RequestExpired, RequestCancelled)):
        return _EXIT_EXPIRED
    if isinstance(exc, DigestMismatch):
        return _EXIT_DIGEST_MISMATCH
    if isinstance(exc, InvalidRequest):
        return _EXIT_INVALID
    if isinstance(exc, (IntegrityError, StoreBusy)):
        return _EXIT_ERROR
    return _EXIT_ERROR


def _format_request_line(request: Request) -> str:
    return (
        f"{request.id}  {request.status.value}  "
        f"expires={_iso_time(request.expires_at_ms)}  "
        f"workflow={request.workflow_id}  {request.summary}"
    )


def _iso_time(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1_000, tz=timezone.utc).isoformat(
        timespec="milliseconds",
    )


if __name__ == "__main__":
    raise SystemExit(main())
