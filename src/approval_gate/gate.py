"""The public approval-gate facade."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any, Callable

from .clock import (
    AsyncSleeper,
    Clock,
    IdGenerator,
    SyncSleeper,
)
from .errors import (
    AlreadyDecided,
    DigestMismatch,
    IdempotencyConflict,
    IntegrityError,
    InvalidRequest,
    RequestCancelled,
    RequestExpired,
    RequestNotFound,
    RequestNotApproved,
    StillPending,
)
from .idempotency import operation_fingerprint
from .models import (
    Outcome,
    PendingPage,
    Proposal,
    Request,
    Status,
    Transition,
    VerifiedAction,
)
from .payload import (
    canonicalize_action,
    freeze_canonical_action,
    sha256_hex,
)
from .state import Event
from .stores.base import (
    NewRequest,
    ResponseCommand,
    ResponseKind,
    ResponseResult,
    Store,
)

__all__ = ["ApprovalGate"]

_LOG = logging.getLogger(__name__)

_FULL_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")
_MAX_SUMMARY = 2_000
_MAX_REASON = 2_000
_MAX_IDEMPOTENCY_KEY = 200
_ONE_MILLISECOND = timedelta(milliseconds=1)

TransitionCallback = Callable[[Transition], None]


class ApprovalGate:
    """Coordinates validation, storage, waiting, and payload verification.

    The store owns transactions and races.  This facade validates inputs,
    canonicalizes actions, computes idempotency fingerprints, translates store
    results into outcomes or typed errors, and never holds a transaction open
    across a wait or callback.
    """

    def __init__(
        self,
        *,
        store: Store,
        clock: Clock,
        ids: IdGenerator,
        sync_sleeper: SyncSleeper,
        async_sleeper: AsyncSleeper,
        poll_interval: float = 1.0,
        on_transition: TransitionCallback | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._sync_sleeper = sync_sleeper
        self._async_sleeper = async_sleeper
        self._poll_interval = _positive_seconds(
            poll_interval,
            "poll_interval",
        )
        self._on_transition = on_transition

    def propose(
        self,
        *,
        workflow_id: str,
        action: Mapping[str, Any],
        summary: str,
        requested_by: str,
        ttl: timedelta,
        dedupe_key: str | None = None,
    ) -> Proposal:
        _require_text(workflow_id, "workflow_id")
        _require_text(summary, "summary", max_length=_MAX_SUMMARY)
        _require_text(requested_by, "requested_by")
        if dedupe_key is not None:
            _require_text(dedupe_key, "dedupe_key")
        ttl_ms = _ttl_milliseconds(ttl)
        canonical = canonicalize_action(action)

        result = self._store.propose(
            NewRequest(
                id=self._ids.new_request_id(),
                workflow_id=workflow_id,
                dedupe_key=dedupe_key,
                canonical_json=canonical,
                action_digest=sha256_hex(canonical),
                summary=summary,
                requested_by=requested_by,
                ttl_ms=ttl_ms,
            ),
        )
        self._notify(result.transitions)
        return Proposal(
            request=result.request,
            deduplicated=result.deduplicated,
        )

    def get(self, request_id: str) -> Request | None:
        request = self._store.get(request_id)
        if request is None:
            return None
        now_ms = self._clock.now_ms()
        return replace(request, overdue=request.overdue_at(now_ms))

    def list(
        self,
        *,
        status: Status | None = None,
        workflow_id: str | None = None,
        limit: int = 100,
    ) -> list[Request]:
        status = _optional_status(status)
        if workflow_id is not None:
            _require_text(workflow_id, "workflow_id")
        _validate_limit(limit)
        now_ms = self._clock.now_ms()
        rows = self._store.list(
            status=status,
            workflow_id=workflow_id,
            limit=limit,
        )
        return [
            replace(request, overdue=request.overdue_at(now_ms))
            for request in rows
        ]

    def outcome(self, request_id: str) -> Outcome | None:
        """Read the stored terminal outcome without sweeping.

        Returns ``None`` for a missing, pending, or overdue-but-unswept
        request.
        """

        return self._store.outcome(request_id)

    def pending(
        self,
        *,
        workflow_id: str | None = None,
        limit: int = 100,
    ) -> PendingPage:
        """Sweep overdue requests, then list only pending unexpired ones.

        Unlike ``get`` and ``list``, this method writes: it records expiry
        transitions before returning the page.
        """

        if workflow_id is not None:
            _require_text(workflow_id, "workflow_id")
        _validate_limit(limit)
        swept = self._store.sweep()
        self._notify(swept.transitions)
        now_ms = self._clock.now_ms()
        rows = self._store.list(
            status=Status.PENDING,
            workflow_id=workflow_id,
            limit=limit,
            expires_after_ms=now_ms,
        )
        return PendingPage(requests=rows, swept=swept.count)

    def sweep(self, *, request_id: str | None = None) -> int:
        if request_id is not None:
            _require_text(request_id, "request_id")
        result = self._store.sweep(request_id=request_id)
        self._notify(result.transitions)
        return result.count

    def approve(
        self,
        request_id: str,
        *,
        responder: str,
        expected_digest: str,
        idempotency_key: str,
        reason: str | None = None,
    ) -> Outcome:
        _require_text(request_id, "request_id")
        _require_text(responder, "responder")
        _require_text(
            idempotency_key,
            "idempotency_key",
            max_length=_MAX_IDEMPOTENCY_KEY,
        )
        _validate_digest(expected_digest)
        _validate_reason(reason)
        return self._respond(
            request_id=request_id,
            operation=Event.APPROVE,
            actor=responder,
            reason=reason,
            expected_digest=expected_digest,
            idempotency_key=idempotency_key,
        )

    def reject(
        self,
        request_id: str,
        *,
        responder: str,
        expected_digest: str,
        idempotency_key: str,
        reason: str | None = None,
    ) -> Outcome:
        _require_text(request_id, "request_id")
        _require_text(responder, "responder")
        _require_text(
            idempotency_key,
            "idempotency_key",
            max_length=_MAX_IDEMPOTENCY_KEY,
        )
        _validate_digest(expected_digest)
        _validate_reason(reason)
        return self._respond(
            request_id=request_id,
            operation=Event.REJECT,
            actor=responder,
            reason=reason,
            expected_digest=expected_digest,
            idempotency_key=idempotency_key,
        )

    def cancel(
        self,
        request_id: str,
        *,
        actor: str,
        idempotency_key: str,
        reason: str | None = None,
    ) -> Outcome:
        _require_text(request_id, "request_id")
        _require_text(actor, "actor")
        _require_text(
            idempotency_key,
            "idempotency_key",
            max_length=_MAX_IDEMPOTENCY_KEY,
        )
        _validate_reason(reason)
        return self._respond(
            request_id=request_id,
            operation=Event.CANCEL,
            actor=actor,
            reason=reason,
            expected_digest=None,
            idempotency_key=idempotency_key,
        )

    def wait(
        self,
        request_id: str,
        *,
        poll_interval: float | None = None,
        timeout: float | None = None,
    ) -> Outcome:
        _require_text(request_id, "request_id")
        interval = self._resolve_poll_interval(poll_interval)
        deadline_ms = self._caller_deadline_ms(timeout)

        while True:
            request = self._store.get(request_id)
            if request is None:
                raise RequestNotFound(f"request {request_id!r} does not exist")
            if request.status is not Status.PENDING:
                return self._stored_outcome(request_id)

            now_ms = self._clock.now_ms()
            if now_ms >= request.expires_at_ms:
                swept = self._store.sweep(request_id=request_id)
                self._notify(swept.transitions)
                continue

            sleep_seconds = interval
            if deadline_ms is not None:
                now_ms = self._clock.now_ms()
                if now_ms >= deadline_ms:
                    raise StillPending(
                        f"request {request_id!r} is still pending",
                    )
                sleep_seconds = min(
                    interval,
                    (deadline_ms - now_ms) / 1_000.0,
                )
            self._sync_sleeper.sleep(sleep_seconds)

    async def wait_async(
        self,
        request_id: str,
        *,
        poll_interval: float | None = None,
        timeout: float | None = None,
    ) -> Outcome:
        _require_text(request_id, "request_id")
        interval = self._resolve_poll_interval(poll_interval)
        deadline_ms = self._caller_deadline_ms(timeout)

        while True:
            request = await asyncio.to_thread(self._store.get, request_id)
            if request is None:
                raise RequestNotFound(f"request {request_id!r} does not exist")
            if request.status is not Status.PENDING:
                return await asyncio.to_thread(
                    self._stored_outcome,
                    request_id,
                )

            now_ms = self._clock.now_ms()
            if now_ms >= request.expires_at_ms:
                swept = await asyncio.to_thread(
                    self._store.sweep,
                    request_id=request_id,
                )
                self._notify(swept.transitions)
                continue

            sleep_seconds = interval
            if deadline_ms is not None:
                now_ms = self._clock.now_ms()
                if now_ms >= deadline_ms:
                    raise StillPending(
                        f"request {request_id!r} is still pending",
                    )
                sleep_seconds = min(
                    interval,
                    (deadline_ms - now_ms) / 1_000.0,
                )
            await self._async_sleeper.sleep(sleep_seconds)

    def verified_action(self, outcome: Outcome) -> VerifiedAction:
        if outcome.status is not Status.APPROVED:
            raise RequestNotApproved(
                f"request {outcome.request_id!r} is not approved",
            )
        request = self._store.get(outcome.request_id)
        if request is None:
            raise RequestNotFound(
                f"request {outcome.request_id!r} does not exist",
            )
        if request.status is not Status.APPROVED:
            raise RequestNotApproved(
                f"request {outcome.request_id!r} is not approved",
            )
        if outcome.action_digest != request.action_digest:
            raise IntegrityError(
                "outcome digest does not match the stored action digest",
            )

        canonical = request.canonical_json
        if sha256_hex(canonical) != request.action_digest:
            raise IntegrityError(
                "stored action bytes do not match the stored digest",
            )
        action = freeze_canonical_action(canonical)
        return VerifiedAction(
            request_id=request.id,
            action=action,
            canonical_json=canonical,
            action_digest=request.action_digest,
        )

    def _respond(
        self,
        *,
        request_id: str,
        operation: Event,
        actor: str,
        reason: str | None,
        expected_digest: str | None,
        idempotency_key: str,
    ) -> Outcome:
        fingerprint = operation_fingerprint(
            request_id=request_id,
            operation=operation,
            actor=actor,
            reason=reason,
            expected_digest=expected_digest,
        )
        result = self._store.respond(
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
        if result.transition is not None:
            self._notify((result.transition,))
        return self._translate(result)

    def _translate(self, result: ResponseResult) -> Outcome:
        if result.kind in (ResponseKind.APPLIED, ResponseKind.REPLAYED):
            if result.outcome is None:
                raise IntegrityError("store returned no outcome")
            return result.outcome
        if result.kind is ResponseKind.NOT_FOUND:
            raise RequestNotFound("request does not exist")
        if result.kind is ResponseKind.IDEMPOTENCY_CONFLICT:
            raise IdempotencyConflict(
                "idempotency key was already used for a different operation",
            )
        if result.kind is ResponseKind.DIGEST_MISMATCH:
            raise DigestMismatch(
                "expected_digest does not match the stored action digest",
            )
        if result.outcome is None:
            raise IntegrityError("terminal store result returned no outcome")
        if result.kind is ResponseKind.ALREADY_DECIDED:
            raise AlreadyDecided(result.outcome)
        if result.kind is ResponseKind.EXPIRED:
            raise RequestExpired(result.outcome)
        if result.kind is ResponseKind.CANCELLED:
            raise RequestCancelled(result.outcome)
        raise IntegrityError(f"unknown store result: {result.kind}")

    def _stored_outcome(self, request_id: str) -> Outcome:
        outcome = self._store.outcome(request_id)
        if outcome is None:
            raise IntegrityError(
                f"terminal request {request_id!r} has no stored outcome",
            )
        return outcome

    def _notify(self, transitions: tuple[Transition, ...]) -> None:
        if self._on_transition is None:
            return
        for transition in transitions:
            try:
                self._on_transition(transition)
            except Exception:
                _LOG.exception(
                    "on_transition callback failed for request %s",
                    transition.request_id,
                )

    def _resolve_poll_interval(self, poll_interval: float | None) -> float:
        if poll_interval is None:
            return self._poll_interval
        return _positive_seconds(poll_interval, "poll_interval")

    def _caller_deadline_ms(self, timeout: float | None) -> int | None:
        if timeout is None:
            return None
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise InvalidRequest(
                "timeout must be a finite, non-negative number",
            )
        return self._clock.now_ms() + round(timeout * 1_000)


def _require_text(
    value: object,
    field: str,
    *,
    max_length: int | None = None,
) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidRequest(f"{field} must be a non-empty string")
    if max_length is not None and len(value) > max_length:
        raise InvalidRequest(
            f"{field} must be at most {max_length} characters",
        )
    return value


def _validate_reason(reason: str | None) -> None:
    if reason is None:
        return
    if not isinstance(reason, str):
        raise InvalidRequest("reason must be a string or None")
    if len(reason) > _MAX_REASON:
        raise InvalidRequest(
            f"reason must be at most {_MAX_REASON} characters",
        )


def _validate_digest(digest: object) -> str:
    if not isinstance(digest, str) or not _FULL_DIGEST.fullmatch(digest):
        raise InvalidRequest(
            "expected_digest must be a full 64-character lowercase "
            "SHA-256 hex digest",
        )
    return digest


def _validate_limit(limit: int) -> None:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 0
    ):
        raise InvalidRequest("limit must be a non-negative integer")


def _optional_status(status: Status | None) -> Status | None:
    if status is None:
        return None
    try:
        return Status(status)
    except (TypeError, ValueError):
        raise InvalidRequest(f"invalid status: {status!r}") from None


def _ttl_milliseconds(ttl: timedelta) -> int:
    if not isinstance(ttl, timedelta):
        raise InvalidRequest("ttl must be a timedelta")
    if ttl <= timedelta(0):
        raise InvalidRequest("ttl must be positive")
    milliseconds = ttl // _ONE_MILLISECOND
    if ttl % _ONE_MILLISECOND:
        milliseconds += 1
    if milliseconds < 1:
        raise InvalidRequest("ttl must be at least one millisecond")
    return milliseconds


def _positive_seconds(value: object, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise InvalidRequest(f"{field} must be a positive number")
    return float(value)
