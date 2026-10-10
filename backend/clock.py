"""Injectable clocks so the daemon can run against simulated time in tests."""
from __future__ import annotations

import asyncio
import time


class RealClock:
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class FakeClock:
    """Simulated time: sleep() advances the clock instantly."""

    def __init__(self, start: float = 1_700_000_000.0):
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += max(0.0, seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


class AcceleratedClock:
    """Wall-clock time sped up by `speed`, for watching a mock session live."""

    def __init__(self, speed: float = 20.0):
        self.speed = speed
        self._t0_real = time.time()
        self._t0 = self._t0_real

    def now(self) -> float:
        return self._t0 + (time.time() - self._t0_real) * self.speed

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds) / self.speed)
