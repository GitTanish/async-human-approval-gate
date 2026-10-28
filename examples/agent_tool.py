"""Runnable agent-side example: propose, wait, verify, then execute.

By default this waits for a human using the CLI or HTTP demo.  Pass
``--auto-approve`` for a self-contained demo where a background demo principal
approves after a moment.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import timedelta

from approval_gate.clock import (
    RealAsyncSleeper,
    RealSyncSleeper,
    SystemClock,
    UuidIds,
)
from approval_gate.errors import ApprovalError
from approval_gate.gate import ApprovalGate
from approval_gate.models import Status
from approval_gate.payload import thaw_json
from approval_gate.stores.sqlite import SqliteStore

__all__ = ["main"]

ACTION = {
    "tool": "send_email",
    "to": "customer@example.com",
    "subject": "Your order has shipped",
    "body": "Your order #1234 is on its way.",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="approvals.db")
    parser.add_argument("--summary", default="Send shipping notification")
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="demo only: approve automatically after one second",
    )
    arguments = parser.parse_args(argv)

    clock = SystemClock()
    store = SqliteStore(arguments.db, clock=clock)
    gate = ApprovalGate(
        store=store,
        clock=clock,
        ids=UuidIds(),
        sync_sleeper=RealSyncSleeper(),
        async_sleeper=RealAsyncSleeper(),
    )

    proposal = gate.propose(
        workflow_id="order-1234-shipping",
        action=ACTION,
        summary=arguments.summary,
        requested_by="agent:fulfilment",
        ttl=timedelta(minutes=5),
        dedupe_key="order-1234-shipping-notification",
    )
    request = proposal.request
    print(f"request: {request.id}")
    print(f"digest: {request.action_digest}")
    print(f"summary: {request.summary}")
    print(f"status: {request.status.value}")

    if proposal.deduplicated:
        print("reused an existing pending proposal with the same dedupe key")

    if arguments.auto_approve:
        _schedule_demo_approval(gate, request.id, request.action_digest)

    print("waiting for a human decision...")
    try:
        outcome = gate.wait(request.id, poll_interval=1.0, timeout=300)
    except ApprovalError as exc:
        print(f"wait failed: {exc}")
        return 1

    print(f"decision: {outcome.status.value}")
    if outcome.status is not Status.APPROVED:
        print(f"reason: {outcome.reason}")
        return 1

    verified = gate.verified_action(outcome)
    print(f"verified digest: {verified.action_digest}")
    print("executing exactly the verified canonical payload:")
    print(json.dumps(thaw_json(verified.action), indent=2, sort_keys=True))

    # The executor should use the verified bytes or frozen snapshot.  It must
    # not re-read or mutate a caller-owned dictionary and execute that instead.
    payload = json.loads(verified.canonical_json)
    assert payload == thaw_json(verified.action)
    execute(payload)
    return 0


def _schedule_demo_approval(
    gate: ApprovalGate,
    request_id: str,
    digest: str,
) -> None:
    def approve_later() -> None:
        time.sleep(1)
        try:
            outcome = gate.approve(
                request_id,
                responder="demo-human@corp",
                expected_digest=digest,
                idempotency_key=f"auto-approve-{request_id}",
                reason="auto-approved for the demo",
            )
            print(f"demo approval applied: {outcome.status.value}")
        except ApprovalError as exc:
            print(f"demo approval failed: {exc}")

    thread = threading.Thread(target=approve_later, daemon=True)
    thread.start()


def execute(payload: dict) -> None:
    """Stand-in for a real side effect with its own downstream idempotency."""

    print(f"executed tool={payload['tool']} to={payload['to']}")


if __name__ == "__main__":
    raise SystemExit(main())
