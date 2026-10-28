"""Demonstration FastAPI adapter for the approval gate.

This is not a production service. Authentication is a static bearer-token map,
and the self-approval/cancellation checks show where adapter policy belongs.
"""

import json
import logging
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request as HttpRequest
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from approval_gate.clock import (
    Clock,
    RealAsyncSleeper,
    RealSyncSleeper,
    SystemClock,
    UuidIds,
)
from approval_gate.errors import (
    AlreadyDecided,
    ApprovalError,
    DigestMismatch,
    IdempotencyConflict,
    IntegrityError,
    InvalidRequest,
    RequestCancelled,
    RequestExpired,
    RequestNotFound,
    RequestNotApproved,
    StoreBusy,
)
from approval_gate.gate import ApprovalGate
from approval_gate.models import Status
from approval_gate.serialization import outcome_to_dict, request_to_dict
from approval_gate.stores.sqlite import SqliteStore

__all__ = ["create_app", "app_factory", "DEFAULT_TOKENS"]

_LOG = logging.getLogger(__name__)

DEFAULT_TOKENS: dict[str, str] = {
    "demo-agent-token": "agent:planner",
    "demo-alice-token": "alice@corp",
    "demo-bob-token": "bob@corp",
}

_ERROR_STATUS: dict[type[ApprovalError], int] = {
    InvalidRequest: 400,
    RequestNotFound: 404,
    IdempotencyConflict: 409,
    AlreadyDecided: 409,
    RequestExpired: 410,
    RequestCancelled: 410,
    DigestMismatch: 412,
    RequestNotApproved: 409,
    IntegrityError: 500,
    StoreBusy: 503,
}


class CreateApprovalBody(BaseModel):
    workflow_id: str
    action: Any
    summary: str
    ttl_seconds: float = 3_600.0
    dedupe_key: str | None = None


class ApproveBody(BaseModel):
    expected_digest: str
    reason: str | None = None


class RejectBody(BaseModel):
    expected_digest: str
    reason: str | None = None


class CancelBody(BaseModel):
    reason: str | None = None


def create_app(
    *,
    db_path: str | Path | None = None,
    tokens: Mapping[str, str] | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    resolved_path = db_path or os.environ.get(
        "APPROVAL_GATE_DB",
        "approvals.db",
    )
    resolved_tokens = dict(tokens) if tokens is not None else _tokens_from_env()
    resolved_clock = clock or SystemClock()

    store = SqliteStore(resolved_path, clock=resolved_clock)
    gate = ApprovalGate(
        store=store,
        clock=resolved_clock,
        ids=UuidIds(),
        sync_sleeper=RealSyncSleeper(),
        async_sleeper=RealAsyncSleeper(),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        store.close()

    app = FastAPI(
        title="Async Human Approval Gate Demo",
        description=(
            "Illustrative adapter with static bearer tokens. "
            "Do not deploy as production authentication."
        ),
        lifespan=lifespan,
    )
    app.state.gate = gate
    app.state.clock = resolved_clock
    app.state.tokens = resolved_tokens

    bearer = HTTPBearer(auto_error=False)

    def authenticate(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer),
        ],
    ) -> str:
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        identity = resolved_tokens.get(credentials.credentials)
        if identity is None:
            raise HTTPException(
                status_code=401,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return identity

    Principal = Annotated[str, Depends(authenticate)]
    IdempotencyKey = Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ]

    @app.exception_handler(ApprovalError)
    async def approval_error_handler(
        _request: HttpRequest,
        exc: ApprovalError,
    ) -> JSONResponse:
        status_code = _ERROR_STATUS.get(type(exc), 500)
        message = str(exc)
        if isinstance(exc, IntegrityError):
            _LOG.exception("approval integrity failure", exc_info=exc)
            message = "internal integrity failure"
        payload: dict[str, Any] = {
            "error": {
                "code": exc.code,
                "message": message,
            },
        }
        if isinstance(
            exc,
            (AlreadyDecided, RequestExpired, RequestCancelled),
        ):
            outcome = exc.outcome
            payload["error"]["current"] = {
                "request_id": outcome.request_id,
                "status": outcome.status.value,
                "action_digest": outcome.action_digest,
                "actor": outcome.actor,
                "reason": outcome.reason,
                "at_ms": outcome.at_ms,
            }
        return JSONResponse(status_code=status_code, content=payload)

    @app.get("/approvals")
    def list_approvals(
        principal: Principal,
        workflow_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        del principal
        page = gate.pending(workflow_id=workflow_id, limit=limit)
        return {
            "items": [request_to_dict(request) for request in page.requests],
            "swept": page.swept,
        }

    @app.get("/approvals/{request_id}")
    def show_approval(
        request_id: str,
        principal: Principal,
    ) -> dict[str, Any]:
        del principal
        request = gate.get(request_id)
        if request is None:
            raise RequestNotFound(f"request {request_id!r} does not exist")
        outcome = (
            None
            if request.status is Status.PENDING
            else gate.outcome(request_id)
        )
        return {
            "request": request_to_dict(request),
            "outcome": None if outcome is None else outcome_to_dict(outcome),
        }

    @app.post("/approvals", status_code=201)
    def create_approval(
        body: CreateApprovalBody,
        principal: Principal,
    ) -> dict[str, Any]:
        ttl = _ttl_from_seconds(body.ttl_seconds)
        proposal = gate.propose(
            workflow_id=body.workflow_id,
            action=body.action,
            summary=body.summary,
            requested_by=principal,
            ttl=ttl,
            dedupe_key=body.dedupe_key,
        )
        return {
            "request": request_to_dict(proposal.request),
            "deduplicated": proposal.deduplicated,
        }

    @app.post("/approvals/{request_id}/approve")
    def approve(
        request_id: str,
        body: ApproveBody,
        principal: Principal,
        idempotency_key: IdempotencyKey = None,
    ) -> dict[str, Any]:
        _require_idempotency_key(idempotency_key)
        request = gate.get(request_id)
        if request is None:
            raise RequestNotFound(f"request {request_id!r} does not exist")
        if request.requested_by == principal:
            raise HTTPException(
                status_code=403,
                detail="the demo denies self-approval",
            )
        outcome = gate.approve(
            request_id,
            responder=principal,
            expected_digest=body.expected_digest,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
        return {
            "outcome": outcome_to_dict(outcome),
            "replayed": outcome.replayed,
        }

    @app.post("/approvals/{request_id}/reject")
    def reject(
        request_id: str,
        body: RejectBody,
        principal: Principal,
        idempotency_key: IdempotencyKey = None,
    ) -> dict[str, Any]:
        _require_idempotency_key(idempotency_key)
        if body.reason is None or not body.reason.strip():
            raise InvalidRequest("reject requires a non-empty reason")
        outcome = gate.reject(
            request_id,
            responder=principal,
            expected_digest=body.expected_digest,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
        return {
            "outcome": outcome_to_dict(outcome),
            "replayed": outcome.replayed,
        }

    @app.post("/approvals/{request_id}/cancel")
    def cancel(
        request_id: str,
        body: CancelBody,
        principal: Principal,
        idempotency_key: IdempotencyKey = None,
    ) -> dict[str, Any]:
        _require_idempotency_key(idempotency_key)
        request = gate.get(request_id)
        if request is None:
            raise RequestNotFound(f"request {request_id!r} does not exist")
        if request.requested_by != principal:
            raise HTTPException(
                status_code=403,
                detail="only the requester may cancel",
            )
        outcome = gate.cancel(
            request_id,
            actor=principal,
            idempotency_key=idempotency_key,
            reason=body.reason,
        )
        return {
            "outcome": outcome_to_dict(outcome),
            "replayed": outcome.replayed,
        }

    return app


def _require_idempotency_key(value: str | None) -> str:
    if not value:
        raise InvalidRequest("Idempotency-Key header is required")
    return value


def _ttl_from_seconds(seconds: float) -> timedelta:
    try:
        ttl = timedelta(seconds=seconds)
    except (OverflowError, ValueError) as exc:
        raise InvalidRequest("ttl_seconds is not a valid duration") from exc
    return ttl


def _tokens_from_env() -> dict[str, str]:
    raw = os.environ.get("APPROVAL_GATE_TOKENS")
    if not raw:
        return dict(DEFAULT_TOKENS)
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("APPROVAL_GATE_TOKENS must be valid JSON") from exc
    if not isinstance(loaded, dict) or not all(
        isinstance(token, str) and isinstance(identity, str)
        for token, identity in loaded.items()
    ):
        raise RuntimeError(
            "APPROVAL_GATE_TOKENS must be a JSON object of token -> identity",
        )
    return loaded


def app_factory() -> FastAPI:
    """Zero-argument factory for ``uvicorn --factory``."""

    return create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app_factory(), host="127.0.0.1", port=8000)
