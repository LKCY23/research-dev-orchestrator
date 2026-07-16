from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401
from miniqueue import (
    InvalidJobError,
    JobState,
    JsonStore,
    LeaseError,
    ManualClock,
    Queue,
    QueueConfig,
    RetryPolicy,
)


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(100)
        self.queue = Queue(
            clock=self.clock,
            retry_policy=RetryPolicy(base_delay=5, multiplier=2, max_delay=60),
        )

    def test_priority_then_age_controls_leasing(self) -> None:
        self.queue.enqueue({"kind": "low"}, job_id="low", priority=1)
        self.queue.enqueue({"kind": "high"}, job_id="high", priority=4)
        leased = self.queue.lease_next("worker")
        self.assertIsNotNone(leased)
        self.assertEqual("high", leased.job_id)

    def test_delayed_job_is_not_available_early(self) -> None:
        self.queue.enqueue({"kind": "later"}, job_id="later", delay_seconds=3)
        self.assertIsNone(self.queue.lease_next("worker"))
        self.clock.advance(3)
        self.assertEqual("later", self.queue.lease_next("worker").job_id)

    def test_expired_lease_can_be_reclaimed(self) -> None:
        self.queue.enqueue({"kind": "work"}, job_id="work")
        first = self.queue.lease_next("worker-a", ttl_seconds=4)
        self.assertEqual(104, first.leased_until)
        self.clock.advance(4)
        second = self.queue.lease_next("worker-b")
        self.assertEqual("worker-b", second.lease_owner)
        self.assertEqual(2, second.attempts)

    def test_only_owner_can_acknowledge(self) -> None:
        self.queue.enqueue({"kind": "work"}, job_id="work")
        self.queue.lease_next("owner")
        with self.assertRaises(LeaseError):
            self.queue.acknowledge("work", "other")
        completed = self.queue.acknowledge("work", "owner")
        self.assertEqual(JobState.SUCCEEDED, completed.state)

    def test_failure_requeues_then_becomes_dead(self) -> None:
        self.queue.enqueue(
            {"kind": "work"}, job_id="work", max_attempts=2
        )
        self.queue.lease_next("worker")
        retry = self.queue.fail("work", "worker", "first")
        self.assertEqual(JobState.QUEUED, retry.state)
        self.assertEqual(105, retry.available_at)
        self.clock.advance(5)
        self.queue.lease_next("worker")
        dead = self.queue.fail("work", "worker", "second")
        self.assertEqual(JobState.DEAD, dead.state)

    def test_default_lease_is_used_for_omitted_ttl(self) -> None:
        self.queue.enqueue({"kind": "work"}, job_id="work")
        leased = self.queue.lease_next("worker")
        self.assertEqual(130, leased.leased_until)

    def test_payload_must_be_json_object(self) -> None:
        with self.assertRaises(InvalidJobError):
            self.queue.enqueue(["not", "an", "object"])

    def test_json_store_survives_new_queue_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            first = Queue(JsonStore(path), clock=self.clock)
            first.enqueue({"kind": "persist"}, job_id="persist")
            second = Queue(JsonStore(path), clock=self.clock)
            self.assertEqual("persist", second.get("persist").job_id)

    def test_stats_count_all_base_states(self) -> None:
        self.queue.enqueue({"kind": "one"}, job_id="one")
        self.queue.enqueue({"kind": "two"}, job_id="two")
        self.queue.lease_next("worker")
        stats = self.queue.stats()
        self.assertEqual(2, stats.total)
        self.assertEqual(1, stats.queued)
        self.assertEqual(1, stats.leased)


if __name__ == "__main__":
    unittest.main()
