"""Clock abstractions keep the fixture deterministic and fast."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


class Clock(Protocol):
    """Minimal clock interface used by Queue and Scheduler."""

    def now(self) -> float:
        """Return seconds from an arbitrary stable epoch."""


class SystemClock:
    """Monotonic production clock."""

    def now(self) -> float:
        return time.monotonic()


@dataclass
class ManualClock:
    """Mutable clock for tests and deterministic simulations."""

    value: float = 0.0

    def now(self) -> float:
        return float(self.value)

    def advance(self, seconds: float) -> float:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TypeError("seconds must be numeric")
        if seconds < 0:
            raise ValueError("cannot move the clock backwards")
        self.value += float(seconds)
        return self.value

    def set(self, value: float) -> None:
        if value < self.value:
            raise ValueError("cannot move the clock backwards")
        self.value = float(value)
