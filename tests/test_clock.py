from __future__ import annotations

import uuid

import pytest

from approval_gate.clock import (
    FakeAsyncSleeper,
    FakeSyncSleeper,
    ManualClock,
    SeqIds,
    SystemClock,
    UuidIds,
)


def test_manual_clock_starts_at_zero_and_supports_set() -> None:
    clock = ManualClock()
    assert clock.now_ms() == 0
    clock.set_ms(1_234)
    assert clock.now_ms() == 1_234


def test_manual_clock_advance_ms_and_seconds() -> None:
    clock = ManualClock(100)
    assert clock.advance_ms(50) == 150
    assert clock.advance(1.5) == 1_650
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_system_clock_truncates_nanoseconds_to_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "approval_gate.clock.time.time_ns",
        lambda: 1_700_000_000_123_456_789,
    )
    assert SystemClock().now_ms() == 1_700_000_000_123


def test_uuid_ids_are_prefixed_and_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = iter(uuid.UUID(int=index) for index in range(101))
    monkeypatch.setattr(
        "approval_gate.clock.uuid.uuid4",
        lambda: next(values),
    )
    ids = UuidIds()
    generated = {ids.new_request_id() for _ in range(100)}
    assert len(generated) == 100
    assert all(value.startswith("req_") for value in generated)
    assert all(len(value) == len("req_") + 32 for value in generated)
    assert UuidIds(prefix="approval_").new_request_id().startswith(
        "approval_",
    )


def test_seq_ids_are_deterministic() -> None:
    ids = SeqIds()
    assert [ids.new_request_id() for _ in range(3)] == [
        "req_1",
        "req_2",
        "req_3",
    ]


def test_fake_sync_sleeper_records_and_advances_clock() -> None:
    clock = ManualClock(1_000)
    sleeper = FakeSyncSleeper(clock)
    sleeper.sleep(0.25)
    sleeper.sleep(2)
    assert sleeper.sleeps == [0.25, 2]
    assert clock.now_ms() == 3_250
    with pytest.raises(ValueError):
        sleeper.sleep(-1)


async def test_fake_async_sleeper_records_and_advances_clock() -> None:
    clock = ManualClock(1_000)
    sleeper = FakeAsyncSleeper(clock)
    await sleeper.sleep(0.5)
    assert sleeper.sleeps == [0.5]
    assert clock.now_ms() == 1_500
    with pytest.raises(ValueError):
        await sleeper.sleep(-1)
