"""Non-blocking agent-side bridge for the approval gate.

The coding agent uses ``propose`` to create a durable request, then ends its
turn.  After the human decides in the approval console, the agent uses
``status`` or ``wait`` to retrieve the outcome and the verified payload.

Exit codes are deliberately useful to a shell-driven agent::

    0  approved (or successfully proposed)
    1  internal error
    2  request not found
    3  rejected
    4  expired
    5  cancelled
    6  still pending
    7  invalid input
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from approval_gate.clock import (
    AsyncSleeper,
    Clock,
    IdGenerator,
    RealAsyncSleeper,
    RealSyncSleeper,
    SyncSleeper,
    SystemClock,
    UuidIds,
)
from approval_gate.errors import (
    ApprovalError,
    IntegrityError,
    InvalidRequest,
    RequestNotFound,
    StillPending,
    StoreBusy,
)
from approval_gate.gate import ApprovalGate
from approval_gate.models import Outcome, Request, Status
from approval_gate.payload import thaw_json
from approval_gate.presentation import (
    digest_fingerprint,
    format_utc,
    one_line,
    short_reference,
)
from approval_gate.stores.sqlite import SqliteStore

__all__ = ["main", "build_parser"]

_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_NOT_FOUND = 2
_EXIT_REJECTED = 3
_EXIT_EXPIRED = 4
_EXIT_CANCELLED = 5
_EXIT_PENDING = 6
_EXIT_INVALID = 7


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(_EXIT_INVALID, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="approval-agent",
        description=(
            "Agent-side bridge: propose an action, then read the human's "
            "durable decision and verified payload."
        ),
    )
    parser.add_argument(
        "--db",
        default="approvals.db",
        help="SQLite database shared with the approval console",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    propose = subparsers.add_parser(
        "propose",
        help="create a pending approval request and return immediately",
    )
    propose.add_argument("--workflow", required=True)
    propose.add_argument("--summary", required=True)
    action = propose.add_mutually_exclusive_group(required=True)
    action.add_argument("--action", help="JSON object to approve")
    action.add_argument(
        "--action-file",
        type=Path,
        help="path to a UTF-8 JSON object to approve",
    )
    propose.add_argument("--ttl-seconds", type=float, default=3_600.0)
    propose.add_argument("--requested-by", default="agent:example")
    propose.add_argument("--dedupe-key")
    propose.add_argument("--json", action="store_true")

    status = subparsers.add_parser(
        "status",
        help="read a request; print the verified payload when approved",
    )
    status.add_argument("request_id")
    status.add_argument("--json", action="store_true")

    wait = subparsers.add_parser(
        "wait",
        help="poll until the request is terminal or the timeout expires",
    )
    wait.add_argument("request_id")
    wait.add_argument("--timeout", type=float)
    wait.add_argument("--poll-interval", type=float)
    wait.add_argument("--json", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
    sync_sleeper: SyncSleeper | None = None,
    async_sleeper: AsyncSleeper | None = None,
) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    resolved_clock = clock or SystemClock()
    store = SqliteStore(arguments.db, clock=resolved_clock)
    gate = ApprovalGate(
        store=store,
        clock=resolved_clock,
        ids=ids or UuidIds(),
        sync_sleeper=sync_sleeper or RealSyncSleeper(),
        async_sleeper=async_sleeper or RealAsyncSleeper(),
    )

    try:
        if arguments.command == "propose":
            return _propose(gate, arguments)
        if arguments.command == "status":
            return _status(gate, arguments.request_id, as_json=arguments.json)
        if arguments.command == "wait":
            return _wait(gate, arguments)
        parser.error(f"unknown command: {arguments.command}")
        return _EXIT_INVALID
    finally:
        store.close()


def _propose(gate: ApprovalGate, arguments: argparse.Namespace) -> int:
    action = _load_action(arguments)
    if action is None:
        return _EXIT_INVALID
    ttl = _ttl_from_arguments(arguments.ttl_seconds)
    if ttl is None:
        return _EXIT_INVALID
    try:
        proposal = gate.propose(
            workflow_id=arguments.workflow,
            action=action,
            summary=arguments.summary,
            requested_by=arguments.requested_by,
            ttl=ttl,
            dedupe_key=arguments.dedupe_key,
        )
    except ApprovalError as exc:
        return _report_error(exc)

    request = proposal.request
    if arguments.json:
        _print_json(
            {
                "request_id": request.id,
                "short_ref": short_reference(request.id),
                "status": request.status.value,
                "summary": request.summary,
                "workflow_id": request.workflow_id,
                "requested_by": request.requested_by,
                "action_digest": request.action_digest,
                "expires_at_ms": request.expires_at_ms,
                "deduplicated": proposal.deduplicated,
            },
        )
    else:
        print(f"request_id={request.id}")
        print(f"short_ref={short_reference(request.id)}")
        print(f"status={request.status.value}")
        print(f"summary={one_line(request.summary)}")
        print(f"workflow={one_line(request.workflow_id)}")
        print(f"requested_by={one_line(request.requested_by)}")
        print(f"expires={format_utc(request.expires_at_ms)}")
        print(f"action_digest={request.action_digest}")
        print(f"deduplicated={str(proposal.deduplicated).lower()}")
    return _EXIT_OK


def _status(
    gate: ApprovalGate,
    request_id: str,
    *,
    as_json: bool,
) -> int:
    request = gate.get(request_id)
    if request is None:
        print(f"error: request {request_id!r} does not exist", file=sys.stderr)
        return _EXIT_NOT_FOUND
    outcome = gate.outcome(request_id)
    if outcome is None:
        if request.status is not Status.PENDING:
            print(
                f"error: terminal request {request_id!r} has no outcome",
                file=sys.stderr,
            )
            return _EXIT_ERROR
        return _print_pending(request, as_json=as_json)
    return _print_outcome(gate, request, outcome, as_json=as_json)


def _wait(gate: ApprovalGate, arguments: argparse.Namespace) -> int:
    try:
        outcome = gate.wait(
            arguments.request_id,
            timeout=arguments.timeout,
            poll_interval=arguments.poll_interval,
        )
    except StillPending as exc:
        request = gate.get(arguments.request_id)
        if request is None:
            print(f"error: {exc}", file=sys.stderr)
            return _EXIT_NOT_FOUND
        return _print_pending(request, as_json=arguments.json)
    except ApprovalError as exc:
        return _report_error(exc)

    request = gate.get(arguments.request_id)
    if request is None:
        print(
            f"error: request {arguments.request_id!r} does not exist",
            file=sys.stderr,
        )
        return _EXIT_NOT_FOUND
    return _print_outcome(gate, request, outcome, as_json=arguments.json)


def _print_pending(request: Request, *, as_json: bool) -> int:
    status = "pending"
    if request.overdue:
        status = "pending (overdue)"
    if as_json:
        _print_json(
            {
                "request_id": request.id,
                "short_ref": short_reference(request.id),
                "status": request.status.value,
                "overdue": request.overdue,
                "summary": request.summary,
                "action_digest": request.action_digest,
                "expires_at_ms": request.expires_at_ms,
                "verified": False,
                "action": None,
                "canonical_json": None,
            },
        )
    else:
        print(f"{status} {request.id}")
        print(f"summary={one_line(request.summary)}")
        print(f"expires={format_utc(request.expires_at_ms)}")
        print(f"payload_fingerprint={digest_fingerprint(request.action_digest)}")
    return _EXIT_PENDING


def _print_outcome(
    gate: ApprovalGate,
    request: Request,
    outcome: Outcome,
    *,
    as_json: bool,
) -> int:
    verified = None
    if outcome.status is Status.APPROVED:
        try:
            verified = gate.verified_action(outcome)
        except ApprovalError as exc:
            return _report_error(exc)

    if as_json:
        _print_json(
            {
                "request_id": request.id,
                "short_ref": short_reference(request.id),
                "status": outcome.status.value,
                "summary": request.summary,
                "action_digest": outcome.action_digest,
                "actor": outcome.actor,
                "reason": outcome.reason,
                "at_ms": outcome.at_ms,
                "verified": verified is not None,
                "action": (
                    thaw_json(verified.action) if verified is not None else None
                ),
                "canonical_json": (
                    verified.canonical_json.decode("utf-8")
                    if verified is not None
                    else None
                ),
            },
        )
    else:
        suffix = " (replayed)" if outcome.replayed else ""
        print(f"{outcome.status.value} {request.id}{suffix}")
        print(f"summary={one_line(request.summary)}")
        print(f"actor={one_line(outcome.actor or '-')}")
        if outcome.reason is not None:
            print(f"reason={one_line(outcome.reason)}")
        print(f"decided={format_utc(outcome.at_ms)}")
        print(f"payload_fingerprint={digest_fingerprint(outcome.action_digest)}")
        if verified is not None:
            print("verified=true")
            print(f"canonical_json={verified.canonical_json.decode('utf-8')}")

    if outcome.status is Status.APPROVED:
        return _EXIT_OK
    if outcome.status is Status.REJECTED:
        return _EXIT_REJECTED
    if outcome.status is Status.EXPIRED:
        return _EXIT_EXPIRED
    if outcome.status is Status.CANCELLED:
        return _EXIT_CANCELLED
    return _EXIT_ERROR


def _load_action(arguments: argparse.Namespace) -> dict[str, Any] | None:
    try:
        if arguments.action_file is not None:
            raw = arguments.action_file.read_text(encoding="utf-8-sig")
        else:
            raw = arguments.action
        action = json.loads(raw)
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: cannot read action JSON: {exc}", file=sys.stderr)
        return None
    if not isinstance(action, dict):
        print("error: action must be a JSON object", file=sys.stderr)
        return None
    return action


def _ttl_from_arguments(seconds: float) -> timedelta | None:
    if not math.isfinite(seconds) or seconds <= 0:
        print(
            "error: --ttl-seconds must be a positive finite number",
            file=sys.stderr,
        )
        return None
    try:
        return timedelta(seconds=seconds)
    except OverflowError:
        print("error: --ttl-seconds is too large", file=sys.stderr)
        return None


def _report_error(exc: ApprovalError) -> int:
    print(f"error: {exc}", file=sys.stderr)
    if isinstance(exc, (IntegrityError, StoreBusy)):
        return _EXIT_ERROR
    if isinstance(exc, RequestNotFound):
        return _EXIT_NOT_FOUND
    if isinstance(exc, InvalidRequest):
        return _EXIT_INVALID
    return _EXIT_ERROR


def _print_json(payload: dict[str, Any]) -> None:
    print(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
