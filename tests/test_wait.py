from __future__ import annotations

from datetime import timedelta

import pytest

from approval_gate.errors import (
    RequestExpired,
    RequestNotFound,
    StillPending,
)
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


def test_wait_returns_a_terminal_outcome_immediately(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose().request
    approve(gate_env, request.id)

    outcome = gate_env.gate.wait(request.id, timeout=60)

    assert outcome.status is Status.APPROVED
    assert outcome.actor == "alice@corp"
    assert gate_env.sync_sleeper.sleeps == []


def test_wait_missing_request(gate_env: GateEnv) -> None:
    with pytest.raises(RequestNotFound):
        gate_env.gate.wait("missing")


def test_wait_timeout_leaves_the_request_pending(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose(ttl=timedelta(seconds=100)).request

    with pytest.raises(StillPending):
        gate_env.gate.wait(
            request.id,
            poll_interval=0.4,
            timeout=1.0,
        )

    assert gate_env.sync_sleeper.sleeps == pytest.approx(
        [0.4, 0.4, 0.2],
    )
    assert sum(gate_env.sync_sleeper.sleeps) == pytest.approx(1.0)
    stored = gate_env.store.get(request.id)
    assert stored is not None
    assert stored.status is Status.PENDING
    assert stored.version == 0
    assert gate_env.store.get_transition(request.id) is None


def test_wait_does_not_oversleep_the_caller_timeout(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose(ttl=timedelta(seconds=100)).request

    with pytest.raises(StillPending):
        gate_env.gate.wait(
            request.id,
            poll_interval=10,
            timeout=0.25,
        )

    assert gate_env.sync_sleeper.sleeps == pytest.approx([0.25])


def test_wait_uses_the_default_poll_interval(gate_env: GateEnv) -> None:
    request = gate_env.propose(ttl=timedelta(seconds=100)).request

    with pytest.raises(StillPending):
        gate_env.gate.wait(request.id, timeout=1.0)

    assert gate_env.sync_sleeper.sleeps == pytest.approx([0.5, 0.5])


def test_wait_expires_an_overdue_request(gate_env: GateEnv) -> None:
    request = gate_env.propose(ttl=timedelta(seconds=1)).request
    gate_env.clock.advance_ms(1_000)

    outcome = gate_env.gate.wait(request.id, timeout=60)

    assert outcome.status is Status.EXPIRED
    assert outcome.actor == "system:expiry"
    assert outcome.reason == "deadline_reached"
    assert gate_env.sync_sleeper.sleeps == []
    stored = gate_env.store.get(request.id)
    assert stored is not None
    assert stored.status is Status.EXPIRED
    assert stored.version == 1


def test_wait_at_the_exact_deadline_records_expiry(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose(ttl=timedelta(seconds=1)).request
    gate_env.clock.advance_ms(1_000)

    with pytest.raises(RequestExpired):
        gate_env.gate.approve(
            request.id,
            responder="alice@corp",
            expected_digest=request.action_digest,
            idempotency_key="at-deadline",
        )

    outcome = gate_env.gate.wait(request.id, timeout=0)
    assert outcome.status is Status.EXPIRED
    assert gate_env.store.get(request.id).version == 1


def test_wait_never_global_sweeps(gate_env: GateEnv) -> None:
    decided = gate_env.propose().request
    approve(gate_env, decided.id)
    overdue = gate_env.propose(ttl=timedelta(seconds=1)).request
    gate_env.clock.advance_ms(1_000)

    outcome = gate_env.gate.wait(decided.id, timeout=0)

    assert outcome.status is Status.APPROVED
    assert gate_env.store.get(overdue.id).status is Status.PENDING
    assert gate_env.store.get_transition(overdue.id) is None


def test_wait_polls_until_another_actor_decides(
    gate_env: GateEnv,
) -> None:
    request = gate_env.propose(ttl=timedelta(seconds=100)).request
    gate = gate_env.gate
    sleeper = gate_env.sync_sleeper
    original_sleep = sleeper.sleep
    calls = {"count": 0}

    def deciding_sleep(seconds: float) -> None:
        original_sleep(seconds)
        calls["count"] += 1
        if calls["count"] == 2:
            gate.approve(
                request.id,
                responder="alice@corp",
                expected_digest=request.action_digest,
                idempotency_key="during-wait",
            )

    sleeper.sleep = deciding_sleep  # type: ignore[method-assign]

    outcome = gate_env.gate.wait(
        request.id,
        poll_interval=0.1,
        timeout=10,
    )

    assert outcome.status is Status.APPROVED
    assert outcome.actor == "alice@corp"
    assert calls["count"] == 2


def test_wait_timeout_zero_checks_before_giving_up(
    gate_env: GateEnv,
) -> None:
    pending = gate_env.propose(ttl=timedelta(seconds=100)).request
    with pytest.raises(StillPending):
        gate_env.gate.wait(pending.id, timeout=0)
    assert gate_env.sync_sleeper.sleeps == []

    decided = gate_env.propose().request
    approve(gate_env, decided.id)
    outcome = gate_env.gate.wait(decided.id, timeout=0)
    assert outcome.status is Status.APPROVED

    expired = gate_env.propose(ttl=timedelta(seconds=1)).request
    gate_env.clock.advance_ms(1_000)
    outcome = gate_env.gate.wait(expired.id, timeout=0)
    assert outcome.status is Status.EXPIRED
