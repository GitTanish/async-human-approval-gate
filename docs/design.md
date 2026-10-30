# Async Human Approval Gate — Design

Status: Implemented (V1)  
Scope: V1  
Last updated: 2026-10-30

## Contents

1. [Summary](#1-summary)
2. [Goals and non-goals](#2-goals-and-non-goals)
3. [Design decisions at a glance](#3-design-decisions-at-a-glance)
4. [Domain model](#4-domain-model)
5. [State machine](#5-state-machine)
6. [Operations and read/write semantics](#6-operations-and-readwrite-semantics)
7. [Idempotency](#7-idempotency)
8. [Expiry](#8-expiry)
9. [Storage](#9-storage)
10. [Facade API](#10-facade-api)
11. [Human interfaces](#11-human-interfaces)
12. [Determinism and testing](#12-determinism-and-testing)
13. [Failure modes](#13-failure-modes)
14. [Evolution](#14-evolution)
15. [Package layout](#15-package-layout)
16. [Milestones](#16-milestones)
17. [Decision log](#17-decision-log)
18. [Deferred](#18-deferred)

## 1. Summary

A small, dependency-free Python library that lets an agent propose an action, persist an approval request, and resume only after a human approves or rejects it. SQLite is the durable store. V1 supports one approver.

The core is deterministic: time, IDs, and sleeping are injected. The database transition is authoritative. Approval is bound to the exact action digest, expiry is fail-closed, and duplicate responses are resolved by an idempotency fingerprint rather than by hoping the first write wins.

The gate guarantees a single durable decision and idempotent retries of that decision. It does not make external side effects exactly-once; execution idempotency remains the caller’s responsibility.

## 2. Goals and non-goals

### Goals

- Durable pending requests that survive process restarts.
- Explicit, exhaustively testable state transitions.
- Correct behavior under concurrent responders and multiple processes.
- Deterministic tests with no real sleeps or wall-clock reads.
- No runtime dependencies in the core.
- A terminal Approval Console, a thin CLI, an agent-side bridge, and FastAPI adapters for the two sides of the flow.
- Fail-closed expiry: a request that times out is never approved.
- TOCTOU protection: an approval is bound to the exact action payload the human saw.

### Non-goals for V1

- Multi-approver policies, quorum, or escalation chains.
- Production-grade authentication, authorization, identity integration, a web UI, notifications, always-allow rules, or batch approval.
- Automatic approval on timeout.
- Exactly-once execution of the approved side effect.
- A policy DSL, multi-tenancy, or a hosted service.
- PostgreSQL, Redis, pub/sub wakeups, or long-poll HTTP.
- Data retention and archival tooling.

The FastAPI demo includes minimal bearer-token authentication and a self-approval check to show where those responsibilities belong. They are examples, not production security.

Those are extension points, not forgotten requirements. See [Evolution](#14-evolution).

## 3. Design decisions at a glance

| Area | Decision |
|---|---|
| Persistence | SQLite, WAL mode, one file |
| Approvers | Single approver in V1 |
| Operational state | `requests.status` |
| Audit history | Append-only `transitions` table |
| Time | Injected clock; UTC epoch milliseconds |
| Deadline boundary | `now >= expires_at_ms` is expired |
| Expiry authority | Conditional database update inside the write transaction |
| Store reads | Pure; no writes |
| `pending()` | Sweeps overdue rows first, then filters by deadline |
| Response replay | Requires an idempotency key plus an operation fingerprint |
| Action payload | Immutable canonical bytes plus a recursive frozen snapshot |
| Audit integrity | Append-only triggers plus immutable request fields |
| Core dependencies | None (standard library only) |
| Waiting | Polling with separate sync/async sleepers; blocking store work offloaded |
| Human interfaces | Terminal Approval Console, thin CLI, and FastAPI demo |
| Agent integration | Non-blocking propose/status/wait bridge; execution stays caller-owned |

## 4. Domain model

### 4.1 Status

```python
class Status(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

TERMINAL = {Status.APPROVED, Status.REJECTED, Status.EXPIRED, Status.CANCELLED}
```

`PENDING` is the only non-terminal state. Once terminal, a request never changes state again.

### 4.2 Request

The stored request is the operational state plus the action the human is being asked to approve.

```python
FrozenJSON: TypeAlias = (
    Mapping[str, "FrozenJSON"]
    | tuple["FrozenJSON", ...]
    | str | int | float | bool | None
)

@dataclass(frozen=True)
class Request:
    id: str
    workflow_id: str
    dedupe_key: str | None
    status: Status
    action: FrozenJSON             # recursive immutable snapshot
    canonical_json: bytes          # UTF-8 bytes of the stored action_json
    action_digest: str             # SHA-256 of canonical_json
    summary: str
    requested_by: str
    created_at_ms: int
    expires_at_ms: int
    version: int
    overdue: bool = False          # derived by the facade; never stored
```

`FrozenJSON` uses read-only mappings and tuples for arrays, so nested values cannot be mutated through the returned request. `thaw_json()` produces a fresh mutable copy for adapters that need one.

`overdue` is true when `status is PENDING and now_ms >= expires_at_ms`. It is informational only. It does not change the stored status.

### 4.3 Transition

`transitions` is the append-only audit history. It records every applied state change and carries the idempotency evidence for user responses.

```python
@dataclass(frozen=True)
class Transition:
    seq: int
    request_id: str
    from_status: Status
    to_status: Status
    actor: str
    reason: str | None
    at_ms: int
    idempotency_key: str | None
    request_fingerprint: str | None
```

Expiry transitions have no idempotency key or fingerprint. Replays do not create transitions.

### 4.4 Outcome

`Outcome` is the terminal result returned by `approve`, `reject`, `cancel`, `wait`, and `outcome`.

```python
@dataclass(frozen=True)
class Outcome:
    request_id: str
    status: Status                 # one of the terminal statuses
    action_digest: str
    actor: str | None              # responder, canceller, or "system:expiry"
    reason: str | None
    at_ms: int | None              # observation/decision time
    replayed: bool = False         # true when this result came from a replay
```

`VerifiedAction` is the execution-safe payload returned by `verified_action()`. It contains the exact canonical bytes that were hashed, plus an immutable parsed snapshot:

```python
@dataclass(frozen=True)
class VerifiedAction:
    request_id: str
    action: FrozenJSON
    canonical_json: bytes
    action_digest: str
```

### 4.5 Proposal

```python
@dataclass(frozen=True)
class Proposal:
    request: Request
    deduplicated: bool             # true when an existing pending request was reused

@dataclass(frozen=True)
class PendingPage:
    requests: list[Request]
    swept: int                     # number expired by the preceding sweep
```

### 4.6 Action, digest, and input validation

- `action` must be a top-level JSON object. Scalars and arrays are rejected at the API boundary.
- Object keys must be strings; non-string keys are rejected rather than coerced.
- Values must be JSON-compatible. `allow_nan=False` rejects NaN and infinity.
- Strings are UTF-8 encoded without Unicode normalization; differently encoded strings have different digests.
- Canonical JSON is produced once, server-side, when the request is proposed:

```python
canonical = json.dumps(
    action,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
).encode("utf-8")
action_digest = hashlib.sha256(canonical).hexdigest()
```

- The exact `canonical` bytes are stored as UTF-8 text in `action_json`; the digest is computed from those bytes, not recomputed later from a caller-owned object.
- `propose()` does not retain or re-read the caller’s mutable mapping. It stores the canonical bytes and builds a `FrozenJSON` snapshot from them. Mutating the original input after `propose()` has no effect.
- `Request.action` is always the frozen snapshot. Nested dictionaries are read-only mappings; arrays are tuples. Attempts to mutate them raise `TypeError`.
- `thaw_json()` is available for adapters that need a plain mutable structure. It returns a fresh copy and is not used for verification.
- `verified_action(outcome)` re-hashes the exact stored `action_json` bytes, checks the canonical round-trip, and returns a `VerifiedAction` containing the same bytes and frozen snapshot. Any mismatch raises `IntegrityError`.
- `summary` is required and non-empty. It is what the human reads.
- `workflow_id`, `requested_by`, `responder`, `actor`, and idempotency keys are non-empty strings; `dedupe_key`, when provided, is non-empty.
- `ttl` must be positive. `expires_at_ms = created_at_ms + ttl_ms`.
- `expected_digest` is required for approve and reject. It must be the full 64-character SHA-256 hex digest returned by `propose()`; comparisons are exact.
- Default guardrails: action JSON at most 64 KiB, summary and reason at most 2,000 characters, idempotency key at most 200 characters.

The server computes the digest. Clients never provide it when proposing; they receive it and send it back when approving or rejecting. The execution path is `wait()` → approved `Outcome` → `verified_action(outcome)` → execute the returned payload. See [Section 6.8](#68-verified_action).

## 5. State machine

### 5.1 Transition matrix

| From | Event | Condition | To | Actor |
|---|---|---|---|---|
| `PENDING` | approve | `now_ms < expires_at_ms` | `APPROVED` | responder |
| `PENDING` | reject | `now_ms < expires_at_ms` | `REJECTED` | responder |
| `PENDING` | cancel | `now_ms < expires_at_ms` | `CANCELLED` | requester/actor |
| `PENDING` | expire | `now_ms >= expires_at_ms` | `EXPIRED` | `system:expiry` |
| terminal | any | — | no transition | — |

A pending request whose deadline has arrived expires, regardless of which event arrives first. A late approve does not get a chance to race the deadline; the transaction records the expiry.

### 5.2 Deadline rule

A request is approvable, rejectable, or cancellable only while:

```text
now_ms < expires_at_ms
```

At `now_ms == expires_at_ms` it is expired. There is no grace period and no inclusive boundary.

The same `now_ms` is used for every check and timestamp inside one write transaction. It is captured after the store has acquired its writer lock, not before.

### 5.3 Authority model

- `requests.status` is the only operational state. Runtime behavior reads it.
- `transitions` is append-only audit history and idempotency evidence.
- Append-only is enforced by SQLite triggers that reject `UPDATE`, `DELETE`, and replacement of an existing transition.
- `transitions` is never used to reconstruct current status at runtime.
- The one-terminal-transition constraint is an integrity guard, not a second authority.
- A consistency test asserts that terminal status and the single terminal transition agree.

### 5.4 Replay is not a transition

An idempotent replay returns the stored terminal outcome. It does not append a row, increment `version`, or change `requests.status`. A replay is a read result, not a state change.

### 5.5 Responsibility split

The layers have deliberately different jobs:

| Layer | Responsibility |
|---|---|
| Reducer (`state.py`) | Pure decision: given a request snapshot, an event, and `now_ms`, decide whether the target transition is legal and what it should be. It never opens a transaction, never checks an idempotency key, and never reads the clock. |
| Store | Atomically applies a reducer decision only if the request is still pending and the deadline predicate still holds. It owns idempotency lookup, expiry recording, conditional updates, and result classification. |
| Facade | Validates input, canonicalizes the action, computes fingerprints, calls the store, and translates store results into `Outcome` values or typed errors. |

The reducer decides legality. SQLite decides which competing legal transition wins. The facade decides how the result is presented. No layer is allowed to borrow another layer’s authority.

The shared store contract suite proves that both stores follow the same reducer semantics, including every row of the transition matrix.

## 6. Operations and read/write semantics

| Operation | Writes? | Behavior |
|---|---|---|
| `propose` | Yes | Creates a request, or returns the existing pending request for the same dedupe key |
| `get` | No | Reads one stored row; the facade derives `overdue` |
| `list` | No | Reads stored rows exactly as they are |
| `outcome` | No | Reads the stored terminal outcome; returns `None` while the request is pending |
| `pending` | Yes | Runs `sweep()`, then lists only rows still before their deadline |
| `sweep` | Yes | Expires overdue pending requests and records the transitions |
| `approve` / `reject` | Yes | Applies a terminal response or returns a replay/terminal result |
| `cancel` | Yes | Applies a terminal cancellation or returns a replay/terminal result |
| `wait` | Sometimes | Polls; performs a targeted expiry write only if its request has passed the deadline |
| `verified_action` | No | Reloads the approved payload, verifies the exact stored bytes, and returns an immutable snapshot |

The store layer is always pure on reads. Only the facade operation `pending()` adds the documented sweep. No list or get operation hides a write.

### 6.1 `propose`

Inputs: `workflow_id`, `action`, `summary`, `requested_by`, `ttl`, optional `dedupe_key`.

Behavior:

1. Validate inputs and canonicalize the action.
2. Begin an immediate transaction.
3. Capture `now_ms` and compute `created_at_ms` / `expires_at_ms`.
4. If `dedupe_key` is set, look for an existing `PENDING` request with the same `(workflow_id, dedupe_key)`.
5. If found and `expires_at_ms > now_ms`, return it with `deduplicated=True`.
6. If found and `expires_at_ms <= now_ms`, apply its expiry transition in this transaction using the same `now_ms`, then treat it as terminal and continue.
7. Insert the new request.
8. If the partial unique index rejects the insert due to a race, re-read the winner; return it only if it is pending and unexpired, otherwise restart the proposal.

Canonicalization happens once, before insert. The stored action is the exact byte sequence whose digest is stored; the caller’s mapping is never retained or re-read.

Dedupe is deadline-aware: an overdue row is never returned as a dedupe hit. It is expired in the same transaction, and the new request is inserted after the old row becomes terminal. This keeps the dedupe result consistent with the deadline rule.

Dedupe scope is `(workflow_id, dedupe_key)` and applies only while the existing request is pending and unexpired. After a request reaches a terminal state, the same key may be used to propose a new request. This is intentional: a rejected action can be revised and proposed again.

The dedupe key is the caller’s assertion that this is the same logical proposal; the action payload is not compared. A caller that reuses an unexpired pending key for different content receives the existing request. Proposal idempotency is opt-in via `dedupe_key`; without it, a retry creates a new request.

### 6.2 `get`, `list`, and `outcome`

Both are pure reads.

- `get(request_id)` returns `None` when missing. The facade adds `overdue`, computed from the injected clock.
- `list(...)` returns stored rows and stored statuses, with `overdue` derived by the facade. It does not expire anything.
- `list` supports optional filters for `status`, `workflow_id`, and `limit`.

A stored `PENDING` row whose deadline has passed is still `PENDING` until a facade write or `sweep()` expires it. `get()` reports `overdue=True` rather than pretending the row is already terminal.

`outcome(request_id)` is a pure read. It returns `None` for a missing or still-pending request. For a terminal request it reads the single transition and returns an `Outcome` with the stored actor, reason, and time. It never sweeps, so an overdue but unswept row still returns `None` until an expiry is recorded. A terminal request with no transition, or a transition that disagrees with the stored status, is a database integrity failure and raises `IntegrityError`. `Outcome.replayed` is always `False` for this read; it is set only when a decision call replays a stored transition.

### 6.3 `pending`

`pending()` is the convenience read for human interfaces, and it writes. It returns a `PendingPage`:

1. Run `sweep()` and capture the count.
2. Capture `now_ms` after the sweep.
3. List `status = 'pending' AND expires_at_ms > now_ms`.
4. Return the rows plus the sweep count.

This gives a useful guarantee: **`pending()` never returns a request that was already overdue at the captured time.** A request whose deadline passes after the query returns may still be listed as pending by that call, but it will be expired by the next operation.

The method’s docstring must say that it sweeps. This is the one facade list operation with a side effect.

### 6.4 `sweep`

`sweep()` finds pending requests with `expires_at_ms <= now_ms`, applies the expiry transition to each, and returns the count. It accepts an optional `request_id` for a targeted sweep used by `wait()`.

- Each request is expired with a conditional update and a transition insert in the same transaction.
- Concurrent sweeps are safe: the conditional update ensures only one wins.
- A sweep is idempotent. Already-terminal requests are ignored.
- Large sweeps are batched (for example, 1,000 rows per transaction) to avoid holding the writer lock too long.

### 6.5 `approve` and `reject`

Both require:

- `responder`
- `expected_digest`
- `idempotency_key`
- optional `reason`

Approval and rejection follow the transaction precedence in [Section 7.3](#73-replay-vs-conflict-precedence). Expiry is checked before the digest precondition and before the decision update. A request that reaches its deadline in the same transaction is expired, never approved.

Reject reasons are required by the CLI and HTTP adapters. The core stores an optional reason; policy belongs to the surface.

### 6.6 `cancel`

Cancellation is a terminal user action with the same idempotency rules as approve/reject. It requires an `actor` and an `idempotency_key`, and takes an optional reason. It has no digest precondition because it does not approve an action.

Cancellation is only possible while `now_ms < expires_at_ms`. A cancellation attempt at or after the deadline produces an expiry transition and an `EXPIRED` outcome, not a cancellation.

Authorization for cancellation (for example, only the requester may cancel) is adapter-level in V1.

### 6.7 `wait`

`wait()` is a polling convenience, not the source of truth.

- It reads the request; a missing request raises `RequestNotFound`.
- If terminal, it returns the stored outcome.
- If pending but overdue, it performs a targeted sweep and re-reads.
- Otherwise it sleeps for the poll interval and repeats.
- A caller timeout before the request deadline raises `StillPending` and leaves the request pending.
- Cancelling the wait never approves, rejects, or cancels the request. A targeted expiry sweep that has already started may still commit the expiry required by the deadline.
- The poll sleep is capped at the remaining caller timeout, so `wait()` does not oversleep its deadline.
- It never performs a global sweep.

The sync and async variants share the same polling logic but not the same blocking primitive:

- `wait()` uses `SyncSleeper.sleep(seconds)`.
- `wait_async()` uses `await AsyncSleeper.sleep(seconds)`.
- `wait_async()` performs store reads and targeted sweeps through `asyncio.to_thread`, so synchronous SQLite work never runs on the event loop.
- Neither variant calls `time.sleep()` or synchronous database methods directly from an async task.
- Caller timeout is measured with the injected `Clock`, not a hidden event-loop timer, so `ManualClock` and a fake async sleeper keep tests deterministic.
- `asyncio.CancelledError` propagates from `wait_async()` without approving, rejecting, or cancelling the request. An in-flight `to_thread` sweep may still commit an already-due expiry.

See [Section 10.1](#101-construction) for the two sleeper dependencies.

### 6.8 `verified_action`

`verified_action(outcome)` is the supported execution path:

1. Require `outcome.status is APPROVED`; otherwise raise `RequestNotApproved`.
2. Load the request and require `request.status is APPROVED`; otherwise raise `RequestNotApproved`.
3. Require `outcome.action_digest == request.action_digest`; otherwise raise `IntegrityError`.
4. Take the UTF-8 bytes of the exact stored `action_json` text and recompute their SHA-256.
5. Require the recomputed digest to match the stored `action_digest`.
6. Parse the bytes and require that canonicalizing the parsed value reproduces the exact stored bytes.
7. Return `VerifiedAction(request_id, action=FrozenJSON, canonical_json=bytes, action_digest=...)`.

The stored `requests.status` is authoritative. A caller-constructed `Outcome` cannot authorize a payload for a request that is not approved.

The executor must use the returned `VerifiedAction` — preferably its `canonical_json` bytes, or its frozen `action` snapshot. It must not verify a caller-owned mutable dictionary and then execute a different reference. Because both the bytes and the snapshot are immutable, there is no window for the payload to change between verification and use.

If any digest or canonical-round-trip check fails, raise `IntegrityError` and do not execute. The gate still cannot enforce what happens after the executor receives the payload; see [Failure modes](#13-failure-modes).

## 7. Idempotency

### 7.1 Keys are required

Every user-initiated terminal operation — approve, reject, or cancel — requires a non-empty `idempotency_key`. Missing keys are rejected at the API boundary with `InvalidRequest`.

This is stricter than treating a matching outcome as a replay. Replay requires proof that the request is the same operation, not merely that the state ended up the same way. Adapters generate one key per rendered user action; CLI and HTTP make this explicit.

Keys are scoped to a request. The same key used on a different request is independent.

### 7.2 Operation fingerprint

The transition stores a fingerprint of the exact operation:

```json
{
  "v": 1,
  "request_id": "req_...",
  "operation": "approve",
  "actor": "alice@corp",
  "reason": "copy reviewed",
  "expected_digest": "9f2c..."
}
```

For `cancel`, the payload omits `expected_digest` and uses `"operation": "cancel"`. For `reject`, `"operation": "reject"`.

The fingerprint is the SHA-256 hex of the canonical JSON of that payload. It is stored in `transitions.request_fingerprint` alongside the key.

Rules:

- The fingerprint includes the exact `reason` string. No trimming or normalization happens in the core; adapters may normalize before calling.
- `expected_digest` is included because approving a different payload is a different operation, even with the same key.
- `actor` is included because the same key used by a different responder must not replay someone else’s decision. For approve/reject it is the responder; for cancel it is the cancelling actor.
- A version field is included so the fingerprint format can evolve deliberately.

### 7.3 Replay vs conflict precedence

All checks happen inside the same write transaction, using one captured `now_ms`:

| Step | Check | Result |
|---|---|---|
| 1 | Request does not exist | `RequestNotFound` (404) |
| 2 | Key exists, fingerprint matches | Return stored outcome, `replayed=True` (200) |
| 3 | Key exists, fingerprint differs | `IdempotencyConflict` (409) |
| 4 | Request is already approved/rejected | `AlreadyDecided` with current outcome (409) |
| 5 | Request is already expired/cancelled | `RequestExpired` / `RequestCancelled` (410) |
| 6 | Deadline reached while still pending | Record expiry, return `RequestExpired` (410) |
| 7 | `expected_digest` mismatch | `DigestMismatch` (412) |
| 8 | Apply the conditional transition | `Applied` (200) |

Replay lookup deliberately precedes the terminal-state checks: a retry of the operation that committed must succeed even though the request is now terminal. Fingerprint comparison deliberately precedes the digest precondition: reusing a key with a different digest is an idempotency conflict, not a stale-action error.

Failed attempts — digest mismatch, unauthorized, invalid input — are not stored, so they do not burn the key. A caller can correct the input and retry with the same key.

### 7.4 Races

- Two responses with the same key: serialized by the write transaction. One applies; the other finds the transition and replays it.
- Two responses with different keys: one applies; the other sees the terminal state and returns `AlreadyDecided`.
- A response and a sweep at the deadline: the conditional updates race; exactly one records a terminal transition. If expiry wins, the response returns `RequestExpired`.
- A response and a cancellation: same rule; the first terminal transition wins, the other is rejected based on the stored state.

No in-process lock is required for SQLite correctness: it serializes writers, and the conditional updates and unique indexes are the backstop. The memory store uses a lock because it has no database to do that work.

## 8. Expiry

### 8.1 Deadline model

`expires_at_ms` is wall-clock UTC milliseconds. It is computed when the request is created. All comparisons use the injected clock.

The deadline is exclusive for actions and inclusive for expiry:

- `now_ms < expires_at_ms` — the request may be approved, rejected, or cancelled.
- `now_ms >= expires_at_ms` — the request is expired.

### 8.2 Authoritative transition

Expiry is enforced by the database update that also records the state change, not by a preliminary Python check that could go stale:

```sql
UPDATE requests
SET status = 'expired', version = version + 1
WHERE id = :id
  AND status = 'pending'
  AND expires_at_ms <= :now_ms;
```

If this updates one row, an expiry transition is inserted in the same transaction:

```text
from_status = pending
to_status   = expired
actor       = system:expiry
reason      = deadline_reached
at_ms       = now_ms
key         = NULL
fingerprint = NULL
```

If it updates zero rows, the request was already terminal or is not yet due; the caller classifies by re-reading.

Approval and rejection use the mirror condition:

```sql
UPDATE requests
SET status = :outcome, version = version + 1
WHERE id = :id
  AND status = 'pending'
  AND expires_at_ms > :now_ms;
```

Both statements live inside one `BEGIN IMMEDIATE` transaction. Only one can apply. The exact deadline instant is expired.

### 8.3 Transaction algorithm

The normative response path:

```python
def respond(cmd: ResponseCommand) -> ResponseResult:
    with store.write_tx():                    # BEGIN IMMEDIATE
        now = clock.now_ms()                  # after writer lock acquisition
        req = store.load(cmd.request_id)
        if req is None:
            return NotFound()

        prior = store.find_transition(req.id, cmd.idempotency_key)
        if prior is not None:
            if prior.request_fingerprint != cmd.fingerprint:
                return IdempotencyConflict()
            return Replayed(outcome_from(prior, req))

        if req.status is not Status.PENDING:
            return terminal_result(req)

        if now >= req.expires_at_ms:
            transition = expire_and_record(req, now)
            if transition is not None:
                return Expired(outcome_from(transition, req))
            return classify(store.load(req.id))

        if cmd.expected_digest != req.action_digest:
            return DigestMismatch()

        changed = store.cas_decide(
            request_id=req.id,
            outcome=cmd.outcome,              # approved or rejected
            now_ms=now,                       # WHERE ... expires_at_ms > now
        )
        if changed != 1:
            return classify(store.load(req.id))

        transition = store.insert_transition(...)
        return Applied(outcome_from(transition, req))
```

`cancel()` uses the same transaction shape with `outcome=cancelled` and no digest precondition.

### 8.4 Sweep, waiting, and `pending()`

- `sweep()` applies the same conditional expiry update and transition insert to every due pending request.
- `wait()` performs a targeted sweep for its own request when the deadline has passed.
- `pending()` runs a global sweep, then filters the read by a captured `now_ms`.

A request can become overdue between two operations, but it cannot be approved after its deadline, and it cannot be returned by `pending()` as already overdue at the time of that call.

### 8.5 Clock behavior

The store never calls `CURRENT_TIMESTAMP` or reads the wall clock itself. It calls the injected clock after acquiring the writer lock. This makes tests deterministic and keeps all timestamps in one domain.

Clock skew is a deployment concern: a forward jump can expire requests early, and a backward jump can extend them. The gate fails closed on the first and is merely permissive on the second. Use a single trusted clock source across processes.

## 9. Storage

### 9.1 SQLite settings

Every connection:

```sql
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA journal_mode = WAL;
PRAGMA recursive_triggers = ON;
```

Writes use `BEGIN IMMEDIATE`, which acquires the writer lock before reading. This is what makes “read, classify, update” safe across processes. WAL allows readers to continue while a writer holds the lock.

`recursive_triggers` is enabled because a `REPLACE` conflict deletes the conflicting row before inserting. With recursive triggers on, the append-only `BEFORE DELETE` trigger fires on that implicit delete and aborts the replacement.

The schema version is tracked with `PRAGMA user_version` and applied by a small migration runner. V1 starts at version 1.

### 9.2 Schema

```sql
CREATE TABLE requests (
    id             TEXT PRIMARY KEY,
    workflow_id    TEXT NOT NULL,
    dedupe_key     TEXT,
    status         TEXT NOT NULL
                   CHECK (status IN ('pending','approved','rejected','expired','cancelled')),
    action_json    TEXT NOT NULL,
    action_digest  TEXT NOT NULL,
    summary        TEXT NOT NULL,
    requested_by   TEXT NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    expires_at_ms  INTEGER NOT NULL,
    version        INTEGER NOT NULL DEFAULT 0,
    CHECK (expires_at_ms > created_at_ms),
    CHECK (length(summary) > 0),
    CHECK (length(action_digest) = 64
           AND action_digest NOT GLOB '*[^0-9a-f]*')
);

CREATE UNIQUE INDEX ux_requests_pending_dedupe
    ON requests(workflow_id, dedupe_key)
    WHERE status = 'pending' AND dedupe_key IS NOT NULL;

CREATE INDEX ix_requests_pending_deadline
    ON requests(status, expires_at_ms);

CREATE INDEX ix_requests_workflow
    ON requests(workflow_id, status, created_at_ms);

CREATE TABLE transitions (
    seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          TEXT NOT NULL UNIQUE
                        REFERENCES requests(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    from_status         TEXT NOT NULL CHECK (from_status = 'pending'),
    to_status           TEXT NOT NULL
                        CHECK (to_status IN ('approved','rejected','expired','cancelled')),
    actor               TEXT NOT NULL,
    reason              TEXT,
    at_ms               INTEGER NOT NULL,
    idempotency_key     TEXT,
    request_fingerprint TEXT,
    CHECK (idempotency_key IS NULL
           OR length(idempotency_key) BETWEEN 1 AND 200),
    CHECK (request_fingerprint IS NULL
           OR (length(request_fingerprint) = 64
               AND request_fingerprint NOT GLOB '*[^0-9a-f]*')),
    CHECK (
        (to_status = 'expired'
         AND idempotency_key IS NULL
         AND request_fingerprint IS NULL)
        OR
        (to_status IN ('approved','rejected','cancelled')
         AND idempotency_key IS NOT NULL
         AND request_fingerprint IS NOT NULL)
    )
);

CREATE TRIGGER requests_insert_pending
BEFORE INSERT ON requests
WHEN NEW.status <> 'pending'
BEGIN
    SELECT RAISE(ABORT, 'new requests must be pending');
END;

CREATE TRIGGER requests_no_replace
BEFORE INSERT ON requests
WHEN EXISTS (SELECT 1 FROM requests WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'requests cannot be replaced');
END;

CREATE TRIGGER requests_no_delete
BEFORE DELETE ON requests
BEGIN
    SELECT RAISE(ABORT, 'requests cannot be deleted');
END;

CREATE TRIGGER requests_immutable_fields
BEFORE UPDATE OF id, workflow_id, dedupe_key, action_json, action_digest,
                 summary, requested_by, created_at_ms, expires_at_ms
ON requests
BEGIN
    SELECT RAISE(ABORT, 'immutable request field');
END;

CREATE TRIGGER requests_status_transition
BEFORE UPDATE OF status ON requests
WHEN OLD.status <> 'pending'
   OR NEW.status NOT IN ('approved','rejected','expired','cancelled')
   OR NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'invalid request status transition');
END;

CREATE TRIGGER requests_version_requires_status
BEFORE UPDATE OF version ON requests
WHEN NEW.status = OLD.status
BEGIN
    SELECT RAISE(ABORT, 'version changes only with a status transition');
END;

CREATE TRIGGER transitions_no_update
BEFORE UPDATE ON transitions
BEGIN
    SELECT RAISE(ABORT, 'transitions is append-only');
END;

CREATE TRIGGER transitions_no_delete
BEFORE DELETE ON transitions
BEGIN
    SELECT RAISE(ABORT, 'transitions is append-only');
END;

CREATE TRIGGER transitions_validate_insert
BEFORE INSERT ON transitions
BEGIN
    SELECT RAISE(ABORT, 'transition already exists')
    WHERE EXISTS (
        SELECT 1 FROM transitions WHERE request_id = NEW.request_id
    );

    SELECT RAISE(ABORT, 'only pending requests can transition')
    WHERE NEW.from_status <> 'pending';

    SELECT RAISE(ABORT, 'invalid terminal status')
    WHERE NEW.to_status NOT IN ('approved','rejected','expired','cancelled');

    SELECT RAISE(ABORT, 'request status must match transition')
    WHERE NOT EXISTS (
        SELECT 1 FROM requests
        WHERE id = NEW.request_id AND status = NEW.to_status
    );

    SELECT RAISE(ABORT, 'user transition at or after deadline')
    WHERE NEW.to_status IN ('approved','rejected','cancelled')
      AND NEW.at_ms >= (
          SELECT expires_at_ms FROM requests WHERE id = NEW.request_id
      );

    SELECT RAISE(ABORT, 'expiry before deadline')
    WHERE NEW.to_status = 'expired'
      AND NEW.at_ms < (
          SELECT expires_at_ms FROM requests WHERE id = NEW.request_id
      );

    SELECT RAISE(ABORT, 'expiry must be system-generated')
    WHERE NEW.to_status = 'expired'
      AND (NEW.actor <> 'system:expiry'
           OR NEW.idempotency_key IS NOT NULL
           OR NEW.request_fingerprint IS NOT NULL);

    SELECT RAISE(ABORT, 'user transition needs key and fingerprint')
    WHERE NEW.to_status IN ('approved','rejected','cancelled')
      AND (NEW.idempotency_key IS NULL OR NEW.request_fingerprint IS NULL);

    SELECT RAISE(ABORT, 'invalid actor for user transition')
    WHERE NEW.to_status IN ('approved','rejected','cancelled')
      AND NEW.actor = 'system:expiry';
END;
```

Integrity is now enforced in three places:

- The partial unique index allows one pending request per `(workflow_id, dedupe_key)`.
- `UNIQUE(request_id)` on `transitions` allows one terminal transition per request.
- Triggers enforce append-only transitions, immutable request fields, legal status changes with exact version increments, and transition inserts that match the matrix and deadline. `requests_no_replace`, `requests_no_delete`, and the `transition already exists` guard close the `INSERT OR REPLACE` bypass, which would otherwise delete and recreate a row without firing a normal update trigger.

None of these replaces the conditional update as the race gate. They are defense in depth and catch application bugs.

The store uses plain `INSERT` statements. It never uses `INSERT OR REPLACE`, `INSERT OR IGNORE`, or an upsert clause for `requests` or `transitions`; an unexpected constraint failure rolls back the transaction and is surfaced as an error.

`transitions_validate_insert` checks that `requests.status` already equals `NEW.to_status`. The store therefore updates the request row first and inserts the transition second, in the same transaction. If the insert fails, the transaction rolls back the status update. This closes the “terminal state without an audit row” hole for the normal path; the contract tests additionally prove that every status change writes a transition.

`requests.version` starts at zero and increments by exactly one for every applied transition. Replays do not increment it.

### 9.3 Proposal dedupe race

`propose()` runs inside `BEGIN IMMEDIATE`:

1. Select the existing pending row for `(workflow_id, dedupe_key)`.
2. If present and `expires_at_ms > now_ms`, return it with `deduplicated=True`.
3. If present and `expires_at_ms <= now_ms`, expire it and insert its expiry transition, then continue.
4. Insert the new request.
5. If the insert still hits the partial unique index, re-read the winner; return it only if it is pending and unexpired, otherwise restart the proposal.

Because the old row is terminal before the new insert, the partial unique index permits the new request. The expiry and the new request commit atomically. The immediate transaction should make step 5 unnecessary in normal operation, but it is required for defense in depth and for future stores with weaker isolation.

A terminal request does not block a new proposal with the same dedupe key. This lets a workflow revise a rejected action and ask again.

### 9.4 Connection model

SQLite connections are not shared across threads. The store uses either a connection per operation or a thread-local connection; both are acceptable at human-approval scale. It never uses one connection concurrently from multiple threads.

`wait_async()` calls the synchronous store through `asyncio.to_thread`, so the worker thread gets its own connection under the same rule. No SQLite connection is used from two threads at once.

Every write opens a transaction, commits on success, and rolls back on any error. The facade never holds a transaction open across a wait or a callback.

### 9.5 Time and identity

- All timestamps are UTC epoch milliseconds stored as SQLite `INTEGER`.
- Request IDs are generated by an injected `IdGenerator`; the default is `req_` plus a UUID4 hex.
- The store captures `now_ms` after acquiring the write lock; it never uses SQLite’s clock.
- The same `Clock` instance is shared by the store and the facade.

## 10. Facade API

### 10.1 Construction

```python
clock = SystemClock()
store = SqliteStore("approvals.db", clock=clock)
gate = ApprovalGate(
    store=store,
    clock=clock,
    ids=UuidIds(),
    sync_sleeper=RealSyncSleeper(),
    async_sleeper=RealAsyncSleeper(),
    poll_interval=1.0,
    on_transition=optional_callback,
)
```

`RealSyncSleeper.sleep()` delegates to `time.sleep()`. `RealAsyncSleeper.sleep()` delegates to `await asyncio.sleep()`. `wait()` uses only the sync sleeper; `wait_async()` uses only the async sleeper and offloads store calls through `asyncio.to_thread`.

`on_transition` is called after a successful commit with each applied `Transition`, including expiry transitions recorded by `sweep()`, `pending()`, `propose()`, or `wait()`. Replays do not call it. It is best-effort: exceptions are logged and swallowed, and a callback failure never rolls back a decision. It exists for logging, metrics, or a future outbox; it is not a second source of truth. The callback must not block the caller for long, and it must not open a write transaction against the same store.

### 10.2 Methods

```python
def propose(
    self,
    *,
    workflow_id: str,
    action: Mapping[str, Any],
    summary: str,
    requested_by: str,
    ttl: timedelta,
    dedupe_key: str | None = None,
) -> Proposal: ...

def get(self, request_id: str) -> Request | None: ...

def list(
    self,
    *,
    status: Status | None = None,
    workflow_id: str | None = None,
    limit: int = 100,
) -> list[Request]: ...

def outcome(self, request_id: str) -> Outcome | None: ...

def pending(
    self,
    *,
    workflow_id: str | None = None,
    limit: int = 100,
) -> PendingPage: ...

def sweep(self, *, request_id: str | None = None) -> int: ...

def approve(
    self,
    request_id: str,
    *,
    responder: str,
    expected_digest: str,
    idempotency_key: str,
    reason: str | None = None,
) -> Outcome: ...

def reject(
    self,
    request_id: str,
    *,
    responder: str,
    expected_digest: str,
    idempotency_key: str,
    reason: str | None = None,
) -> Outcome: ...

def cancel(
    self,
    request_id: str,
    *,
    actor: str,
    idempotency_key: str,
    reason: str | None = None,
) -> Outcome: ...

def wait(
    self,
    request_id: str,
    *,
    poll_interval: float | None = None,
    timeout: float | None = None,
) -> Outcome: ...

async def wait_async(
    self,
    request_id: str,
    *,
    poll_interval: float | None = None,
    timeout: float | None = None,
) -> Outcome: ...

def verified_action(self, outcome: Outcome) -> VerifiedAction: ...
```

`wait()` raises `StillPending` when the caller timeout expires before the request deadline; the request remains pending and can be awaited again or decided by someone else. If the request deadline has already passed, the targeted sweep records the expiry and `wait()` returns the expiry outcome instead. `wait_async()` uses the async sleeper and offloads synchronous store work; it never blocks the event loop.

### 10.3 Errors

```text
ApprovalError
├── RequestNotFound
├── InvalidRequest
├── IdempotencyConflict
├── AlreadyDecided         # carries the current Outcome
├── RequestExpired         # carries the expiry Outcome
├── RequestCancelled       # carries the cancellation Outcome
├── DigestMismatch
├── RequestNotApproved     # verified_action on a non-approved request
├── IntegrityError         # payload/digest mismatch or audit inconsistency
├── StillPending
└── StoreBusy              # optional, when busy_timeout is exhausted
```

Terminal errors carry the current outcome so adapters can return a useful 409/410 body and clients can reconcile without a second call.

## 11. Human interfaces

### 11.1 CLI

```text
approval-gate --db approvals.db list [--workflow ID] [--limit N]
approval-gate --db approvals.db show ID
approval-gate --db approvals.db review [ID] --responder alice
    [--workflow ID] [--limit N]
approval-gate --db approvals.db approve ID --responder alice --digest HEX
    [--reason TEXT] [--idempotency-key KEY]
approval-gate --db approvals.db reject ID --responder bob --digest HEX
    --reason TEXT [--idempotency-key KEY]
approval-gate --db approvals.db cancel ID --actor agent:planner
    [--reason TEXT] [--idempotency-key KEY]
approval-gate --db approvals.db sweep
```

- `list` calls `pending()` and therefore sweeps, then prints the page’s requests. This is documented in `--help`.
- `show` uses the pure `get()`.
- `review` is an interactive one-request view. It requires a TTY, renders the action card, and handles the digest behind the decision. It is a convenience surface, not an authorization boundary.
- Approve/reject require the full digest. A digest prefix is rejected. These commands remain the non-interactive automation path.
- Reject requires a reason at the CLI layer.
- If `--idempotency-key` is omitted, the CLI generates one and prints it before applying, so a failed invocation can be retried with the same key.

Suggested exit codes:

| Code | Meaning |
|---|---|
| 0 | Applied or replayed |
| 2 | Not found |
| 3 | Conflict or already decided |
| 4 | Expired or cancelled |
| 5 | Digest mismatch |
| 6 | Invalid input |

### 11.2 Approval Console and agent bridge

The terminal Approval Console is the human surface used for dogfooding. It lives in `examples/approval_console.py`; it is an example adapter, not part of the installed wheel, but it uses only the public facade plus the shared presentation helpers.

```text
python examples/approval_console.py --db approvals.db --as alice@corp
    [--workflow ID] [--limit N]

approval> pending
approval> show [N|ID]
approval> approve [N|ID]
approval> reject [N|ID] REASON
approval> details [N|ID]
approval> quit
```

- `pending` calls `pending()` and therefore sweeps. It lists only unexpired pending requests.
- A numeric target is resolved against the page just rendered. A request ID is read with the pure `get()`.
- The card renders `action_rows(request.action)` plus a short digest fingerprint. Known fields such as tool, command, branch, recipient, and message get human labels; unknown fields remain visible in an `Other fields` block. The canonical JSON and full digest are available through `details`.
- Approve and reject confirm first. The console binds `expected_digest` to the request it rendered and generates a fresh idempotency key for the decision. The human never types either value.
- Reject requires a non-empty reason. The console asks again if the reason is blank.
- A direct `show` of an overdue request records the targeted expiry and prints the receipt; it does not present an expired action as approvable.
- A terminal request shows its receipt instead of an approval card. This includes decisions made by another surface.
- `--as` is an identity assertion, not authentication. The console has no authority of its own; the gate records the asserted identity in the transition.
- Untrusted summary, actor, workflow, and reason fields are rendered on one line. Payload blocks are indented and control characters are escaped, so stored text cannot drive the terminal.

The agent bridge is `examples/agent_cli.py`:

```text
python examples/agent_cli.py --db approvals.db propose --workflow ID
    --summary TEXT (--action JSON | --action-file PATH) [--ttl-seconds N]
    [--requested-by ID] [--dedupe-key KEY] [--json]
python examples/agent_cli.py --db approvals.db status ID [--json]
python examples/agent_cli.py --db approvals.db wait ID
    [--timeout N] [--poll-interval N] [--json]
```

`propose` creates a durable request and exits immediately, so the agent can end its turn. `status` is a pure read; when the request is approved it calls `verified_action()` and prints the verified canonical payload. `wait` is an opt-in polling convenience. The bridge never executes the action and never treats a pending request as permission to continue.

Exit codes are `0` approved/proposed, `1` internal error, `2` not found, `3` rejected, `4` expired, `5` cancelled, `6` still pending, and `7` invalid input.

The bridge is a coordination tool, not a sandbox. An agent that can run arbitrary commands can bypass it unless the tool loop or execution environment enforces the gate.

### 11.3 HTTP demo

The FastAPI app is a demonstration adapter, not a production service.

```text
GET  /approvals?workflow_id=...
GET  /approvals/{id}
POST /approvals
POST /approvals/{id}/approve
POST /approvals/{id}/reject
POST /approvals/{id}/cancel
```

- `GET /approvals` lists pending requests only. It calls `pending()` and returns `{"items": [...], "swept": N}` from the `PendingPage`. The sweep count makes the documented side effect visible.
- `GET /approvals/{id}` is a pure read and includes `overdue`; when terminal, it includes the stored outcome and therefore the terminal actor, reason, and time.
- `POST /approvals` exists for the demo so non-Python agents can exercise the flow. The real agent integration uses the library.
- Approve and reject require `expected_digest`; all three decision endpoints require an `Idempotency-Key` header. A missing key is a 400.
- Reject requires a non-empty reason in the demo, mirroring the CLI.
- `requested_by`, `responder`, and `actor` come from the authenticated principal, never from the request body.
- A replay returns 200 with `"replayed": true`.
- The demo denies self-approval: the authenticated principal cannot approve a request whose `requested_by` is the same identity.
- There is no long-poll or wait endpoint in V1. Clients poll the pure `GET /approvals/{id}`.

Suggested status mapping:

| Error | HTTP |
|---|---|
| `InvalidRequest` | 400 |
| `RequestNotFound` | 404 |
| `IdempotencyConflict` | 409 |
| `AlreadyDecided` | 409, include current outcome |
| `RequestExpired` | 410, include expiry outcome |
| `RequestCancelled` | 410, include cancellation outcome |
| `DigestMismatch` | 412 |
| `IntegrityError` | 500, internal; log and alert |
| `StoreBusy` | 503 |

Adapter-level failures sit outside the core error taxonomy: a missing or invalid bearer token returns 401, and the demo's self-approval denial returns 403.

Error envelope:

```json
{
  "error": {
    "code": "already_decided",
    "message": "Request was already approved",
    "current": {
      "status": "approved",
      "actor": "alice@corp",
      "at_ms": 1767225600000
    }
  }
}
```

### 11.4 Authentication and trust

The core treats `requested_by`, `responder`, and `actor` as opaque strings. It does not authenticate them.

- The FastAPI demo uses static bearer tokens mapped to identities. This is an illustrative adapter, not a production auth system.
- The CLI treats `--responder` and `--actor`, and the Approval Console treats `--as`, as caller-supplied identity assertions, not authentication.
- Production adapters must authenticate the human and derive `requested_by`, `responder`, and `actor` from that identity.
- The approval digest is part of the authorization decision: the human approves the digest they saw, and the server refuses to apply it to a different payload.

## 12. Determinism and testing

### 12.1 Injected dependencies

The core never calls `datetime.now()`, `time.time()`, `uuid.uuid4()`, or `time.sleep()` directly.

| Dependency | Production | Tests |
|---|---|---|
| `Clock` | `SystemClock` | `ManualClock` |
| `IdGenerator` | `UuidIds` | `SeqIds` |
| `SyncSleeper` | `RealSyncSleeper` (`time.sleep`) | `FakeSyncSleeper` |
| `AsyncSleeper` | `RealAsyncSleeper` (`asyncio.sleep`) | `FakeAsyncSleeper` |

```python
class SyncSleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...

class AsyncSleeper(Protocol):
    async def sleep(self, seconds: float) -> None: ...
```

`seconds` is always non-negative. `wait()` calls only `SyncSleeper`; `wait_async()` calls only `AsyncSleeper`. A sleeper never performs store work or advances the clock in production.

`ManualClock.advance(...)` drives deadlines and wait loops. The fake sleepers record requested delays and advance the manual clock, so sync and async wait tests complete instantly. `wait_async()` uses `asyncio.to_thread` for store reads and targeted sweeps; a test asserts that another task continues to run while the wait is polling.

### 12.2 Test layers

**Pure reducer tests**

- Every `(status, event, deadline condition)` combination.
- Terminal states reject everything.
- Exact-boundary behavior: `deadline - 1ms`, `deadline`, `deadline + 1ms`.
- A late approve/reject/cancel produces expiry, not the requested outcome.
- Illegal events leave state unchanged.

**Fingerprint tests**

- Same key + same operation replays.
- Same key + different outcome conflicts.
- Same key + different digest conflicts.
- Same key + different responder conflicts.
- Same key + different reason conflicts.
- Failed preconditions do not store a key.

**Store contract suite**

The same suite runs against the memory and SQLite stores:

- Propose, dedupe, and terminal dedupe-key reuse.
- Proposing with a dedupe key whose pending request is overdue records the expiry and creates a new request.
- `get`/`list` are pure.
- `sweep` records exactly one expiry transition per due request.
- Replay creates no transition and no version bump.
- Terminal status and terminal transition agree.
- Terminal outcome reads return the stored actor, reason, and time.
- `version` increments once per applied transition.
- A response after the deadline records expiry.
- Read results never mutate the store.
- Every terminal status has exactly one transition; no terminal status is committed without one.
- Every transition follows the matrix and deadline.

**SQLite trigger tests**

- `UPDATE` and `DELETE` on `transitions` fail.
- `INSERT OR REPLACE` on `transitions` fails; an existing terminal transition cannot be swapped.
- `INSERT OR REPLACE` on `requests` fails; an existing request cannot be replaced.
- `DELETE` on `requests` fails.
- `INSERT OR REPLACE` with a new ID but a conflicting pending dedupe key fails.
- `UPDATE` of `action_json`, `action_digest`, or other immutable request fields fails.
- Direct inserts of an invalid transition (wrong deadline, wrong status, missing fingerprint) fail.
- A status update that does not increment `version` by exactly one fails; a `version` update without a status change fails.

**Payload integrity tests**

- Mutating the caller’s action mapping after `propose()` has no effect on the stored bytes or `Request.action`.
- Nested mutation through `Request.action` raises `TypeError`.
- `Request.action` round-trips to the exact stored canonical bytes.
- `verified_action(outcome)` returns the exact stored bytes and digest.
- A test-only tamper fixture that corrupts stored action bytes is detected as `IntegrityError`.
- `verified_action` rejects a non-approved outcome with `RequestNotApproved`.
- `verified_action` rejects an outcome that claims approval for a request stored as rejected.
- `verified_action` rejects a mismatched outcome digest with `IntegrityError`.

**Concurrency tests**

- Twenty threads approve and reject the same request: exactly one wins, exactly one terminal transition exists.
- Two responses to the same request with the same idempotency key: one applies, one replays.
- Two responses to the same request with different keys: one applies, the other gets `AlreadyDecided`.
- Approve versus sweep at the exact deadline: exactly one terminal transition.
- Concurrent `propose` calls with the same dedupe key: one request is created.
- Separate processes propose, respond, and read the same SQLite file.

**Facade and wait tests**

- `wait()` returns a decision immediately if already terminal.
- `wait()` on a missing request raises `RequestNotFound`.
- `wait()` expires an overdue request and records the transition.
- Caller timeout before the deadline raises `StillPending` and leaves the request pending.
- Cancelling a wait never approves, rejects, or cancels the request; an in-flight targeted sweep may still record an already-due expiry.
- `pending()` never returns a request overdue at its captured `now`.
- `get()` reports `overdue=True` without changing status.
- `outcome()` is a pure read: `None` while pending, stored details when terminal, and `None` for an overdue but unswept request.
- `wait_async()` yields to the event loop while polling; a concurrent task runs during the fake sleep.
- `wait_async()` performs store reads and targeted sweeps on a worker thread, never on the loop thread.
- `wait_async()` timeout is measured with the injected clock; a timeout before the deadline leaves the request pending.

**Adapter tests**

- CLI exit codes and digest requirement.
- Interactive `review` and the example Approval Console: list, show, approve/reject confirmation, details, expiry receipts, terminal requests, invalid targets, workflow filtering, and terminal-control escaping.
- Agent bridge propose/status/wait output, JSON mode, payload verification, and exit codes.
- HTTP status mapping, missing idempotency key, self-approval denial, and replay body.
- `list` in the CLI performs a sweep; `show` does not.

**Property-based tests**

Using Hypothesis, generate sequences of propose/approve/reject/cancel/sweep/advance-clock operations and assert the invariants below.

### 12.3 Invariants

1. A terminal request never changes status.
2. A request has at most one terminal transition.
3. If terminal, `requests.status` equals the terminal transition’s `to_status`.
4. Every applied user response has exactly one transition with its key and fingerprint.
5. A replay creates no transition and no version increment.
6. No approve/reject/cancel transition has `at_ms >= expires_at_ms`.
7. Every expiry transition has `at_ms >= expires_at_ms`.
8. `transitions` is append-only; the triggers reject `UPDATE`, `DELETE`, and replacement of an existing transition.
9. `version` increases by exactly one per applied transition.
10. `pending()` returns no request with `expires_at_ms <= captured_now`.
11. Store reads never write.
12. `action_digest` equals the SHA-256 of the exact stored canonical bytes and never changes.
13. The terminal transition is the only source of the terminal actor, reason, and time.
14. Mutating the caller’s proposal input after `propose()` cannot change the stored payload or its digest.
15. A transition insert either matches the matrix and deadline or the database aborts it.
16. `verified_action()` returns the exact bytes it verified; no mutable payload is used between verification and execution.
17. Normal application paths never commit a terminal status without its transition.
18. `propose()` never returns a deduplicated request that is overdue at its captured `now`; it records the expiry and creates a new request.
19. `requests` rows cannot be deleted or replaced; retention requires an explicit maintenance migration.

### 12.4 Definition of done

- The reducer table is exhaustively tested.
- Memory and SQLite pass the same contract suite.
- The concurrency and deadline-boundary tests pass repeatedly.
- The process-restart test passes.
- Action immutability, canonical-byte verification, and trigger-enforcement tests pass.
- The async wait test proves the event loop is not blocked.
- No unit test uses real sleeps or the wall clock.
- CLI, Approval Console, agent bridge, and FastAPI demos run against a temporary database.
- The public API and schema are documented.

## 13. Failure modes

**Crash before commit.** No state change. Retry with the same idempotency key.

**Crash after commit, before responding.** The retry finds the stored transition and replays it.

**Crash after approval, before execution.** The side effect may never happen. The gate records a decision; it is not an execution engine. On restart, call `outcome(request_id)`; if it is `APPROVED`, call `verified_action(outcome)` and execute the returned payload. The caller must resume and execute.

**Side effect happened, response was lost.** The downstream action must be idempotent using the request ID as its idempotency key. The gate cannot guarantee exactly-once external effects.

**Caller mutates the action after proposing.** The stored canonical bytes and frozen snapshot are independent of the caller’s mapping. The request and digest do not change.

**Action mutated after approval.** `verified_action(outcome)` re-hashes the exact stored bytes, requires a canonical round-trip, and returns an immutable `VerifiedAction`. Any mismatch raises `IntegrityError` and execution must not proceed. A stale approval UI is rejected with 412 because the submitted digest does not match the current request.

**Audit tampering.** SQLite triggers reject `UPDATE`, `DELETE`, and replacement inserts on `transitions`; reject updates to immutable request fields; reject request replacement or deletion; and reject invalid transition inserts. A privileged operator can still drop triggers or edit the file directly; that is outside the application’s trust boundary.

**Two humans respond simultaneously.** The database conditional update and one-transition constraint ensure one winner. The loser receives the current outcome.

**Response exactly at the deadline.** Expiry wins. The transaction records the expiry and returns 410.

**Sweep races a response.** Same conditional update; one terminal transition wins.

**Clock skew.** A forward jump expires early; a backward jump extends. Use one trusted time source. The design fails closed with respect to accidental approval, not with respect to early expiry.

**Database busy.** `busy_timeout` absorbs normal contention. If exhausted, raise `StoreBusy`; the caller retries with the same key.

**Notification failure.** `on_transition` is post-commit and best-effort. The decision is already durable.

**Sensitive action payloads.** The payload is stored because the human must see it. Logs should include the digest and summary, not the full action by default. Encryption at rest and retention are deployment concerns.

**Dedupe key reuse.** Only unexpired pending requests dedupe; an overdue row is expired during `propose()`. After a terminal state, the key can create a new request. This is deliberate, not a bug.

## 14. Evolution

**Quorum and multiple approvers.** Add an approval policy to the request and a `votes` table with one row per approver. Keep `transitions` as the final state change. The reducer computes whether a vote reaches the threshold; the one-terminal-transition constraint still guarantees one final transition. Idempotency becomes per vote.

**PostgreSQL.** Implement the same store contract. Use `SELECT ... FOR UPDATE` or conditional `UPDATE ... WHERE status='pending' AND expires_at_ms > now`. The partial unique index and the transition uniqueness constraint translate directly.

**Claim/consume.** Add a `claims` table keyed by request ID to make execution claiming explicit. This still does not replace downstream idempotency.

**Transactional outbox.** If notifications must be reliable, write an outbox row in the same transaction and let a worker deliver it. Keep `on_transition` for best-effort observation only.

**Wakeups.** Add `LISTEN/NOTIFY`, SSE, or a webhook adapter. Polling remains the correctness fallback.

**Full async facade.** V1 provides `wait_async()` and documents `asyncio.to_thread` for the blocking write methods. A later version can expose async wrappers for every facade method.

**Retention.** Add a purge operation for terminal requests older than a configured age. Retention must account for the append-only triggers and the request delete guard: archive transitions first or use a deliberate maintenance migration that drops and recreates the relevant triggers. Normal facade code must not delete audit rows or request rows.

**Policy hook.** If authorization must live in the core, add a pure `authorize(request, response)` hook evaluated before the decision update. V1 keeps authorization in adapters.

**Multi-tenancy.** Add `tenant_id` and include it in every key and index. Do not bolt it on by prefixing opaque IDs.

## 15. Package layout

```text
pyproject.toml
src/approval_gate/
    __init__.py
    models.py          # Status, Request, Transition, Outcome, Proposal, PendingPage, VerifiedAction
    payload.py         # canonical JSON, FrozenJSON, verified payload
    state.py           # pure reducer and transition table
    gate.py            # facade, wait loops, async offload
    clock.py           # Clock, ManualClock, IdGenerator, sync/async sleepers
    errors.py
    presentation.py    # action cards, fingerprints, timestamps, terminal escaping
    stores/
        base.py        # Store protocol and contract test helpers
        memory.py      # tests and examples
        sqlite.py      # durable store
        schema.sql     # tables, indexes, and triggers
    cli.py
examples/
    fastapi_app.py       # bearer-token HTTP demo
    agent_tool.py        # propose -> wait -> verify -> execute
    approval_console.py  # terminal human approval console
    agent_cli.py         # non-blocking agent bridge
tests/
    test_state.py
    test_idempotency.py
    test_payload.py
    test_store_contract.py
    test_triggers.py
    test_sqlite_concurrency.py
    test_wait.py
    test_async_wait.py
    test_cli.py
    test_presentation.py
    test_agent_cli.py
    test_approval_console.py
    test_http.py
docs/
    design.md
```

The core package requires Python 3.11+ and has no runtime dependencies. FastAPI, Uvicorn, and Hypothesis live in optional/dev dependency groups.

## 16. Milestones

**M0 — Design capture.** This document. No code.

**M1 — Pure core.** Models, canonical JSON/FrozenJSON handling, clock, ID generator, reducer, and exhaustive reducer tests. No database.

**M2 — Stores.** Schema, migrations, triggers, SQLite store, memory store, and the shared contract suite. Include concurrency, boundary, and trigger-enforcement tests.

**M3 — Facade and waiting.** `propose`, `get`, `list`, `outcome`, `pending`, `sweep`, `approve`, `reject`, `cancel`, `verified_action`, sync/async wait, and facade tests.

**M4 — Adapters.** CLI, FastAPI demo, terminal Approval Console, and an agent-side bridge. The agent examples propose, wait, obtain a `VerifiedAction`, and execute only the exact returned payload.

**M5 — Packaging.** `pyproject.toml`, README usage, and a runnable demo.

Exit criteria for M2/M3:

- The concurrency tests pass repeatedly.
- The exact-boundary tests pass.
- The process-restart test passes.
- Action immutability and canonical-byte verification tests pass.
- Trigger-enforcement tests pass: append-only transitions, replacement inserts, immutable request fields, invalid status changes, invalid transition inserts.
- The async wait test proves the event loop is not blocked.
- No test uses real sleeps or the wall clock.
- The store contract suite passes on both backends.
- The terminal-state/audit consistency test passes.

## 17. Decision log

| # | Decision | Rationale |
|---|---|---|
| D1 | Single approver in V1 | Matches the component’s scope; keeps the reducer small |
| D2 | SQLite as the durable store | Zero infrastructure, real transactions, cross-process safe |
| D3 | `requests.status` is the operational state | One unambiguous source for behavior |
| D4 | `transitions` is append-only audit history | Auditability without a competing authority; enforced by triggers |
| D5 | Store reads are pure | Predictable reads; no hidden writes in query paths |
| D6 | `pending()` sweeps, then filters | Convenient human view without pretending every read writes |
| D7 | Deadline equality is expired | No ambiguous boundary |
| D8 | Expiry is a conditional database transition | Authoritative under concurrency and process crashes |
| D9 | Idempotency requires a key plus fingerprint | Replay only when the operation is provably identical |
| D10 | Same key with a different operation conflicts | Prevents silent success for a different decision or digest |
| D11 | Approvals are bound to immutable canonical action bytes | Prevents TOCTOU mutation and digest/payload divergence |
| D12 | Time, IDs, and sleep are injected | Deterministic tests and no wall-clock coupling |
| D13 | Waiting is polling | Human timescales make pub/sub unnecessary in V1 |
| D14 | Core has no runtime dependencies | Small, portable, easy to audit |
| D15 | Authorization stays in adapters | The core has no identity provider or deployment context |
| D16 | Database triggers enforce audit and transition integrity | Defense in depth, not just application convention |
| D17 | Sync and async wait use separate sleeper protocols | No blocking sleep or synchronous DB work on the event loop |
| D18 | Demo auth is explicitly illustrative | Keeps the non-goal honest without weakening the example |
| D19 | The terminal Approval Console is the primary human surface | Readable approval cards and confirmations fit human-paced review; no browser or extra runtime is required |
| D20 | The agent bridge is non-blocking by default | A coding agent can propose and end its turn; `status`/`wait` are explicit resume points, not a held connection |
| D21 | The console binds the digest behind the decision | Humans review the payload, not a 64-character token, while the gate still verifies exact bytes |

## 18. Deferred

- Multi-approver policies, quorum, delegation, and escalation.
- A core authorization hook or policy language.
- A web UI, notification delivery, and a transactional outbox.
- Always-allow rules, batch approval, and editing a proposed payload inside the approval surface.
- Exactly-once execution claims and a `claim()` API.
- PostgreSQL and other store backends.
- Pub/sub, webhooks, SSE, and long-poll waiting.
- Full async wrappers for every facade method. V1 provides `wait_async()` and documents `asyncio.to_thread` for blocking writes.
- A database-level deferred constraint that every status update also writes a transition. SQLite lacks deferred triggers, so this remains a store contract test.
- Retention, archival, and purge tooling. Any purge must account for the append-only triggers, the request delete guard, and the no-replace guards; use a deliberate maintenance migration.
- Cursor pagination for large request lists.
- Multi-tenancy and per-tenant quotas.
- Encryption at rest and field-level redaction.
- Clock-skew tolerance windows (deliberately omitted to keep expiry unambiguous).



