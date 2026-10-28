"""In-memory store for tests and examples.

It follows the same contract as the SQLite store, using one lock in place of
database transactions.
"""

from __future__ import annotations

from dataclasses import replace
from threading import RLock

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

__all__ = ["MemoryStore"]


class MemoryStore(Store):
    """A thread-safe in-process implementation of the store contract."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._lock = RLock()
        self._requests: dict[str, Request] = {}
        self._transitions: dict[str, Transition] = {}
        self._next_seq = 0

    def propose(self, command: NewRequest) -> ProposeResult:
        with self._lock:
            now = self._clock.now_ms()
            transitions: list[Transition] = []

            if command.dedupe_key is not None:
                existing = self._find_pending_dedupe(
                    command.workflow_id,
                    command.dedupe_key,
                )
                if existing is not None:
                    if existing.expires_at_ms > now:
                        return ProposeResult(
                            request=existing,
                            deduplicated=True,
                            transitions=tuple(transitions),
                        )
                    transitions.append(self._expire_locked(existing, now))

            if command.id in self._requests:
                raise IntegrityError("request id already exists")

            request = self._build_request(command, now)
            self._requests[request.id] = request
            return ProposeResult(
                request=request,
                deduplicated=False,
                transitions=tuple(transitions),
            )

    def get(self, request_id: str) -> Request | None:
        with self._lock:
            return self._requests.get(request_id)

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
        with self._lock:
            rows = [
                request
                for request in self._requests.values()
                if (status is None or request.status is status)
                and (
                    workflow_id is None
                    or request.workflow_id == workflow_id
                )
                and (
                    expires_after_ms is None
                    or request.expires_at_ms > expires_after_ms
                )
            ]
        rows.sort(key=lambda request: (-request.created_at_ms, request.id))
        return rows[:limit]

    def outcome(self, request_id: str) -> Outcome | None:
        with self._lock:
            request = self._requests.get(request_id)
            if request is None or request.status is Status.PENDING:
                return None
            transition = self._transitions.get(request_id)
            if transition is None:
                raise IntegrityError("terminal request has no transition")
            return outcome_from_transition(request, transition)

    def get_transition(self, request_id: str) -> Transition | None:
        with self._lock:
            return self._transitions.get(request_id)

    def sweep(self, *, request_id: str | None = None) -> SweepResult:
        with self._lock:
            now = self._clock.now_ms()
            if request_id is not None:
                request = self._requests.get(request_id)
                if (
                    request is None
                    or request.status is not Status.PENDING
                    or request.expires_at_ms > now
                ):
                    return SweepResult()
                transition = self._expire_locked(request, now)
                return SweepResult(transitions=(transition,))

            due = sorted(
                (
                    request
                    for request in self._requests.values()
                    if request.status is Status.PENDING
                    and request.expires_at_ms <= now
                ),
                key=lambda request: (request.created_at_ms, request.id),
            )
            transitions = tuple(
                self._expire_locked(request, now) for request in due
            )
            return SweepResult(transitions=transitions)

    def respond(self, command: ResponseCommand) -> ResponseResult:
        with self._lock:
            now = self._clock.now_ms()
            request = self._requests.get(command.request_id)
            if request is None:
                return ResponseResult(ResponseKind.NOT_FOUND)

            prior = self._transitions.get(request.id)
            if (
                prior is not None
                and prior.idempotency_key == command.idempotency_key
            ):
                if prior.request_fingerprint != command.fingerprint:
                    return ResponseResult(ResponseKind.IDEMPOTENCY_CONFLICT)
                return ResponseResult(
                    kind=ResponseKind.REPLAYED,
                    outcome=outcome_from_transition(
                        request,
                        prior,
                        replayed=True,
                    ),
                )

            if request.status is not Status.PENDING:
                return self._classify_locked(request)

            if now >= request.expires_at_ms:
                transition = self._expire_locked(request, now)
                updated = self._requests[request.id]
                return ResponseResult(
                    kind=ResponseKind.EXPIRED,
                    outcome=outcome_from_transition(updated, transition),
                    transition=transition,
                )

            if (
                command.operation is not Event.CANCEL
                and command.expected_digest != request.action_digest
            ):
                return ResponseResult(ResponseKind.DIGEST_MISMATCH)

            target = target_status(command.operation)
            updated = replace(
                request,
                status=target,
                version=request.version + 1,
            )
            self._requests[request.id] = updated
            transition = self._new_transition(
                request_id=request.id,
                to_status=target,
                actor=command.actor,
                reason=command.reason,
                at_ms=now,
                idempotency_key=command.idempotency_key,
                request_fingerprint=command.fingerprint,
            )
            return ResponseResult(
                kind=ResponseKind.APPLIED,
                outcome=outcome_from_transition(updated, transition),
                transition=transition,
            )

    def close(self) -> None:
        """The memory store owns no external resources."""

    def _build_request(self, command: NewRequest, now_ms: int) -> Request:
        if command.ttl_ms <= 0:
            raise IntegrityError("ttl must be positive")
        canonical = bytes(command.canonical_json)
        if sha256_hex(canonical) != command.action_digest:
            raise IntegrityError("action digest does not match canonical bytes")
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

    def _find_pending_dedupe(
        self,
        workflow_id: str,
        dedupe_key: str,
    ) -> Request | None:
        for request in self._requests.values():
            if (
                request.status is Status.PENDING
                and request.workflow_id == workflow_id
                and request.dedupe_key == dedupe_key
            ):
                return request
        return None

    def _expire_locked(self, request: Request, now_ms: int) -> Transition:
        updated = replace(
            request,
            status=Status.EXPIRED,
            version=request.version + 1,
        )
        self._requests[request.id] = updated
        return self._new_transition(
            request_id=request.id,
            to_status=Status.EXPIRED,
            actor=EXPIRY_ACTOR,
            reason=EXPIRY_REASON,
            at_ms=now_ms,
            idempotency_key=None,
            request_fingerprint=None,
        )

    def _new_transition(
        self,
        *,
        request_id: str,
        to_status: Status,
        actor: str,
        reason: str | None,
        at_ms: int,
        idempotency_key: str | None,
        request_fingerprint: str | None,
    ) -> Transition:
        if request_id in self._transitions:
            raise IntegrityError("transition already exists")
        self._next_seq += 1
        transition = Transition(
            seq=self._next_seq,
            request_id=request_id,
            from_status=Status.PENDING,
            to_status=to_status,
            actor=actor,
            reason=reason,
            at_ms=at_ms,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        self._transitions[request_id] = transition
        return transition

    def _classify_locked(self, request: Request) -> ResponseResult:
        if request.status is Status.PENDING:
            raise StoreBusy("request changed concurrently; retry")
        transition = self._transitions.get(request.id)
        if transition is None:
            raise IntegrityError("terminal request has no transition")
        return ResponseResult(
            kind=response_kind_for_terminal(request.status),
            outcome=outcome_from_transition(request, transition),
        )
