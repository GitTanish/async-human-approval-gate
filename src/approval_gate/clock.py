"""Injected time, identity, and sleeping dependencies."""

from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from typing import Protocol
from threading import Lock

__all__ = [
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
]


class Clock(Protocol):
    """A monotonic-enough source of UTC epoch milliseconds."""

    def now_ms(self) -> int: ...


class SystemClock:
    """The production clock."""

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class ManualClock:
    """A deterministic clock for tests and examples."""

    def __init__(self, now_ms: int = 0) -> None:
        self._now_ms = now_ms
        self._lock = Lock()

    def now_ms(self) -> int:
        with self._lock:
            return self._now_ms

    def set_ms(self, now_ms: int) -> None:
        """Set the absolute time in epoch milliseconds."""

        with self._lock:
            self._now_ms = now_ms

    def advance_ms(self, delta_ms: int) -> int:
        """Advance by integer milliseconds and return the new time."""

        with self._lock:
            self._now_ms += delta_ms
            return self._now_ms

    def advance(self, seconds: float) -> int:
        """Advance by (possibly fractional) seconds and return the new time."""

        if seconds < 0:
            raise ValueError("cannot advance a clock by a negative duration")
        return self.advance_ms(round(seconds * 1000))


class IdGenerator(Protocol):
    def new_request_id(self) -> str: ...


class UuidIds:
    """Production request IDs: a prefix plus a UUID4 hex string."""

    def __init__(self, prefix: str = "req_") -> None:
        self._prefix = prefix

    def new_request_id(self) -> str:
        return f"{self._prefix}{uuid.uuid4().hex}"


class SeqIds:
    """Deterministic request IDs for tests."""

    def __init__(self, prefix: str = "req_") -> None:
        self._prefix = prefix
        self._counter = itertools.count(1)

    def new_request_id(self) -> str:
        return f"{self._prefix}{next(self._counter)}"


class SyncSleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class AsyncSleeper(Protocol):
    async def sleep(self, seconds: float) -> None: ...


class RealSyncSleeper:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class RealAsyncSleeper:
    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeSyncSleeper:
    """Record delays and optionally advance a ``ManualClock``."""

    def __init__(self, clock: ManualClock | None = None) -> None:
        self.clock = clock
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must be non-negative")
        self.sleeps.append(seconds)
        if self.clock is not None:
            self.clock.advance(seconds)


class FakeAsyncSleeper:
    """Async counterpart to ``FakeSyncSleeper`` that yields to the loop."""

    def __init__(self, clock: ManualClock | None = None) -> None:
        self.clock = clock
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must be non-negative")
        self.sleeps.append(seconds)
        if self.clock is not None:
            self.clock.advance(seconds)
        await asyncio.sleep(0)
