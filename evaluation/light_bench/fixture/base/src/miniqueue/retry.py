"""Deterministic retry delay policy."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidJobError


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential retry policy with an optional deterministic spread.

    ``attempt`` is one-based: attempt 1 is the execution that just failed.
    The first failure therefore waits exactly ``base_delay`` before applying
    the multiplier on later failures.
    """

    base_delay: float = 1.0
    multiplier: float = 2.0
    max_delay: float = 300.0
    spread: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("base_delay", self.base_delay),
            ("multiplier", self.multiplier),
            ("max_delay", self.max_delay),
            ("spread", self.spread),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidJobError(f"{name} must be numeric")
        if self.base_delay < 0:
            raise InvalidJobError("base_delay cannot be negative")
        if self.multiplier < 1:
            raise InvalidJobError("multiplier must be at least one")
        if self.max_delay < self.base_delay:
            raise InvalidJobError("max_delay cannot be below base_delay")
        if not 0 <= self.spread <= 1:
            raise InvalidJobError("spread must be between zero and one")

    def delay_for_attempt(self, attempt: int, *, key: str = "") -> float:
        """Return the delay after a one-based failed attempt.

        Spread is deterministic per key and attempt. It prevents synchronized
        retries in examples without introducing random or flaky tests.
        """

        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise InvalidJobError("attempt must be a positive integer")
        raw = self.base_delay * (self.multiplier ** (attempt - 1))
        capped = min(raw, self.max_delay)
        if self.spread == 0 or capped == 0:
            return float(capped)
        bucket = self._stable_bucket(key, attempt)
        factor = 1 - self.spread + (2 * self.spread * bucket)
        return min(float(self.max_delay), capped * factor)

    @staticmethod
    def _stable_bucket(key: str, attempt: int) -> float:
        data = f"{key}:{attempt}".encode("utf-8")
        value = 2166136261
        for byte in data:
            value ^= byte
            value = (value * 16777619) & 0xFFFFFFFF
        return value / 0xFFFFFFFF
