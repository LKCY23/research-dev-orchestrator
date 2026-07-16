from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from miniqueue.errors import InvalidJobError
from miniqueue.retry import RetryPolicy


class RetryPolicyTests(unittest.TestCase):
    def test_first_failure_uses_base_delay(self) -> None:
        policy = RetryPolicy(base_delay=3, multiplier=2, max_delay=30)
        self.assertEqual(3, policy.delay_for_attempt(1))

    def test_later_failures_grow_and_cap(self) -> None:
        policy = RetryPolicy(base_delay=3, multiplier=2, max_delay=10)
        self.assertEqual([3, 6, 10, 10], [
            policy.delay_for_attempt(attempt) for attempt in range(1, 5)
        ])

    def test_attempt_is_one_based(self) -> None:
        with self.assertRaises(InvalidJobError):
            RetryPolicy().delay_for_attempt(0)

    def test_spread_is_repeatable(self) -> None:
        policy = RetryPolicy(base_delay=10, spread=0.2)
        self.assertEqual(
            policy.delay_for_attempt(2, key="job-a"),
            policy.delay_for_attempt(2, key="job-a"),
        )


if __name__ == "__main__":
    unittest.main()
