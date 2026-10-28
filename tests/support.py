from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from approval_gate.clock import FakeAsyncSleeper, FakeSyncSleeper, ManualClock
from approval_gate.gate import ApprovalGate
from approval_gate.idempotency import operation_fingerprint
from approval_gate.models import Proposal, Request, Status
from approval_gate.payload import canonicalize_action, sha256_hex
from approval_gate.state import Event
from approval_gate.stores import NewRequest, ProposeResult, ResponseResult, Store


def make_request(
    *,
    status: Status = Status.PENDING,
    expires_at_ms: int = 10_000,
    created_at_ms: int = 0,
    version: int = 0,
) -> Request:
    canonical = b"{}"
    return Request(
        id="req_1",
        workflow_id="wf_1",
        dedupe_key=None,
        status=status,
        action=MappingProxyType({}),
        canonical_json=canonical,
        action_digest=hashlib.sha256(canonical).hexdigest(),
        summary="Send the customer email",
        requested_by="agent:planner",
        created_at_ms=created_at_ms,
        expires_at_ms=expires_at_ms,
        version=version,
    )


@dataclass
class StoreHarness:
    """Convenience wrappers so contract tests read as behavior, not plumbing."""

    store: Store
    clock: ManualClock
    _next_request: int = field(default=0, init=False)
    _next_key: int = field(default=0, init=False)

    def propose(
        self,
        *,
        action: dict[str, Any] | None = None,
        workflow_id: str = "wf_1",
        summary: str = "Send the customer email",
        requested_by: str = "agent:planner",
        ttl_ms: int = 60_000,
        dedupe_key: str | None = None,
        request_id: str | None = None,
    ) -> ProposeResult:
        if request_id is None:
            self._next_request += 1
            request_id = f"req_{self._next_request}"
        canonical = canonicalize_action(action or {})
        return self.store.propose(
            NewRequest(
                id=request_id,
                workflow_id=workflow_id,
                dedupe_key=dedupe_key,
                canonical_json=canonical,
                action_digest=sha256_hex(canonical),
                summary=summary,
                requested_by=requested_by,
                ttl_ms=ttl_ms,
            ),
        )

    def respond(
        self,
        request_id: str,
        operation: Event,
        *,
        actor: str = "alice@corp",
        reason: str | None = None,
        expected_digest: str | None = None,
        idempotency_key: str | None = None,
        fingerprint: str | None = None,
    ) -> ResponseResult:
        request = self.store.get(request_id)
        if request is None:
            raise AssertionError(f"test request {request_id!r} does not exist")
        if expected_digest is None and operation is not Event.CANCEL:
            expected_digest = request.action_digest
        if idempotency_key is None:
            self._next_key += 1
            idempotency_key = f"key_{self._next_key}"
        if fingerprint is None:
            fingerprint = operation_fingerprint(
                request_id=request_id,
                operation=operation,
                actor=actor,
                reason=reason,
                expected_digest=(
                    None if operation is Event.CANCEL else expected_digest
                ),
            )
        from approval_gate.stores import ResponseCommand

        return self.store.respond(
            ResponseCommand(
                request_id=request_id,
                operation=operation,
                actor=actor,
                reason=reason,
                expected_digest=expected_digest,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            ),
        )


@dataclass
class GateEnv:
    gate: ApprovalGate
    store: Store
    clock: ManualClock
    sync_sleeper: FakeSyncSleeper
    async_sleeper: FakeAsyncSleeper

    def propose(
        self,
        *,
        action: dict[str, Any] | None = None,
        workflow_id: str = "wf_1",
        summary: str = "Send the customer email",
        requested_by: str = "agent:planner",
        ttl: timedelta = timedelta(seconds=60),
        dedupe_key: str | None = None,
    ) -> Proposal:
        return self.gate.propose(
            workflow_id=workflow_id,
            action=action or {},
            summary=summary,
            requested_by=requested_by,
            ttl=ttl,
            dedupe_key=dedupe_key,
        )
