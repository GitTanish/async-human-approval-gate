"""Async human approval gate: durable decisions bound to exact payloads."""

from __future__ import annotations

from .clock import (
    AsyncSleeper,
    Clock,
    FakeAsyncSleeper,
    FakeSyncSleeper,
    IdGenerator,
    ManualClock,
    RealAsyncSleeper,
    RealSyncSleeper,
    SeqIds,
    SyncSleeper,
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
    RequestNotApproved,
    StillPending,
    StoreBusy,
)
from .gate import ApprovalGate
from .idempotency import operation_fingerprint
from .models import (
    TERMINAL,
    Outcome,
    PendingPage,
    Proposal,
    Request,
    Status,
    Transition,
    VerifiedAction,
)
from .payload import (
    FrozenJSON,
    canonicalize_action,
    freeze_canonical_action,
    sha256_hex,
    thaw_json,
)
from .state import Decision, Event, decide
from .stores import (
    MemoryStore,
    NewRequest,
    ProposeResult,
    ResponseCommand,
    ResponseKind,
    ResponseResult,
    SqliteStore,
    Store,
    SweepResult,
)

__all__ = [
    # clock and dependencies
    "Clock",
    "SystemClock",
    "ManualClock",
    "IdGenerator",
    "UuidIds",
    "SeqIds",
    "SyncSleeper",
    "AsyncSleeper",
    "RealSyncSleeper",
    "RealAsyncSleeper",
    "FakeSyncSleeper",
    "FakeAsyncSleeper",
    # errors
    "ApprovalError",
    "RequestNotFound",
    "InvalidRequest",
    "IdempotencyConflict",
    "AlreadyDecided",
    "RequestExpired",
    "RequestCancelled",
    "DigestMismatch",
    "RequestNotApproved",
    "IntegrityError",
    "StillPending",
    "StoreBusy",
    # models
    "Status",
    "TERMINAL",
    "Request",
    "Transition",
    "Outcome",
    "VerifiedAction",
    "Proposal",
    "PendingPage",
    # payload
    "FrozenJSON",
    "canonicalize_action",
    "freeze_canonical_action",
    "sha256_hex",
    "thaw_json",
    # facade
    "ApprovalGate",
    "operation_fingerprint",
    # stores
    "Store",
    "MemoryStore",
    "SqliteStore",
    "NewRequest",
    "ProposeResult",
    "SweepResult",
    "ResponseCommand",
    "ResponseKind",
    "ResponseResult",
    # reducer
    "Event",
    "Decision",
    "decide",
]
