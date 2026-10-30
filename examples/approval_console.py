"""Terminal approval console: the human's side of the gate.

The console is only a surface.  The person using it is the approver, and the
decision transition records that person's identity.  The console never
invents a decision and never types a request ID or digest on the human's
behalf.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable, Sequence

from approval_gate.clock import (
    Clock,
    RealAsyncSleeper,
    RealSyncSleeper,
    SystemClock,
    UuidIds,
)
from approval_gate.errors import ApprovalError
from approval_gate.gate import ApprovalGate
from approval_gate.models import Outcome, Request, Status
from approval_gate.presentation import (
    action_rows,
    canonical_json_text,
    digest_fingerprint,
    format_remaining,
    format_utc,
    one_line,
    safe_terminal,
    short_reference,
)
from approval_gate.stores.sqlite import SqliteStore

__all__ = ["main", "build_parser", "ApprovalConsole"]

_EXIT_OK = 0
_EXIT_INVALID = 6


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(_EXIT_INVALID, f"{self.prog}: error: {message}\n")


class _ConsoleCancelled(Exception):
    """The human closed the prompt or pressed Ctrl-C."""


class _InvalidTarget(Exception):
    """A command target is malformed, so no default may be selected."""


class ApprovalConsole:
    """A small command loop over the durable approval store."""

    def __init__(
        self,
        gate: ApprovalGate,
        *,
        clock: Clock,
        responder: str,
        db_path: str,
        workflow_id: str | None = None,
        limit: int = 100,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._gate = gate
        self._clock = clock
        self._responder = responder
        self._db_path = db_path
        self._workflow_id = workflow_id
        self._limit = limit
        self._input = input_fn or input
        self._last_pending: list[Request] = []
        self._selected_id: str | None = None

    def run(self) -> int:
        print("Approval Console")
        print(f"Approver: {one_line(self._responder)}")
        print(f"Database: {self._db_path}")
        print('Type "help" for commands.')
        print()
        while True:
            try:
                line = self._input("approval> ")
            except (EOFError, KeyboardInterrupt):
                print()
                print("Goodbye.")
                return _EXIT_OK
            command, _, rest = line.strip().partition(" ")
            command = command.lower()
            rest = rest.strip()
            if not command:
                continue

            try:
                if command in {"help", "h", "?"}:
                    self._help()
                elif command in {"pending", "list", "ls", "inbox"}:
                    self._show_pending()
                elif command in {"show", "display"}:
                    self._show(self._target(rest))
                elif command in {"approve", "a"}:
                    self._approve(self._target(rest))
                elif command in {"reject", "r"}:
                    target, reason = self._reject_arguments(rest)
                    self._reject(target, reason)
                elif command in {"details", "d"}:
                    self._details(self._target(rest))
                elif command in {"quit", "exit", "q"}:
                    print("Goodbye.")
                    return _EXIT_OK
                else:
                    print(f"Unknown command: {command}. Type 'help'.")
            except _ConsoleCancelled:
                print()
                print("Goodbye.")
                return _EXIT_OK
            except _InvalidTarget as exc:
                print(f"{exc} Type 'help' for usage.")
            except ApprovalError as exc:
                self._report_error(exc)

    def _help(self) -> None:
        print("Commands:")
        print("  pending                 list pending approvals")
        print("  show [N|ID|#REF]        show an approval card")
        print("  approve [N|ID|#REF]     approve after confirmation")
        print("  reject [N|ID|#REF] REASON")
        print("                          reject with a reason")
        print("  details [N|ID|#REF]     full ID, digest, canonical payload")
        print("  quit                    leave the console")
        print()

    def _show_pending(self) -> None:
        page = self._gate.pending(
            workflow_id=self._workflow_id,
            limit=self._limit,
        )
        if page.swept:
            print(f"Swept {page.swept} expired request(s).")
        self._last_pending = page.requests
        if not page.requests:
            self._selected_id = None
            print("No pending approvals.")
            print()
            return
        now_ms = self._clock.now_ms()
        print("Pending approvals:")
        for index, request in enumerate(page.requests, start=1):
            print(
                f"  {index}. {short_reference(request.id)}  "
                f"{one_line(request.summary)}",
            )
            print(
                f"     {one_line(request.workflow_id)} | "
                f"{one_line(request.requested_by)} | "
                f"{format_remaining(now_ms, request.expires_at_ms)}",
            )
        if len(page.requests) == 1:
            self._selected_id = page.requests[0].id
        else:
            self._selected_id = None
        print('Use "show N", "approve N", or "reject N <reason>".')
        print()

    def _show(self, target: str | None) -> None:
        request = self._resolve(target)
        if request is None:
            return
        request = self._refresh_if_overdue(request)
        outcome = self._gate.outcome(request.id)
        if request.status is not Status.PENDING or outcome is not None:
            self._print_receipt(request, outcome)
            return
        self._print_card(request)

    def _approve(self, target: str | None) -> None:
        request = self._resolve(target)
        if request is None:
            return
        request = self._refresh_if_overdue(request)
        outcome = self._gate.outcome(request.id)
        if outcome is not None:
            self._print_receipt(request, outcome)
            return

        self._print_card(request)
        if not self._confirm(
            f'Approve "{_short(request.summary)}"? [y/N] ',
        ):
            print("Not approved.")
            return

        key = uuid.uuid4().hex
        try:
            outcome = self._gate.approve(
                request.id,
                responder=self._responder,
                expected_digest=request.action_digest,
                idempotency_key=key,
            )
        except ApprovalError as exc:
            self._report_error(exc)
            return
        self._selected_id = None
        self._print_outcome(outcome, request)

    def _reject(self, target: str | None, reason: str) -> None:
        request = self._resolve(target)
        if request is None:
            return
        request = self._refresh_if_overdue(request)
        outcome = self._gate.outcome(request.id)
        if outcome is not None:
            self._print_receipt(request, outcome)
            return

        self._print_card(request)
        if not reason:
            reason = self._ask("Reason for rejection: ").strip()
        while not reason:
            print("A rejection needs a non-empty reason.")
            reason = self._ask("Reason for rejection: ").strip()
        if not self._confirm(
            f'Reject "{_short(request.summary)}"? [y/N] ',
        ):
            print("Not rejected.")
            return

        key = uuid.uuid4().hex
        try:
            outcome = self._gate.reject(
                request.id,
                responder=self._responder,
                expected_digest=request.action_digest,
                idempotency_key=key,
                reason=reason,
            )
        except ApprovalError as exc:
            self._report_error(exc)
            return
        self._selected_id = None
        self._print_outcome(outcome, request)

    def _details(self, target: str | None) -> None:
        request = self._resolve(target)
        if request is None:
            return
        print("Technical details")
        print(f"  request_id: {request.id}")
        print(f"  action_digest: {request.action_digest}")
        print(
            f"  payload_fingerprint: "
            f"{digest_fingerprint(request.action_digest)}",
        )
        print(f"  status: {request.status.value}")
        print(f"  created_at: {format_utc(request.created_at_ms)}")
        print(f"  expires_at: {format_utc(request.expires_at_ms)}")
        print(f"  version: {request.version}")
        print("canonical_json:")
        for line in canonical_json_text(request).splitlines():
            print(f"  {safe_terminal(line)}")
        print()

    def _resolve(self, target: str | None) -> Request | None:
        if target is not None and target.isdigit():
            page = self._refresh_pending()
            index = int(target)
            if 1 <= index <= len(page.requests):
                request = page.requests[index - 1]
                self._selected_id = request.id
                return request
            print(f"Choose a number from 1 to {len(page.requests)}.")
            return None

        if target is not None:
            if target.startswith("#") or _looks_like_short_reference(target):
                return self._resolve_short_reference(target)
            request = self._gate.get(target)
            if request is None:
                print(f"Request {target!r} was not found.")
                return None
            self._selected_id = request.id
            return request

        if self._selected_id is not None:
            request = self._gate.get(self._selected_id)
            if request is not None:
                return request
            self._selected_id = None

        page = self._refresh_pending()
        if not page.requests:
            print("No pending approvals.")
            return None
        if len(page.requests) == 1:
            request = page.requests[0]
            self._selected_id = request.id
            return request
        print("More than one request is pending; specify a number or ID.")
        self._show_pending()
        return None

    def _resolve_short_reference(self, target: str) -> Request | None:
        needle = target[1:] if target.startswith("#") else target
        needle = needle.upper()
        rows = self._gate.list(
            workflow_id=self._workflow_id,
            limit=self._limit,
        )
        matches = [
            request
            for request in rows
            if short_reference(request.id)[1:] == needle
        ]
        if len(matches) == 1:
            self._selected_id = matches[0].id
            return matches[0]
        if len(matches) > 1:
            print(f"Short reference {target!r} is ambiguous; use the full ID.")
            return None
        print(f"No request with short reference {target!r}.")
        return None

    def _refresh_pending(self):
        page = self._gate.pending(
            workflow_id=self._workflow_id,
            limit=self._limit,
        )
        if page.swept:
            print(f"Swept {page.swept} expired request(s).")
        self._last_pending = page.requests
        return page

    def _refresh_if_overdue(self, request: Request) -> Request:
        if not request.overdue:
            return request
        self._gate.sweep(request_id=request.id)
        refreshed = self._gate.get(request.id)
        return refreshed or request

    def _print_card(self, request: Request) -> None:
        now_ms = self._clock.now_ms()
        print()
        print(f"Approval {short_reference(request.id)}")
        print(one_line(request.summary))
        print(
            f"Requested by {one_line(request.requested_by)} | "
            f"workflow {one_line(request.workflow_id)}",
        )
        print(f"Created {format_utc(request.created_at_ms)}")
        print(
            f"Expires {format_utc(request.expires_at_ms)} "
            f"({format_remaining(now_ms, request.expires_at_ms)})",
        )
        print()
        for row in action_rows(request.action):
            if row.block:
                print(f"{row.label}:")
                self._print_block(row.value, max_lines=30)
            else:
                print(f"{row.label}: {safe_terminal(row.value)}")
        print()
        print(
            f"Payload fingerprint: "
            f"{digest_fingerprint(request.action_digest)}",
        )
        if request.status is Status.PENDING:
            print("The agent is waiting and will only resume if approved.")
        print()

    def _print_block(
        self,
        value: str,
        *,
        indent: str = "  ",
        max_lines: int | None = None,
    ) -> None:
        lines = safe_terminal(value).splitlines() or [""]
        shown = lines if max_lines is None else lines[:max_lines]
        for line in shown:
            print(f"{indent}{line}")
        if max_lines is not None and len(lines) > max_lines:
            remaining = len(lines) - max_lines
            print(f"{indent}... {remaining} more line(s); use details")

    def _print_receipt(
        self,
        request: Request,
        outcome: Outcome | None,
    ) -> None:
        print()
        print(f"Approval {short_reference(request.id)}")
        print(f"  summary: {one_line(request.summary)}")
        print(f"  status: {request.status.value}")
        if outcome is None:
            print("  outcome: not recorded")
            print()
            return
        print(f"  actor: {one_line(outcome.actor or '-')}")
        if outcome.reason is not None:
            print(f"  reason: {one_line(outcome.reason)}")
        print(f"  decided: {format_utc(outcome.at_ms)}")
        print(f"  request_id: {outcome.request_id}")
        print(
            f"  payload_fingerprint: "
            f"{digest_fingerprint(outcome.action_digest)}",
        )
        print()
        if outcome.status is Status.APPROVED:
            print("The agent may now verify and execute the approved payload.")
        else:
            print("The agent will not execute this action.")
        print()

    def _print_outcome(self, outcome: Outcome, request: Request) -> None:
        suffix = " (replayed)" if outcome.replayed else ""
        print(f"{outcome.status.value} {outcome.request_id}{suffix}")
        print(f"  summary: {one_line(request.summary)}")
        print(f"  actor: {one_line(outcome.actor or '-')}")
        if outcome.reason is not None:
            print(f"  reason: {one_line(outcome.reason)}")
        print(f"  decided: {format_utc(outcome.at_ms)}")
        if outcome.status is Status.APPROVED:
            print("The agent may now verify and execute the approved payload.")
        else:
            print("The agent will not execute this action.")
        print()

    def _report_error(self, exc: ApprovalError) -> None:
        print(f"error: {exc}")
        outcome = getattr(exc, "outcome", None)
        if outcome is not None:
            print(
                f"current: status={outcome.status.value} "
                f"actor={outcome.actor or '-'} at={format_utc(outcome.at_ms)}",
            )
        print()

    def _target(self, rest: str) -> str | None:
        if not rest:
            return None
        if len(rest.split()) != 1:
            raise _InvalidTarget(
                "A target must be one number or request ID.",
            )
        return rest

    def _reject_arguments(self, rest: str) -> tuple[str | None, str]:
        if not rest:
            return None, ""
        first, _, tail = rest.partition(" ")
        if (
            first.isdigit()
            or first.startswith("req_")
            or first.startswith("#")
            or _looks_like_short_reference(first)
        ):
            return first, tail.strip()
        return None, rest

    def _ask(self, prompt: str) -> str:
        try:
            return self._input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            raise _ConsoleCancelled from None

    def _confirm(self, prompt: str) -> bool:
        return self._ask(prompt).strip().lower() in {"y", "yes"}


def _looks_like_short_reference(token: str) -> bool:
    raw = token[1:] if token.startswith("#") else token
    return len(raw) == 8 and all(
        character in "0123456789abcdefABCDEF" for character in raw
    )


def _short(text: str, limit: int = 80) -> str:
    printable = one_line(text)
    if len(printable) <= limit:
        return printable
    return f"{printable[: limit - 1]}..."


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="approval-console",
        description=(
            "Review pending actions and record the human decision. "
            "The console is only the surface; the identity passed with --as "
            "is the actor recorded by the gate."
        ),
    )
    parser.add_argument(
        "--db",
        default="approvals.db",
        help="SQLite database shared with the agent",
    )
    parser.add_argument(
        "--as",
        dest="responder",
        required=True,
        help="human identity recorded on approve/reject (assertion, not auth)",
    )
    parser.add_argument(
        "--workflow",
        help="only list requests from this workflow",
    )
    parser.add_argument("--limit", type=int, default=100)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Clock | None = None,
    input_fn: Callable[[str], str] | None = None,
) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if not arguments.responder.strip():
        parser.error("--as must be a non-empty identity")
    resolved_clock = clock or SystemClock()
    store = SqliteStore(arguments.db, clock=resolved_clock)
    gate = ApprovalGate(
        store=store,
        clock=resolved_clock,
        ids=UuidIds(),
        sync_sleeper=RealSyncSleeper(),
        async_sleeper=RealAsyncSleeper(),
    )
    try:
        console = ApprovalConsole(
            gate,
            clock=resolved_clock,
            responder=arguments.responder,
            db_path=arguments.db,
            workflow_id=arguments.workflow,
            limit=arguments.limit,
            input_fn=input_fn,
        )
        return console.run()
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
