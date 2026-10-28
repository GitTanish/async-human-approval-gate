from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from typing import Any

import pytest

from approval_gate.errors import RequestNotFound, StillPending
from approval_gate.models import Status

from support import GateEnv


def approve(env: GateEnv, request_id: str) -> None:
    request = env.store.get(request_id)
    assert request is not None
    env.gate.approve(
        request_id,
        responder="alice@corp",
        expected_digest=request.action_digest,
        idempotency_key="key",
    )


async def test_wait_async_returns_terminal_immediately(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose().request
    approve(gate_env, request.id)

    outcome = await gate_env.gate.wait_async(request.id, timeout=60)

    assert outcome.status is Status.APPROVED
    assert outcome.actor == "alice@corp"
    assert gate_env.async_sleeper.sleeps == []


async def test_wait_async_missing_request(gate_env: GateEnv) -> None:
    with pytest.raises(RequestNotFound):
        await gate_env.gate.wait_async("missing")


async def test_wait_async_timeout_leaves_the_request_pending(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose(ttl=timedelta(seconds=100)).request

    with pytest.raises(StillPending):
        await gate_env.gate.wait_async(
            request.id,
            poll_interval=0.2,
            timeout=0.5,
        )

    assert gate_env.async_sleeper.sleeps == pytest.approx([0.2, 0.2, 0.1])
    stored = gate_env.store.get(request.id)
    assert stored is not None
    assert stored.status is Status.PENDING
    assert stored.version == 0
    assert gate_env.store.get_transition(request.id) is None


async def test_wait_async_expires_an_overdue_request(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose(ttl=timedelta(seconds=1)).request
    gate_env.clock.advance_ms(1_000)

    outcome = await gate_env.gate.wait_async(request.id, timeout=60)

    assert outcome.status is Status.EXPIRED
    assert outcome.actor == "system:expiry"
    assert gate_env.async_sleeper.sleeps == []
    assert gate_env.store.get(request.id).version == 1


async def test_wait_async_yields_to_the_event_loop(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose(ttl=timedelta(seconds=100)).request
    gate = gate_env.gate
    ran = False

    async def decide_soon() -> None:
        nonlocal ran
        for _ in range(3):
            await asyncio.sleep(0)
        ran = True
        gate.approve(
            request.id,
            responder="alice@corp",
            expected_digest=request.action_digest,
            idempotency_key="concurrent",
        )

    waiter = asyncio.create_task(
        gate.wait_async(request.id, poll_interval=0.5, timeout=5),
    )
    decider = asyncio.create_task(decide_soon())

    outcome = await waiter
    await decider

    assert ran is True
    assert outcome.status is Status.APPROVED


async def test_wait_async_offloads_store_calls_to_a_worker_thread(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose().request
    approve(gate_env, request.id)

    class ThreadRecordingStore:
        def __init__(self, inner: Any) -> None:
            self.inner = inner
            self.threads: list[int] = []

        def _record(self) -> None:
            self.threads.append(threading.get_ident())

        def get(self, request_id: str):
            self._record()
            return self.inner.get(request_id)

        def outcome(self, request_id: str):
            self._record()
            return self.inner.outcome(request_id)

        def sweep(self, *, request_id: str | None = None):
            self._record()
            return self.inner.sweep(request_id=request_id)

    recording = ThreadRecordingStore(gate_env.store)

    from approval_gate.clock import SeqIds
    from approval_gate.gate import ApprovalGate

    gate = ApprovalGate(
        store=recording,  # type: ignore[arg-type]
        clock=gate_env.clock,
        ids=SeqIds(),
        sync_sleeper=gate_env.sync_sleeper,
        async_sleeper=gate_env.async_sleeper,
    )

    outcome = await gate.wait_async(request.id)

    assert outcome.status is Status.APPROVED
    assert recording.threads
    assert all(
        thread_id != threading.main_thread().ident
        for thread_id in recording.threads
    )


async def test_cancelling_wait_async_never_decides(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose(ttl=timedelta(seconds=100)).request

    waiter = asyncio.create_task(
        gate_env.gate.wait_async(request.id, poll_interval=0.5),
    )
    await asyncio.sleep(0)
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    stored = gate_env.store.get(request.id)
    assert stored is not None
    assert stored.status is Status.PENDING
    assert stored.version == 0
    assert gate_env.store.get_transition(request.id) is None

    approve(gate_env, request.id)
    outcome = await gate_env.gate.wait_async(request.id, timeout=0)
    assert outcome.status is Status.APPROVED
