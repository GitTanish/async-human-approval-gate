from __future__ import annotations

import pytest

from approval_gate.clock import (
    FakeAsyncSleeper,
    FakeSyncSleeper,
    ManualClock,
    SeqIds,
)
from approval_gate.gate import ApprovalGate
from approval_gate.stores import MemoryStore, SqliteStore

from support import GateEnv, StoreHarness


@pytest.fixture(params=["memory", "sqlite"])
def harness(request: pytest.FixtureRequest, tmp_path) -> StoreHarness:
    clock = ManualClock(1_000_000)
    if request.param == "memory":
        store = MemoryStore(clock=clock)
    else:
        store = SqliteStore(tmp_path / "approvals.sqlite3", clock=clock)
    prepared = StoreHarness(store=store, clock=clock)
    try:
        yield prepared
    finally:
        store.close()


@pytest.fixture(params=["memory", "sqlite"])
def gate_env(request: pytest.FixtureRequest, tmp_path) -> GateEnv:
    clock = ManualClock(1_000_000)
    if request.param == "memory":
        store = MemoryStore(clock=clock)
    else:
        store = SqliteStore(tmp_path / "gate.sqlite3", clock=clock)
    sync_sleeper = FakeSyncSleeper(clock)
    async_sleeper = FakeAsyncSleeper(clock)
    gate = ApprovalGate(
        store=store,
        clock=clock,
        ids=SeqIds(),
        sync_sleeper=sync_sleeper,
        async_sleeper=async_sleeper,
        poll_interval=0.5,
    )
    environment = GateEnv(
        gate=gate,
        store=store,
        clock=clock,
        sync_sleeper=sync_sleeper,
        async_sleeper=async_sleeper,
    )
    try:
        yield environment
    finally:
        store.close()
