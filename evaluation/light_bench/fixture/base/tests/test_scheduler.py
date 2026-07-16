from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from miniqueue import JobState, ManualClock, Queue, Scheduler


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(20)
        self.queue = Queue(clock=self.clock)

    def test_run_once_dispatches_and_acknowledges(self) -> None:
        self.queue.enqueue({"kind": "double", "value": 4}, job_id="job")
        scheduler = Scheduler(
            self.queue,
            {"double": lambda payload: payload["value"] * 2},
            worker_id="worker",
        )
        result = scheduler.run_once()
        self.assertEqual("succeeded", result.status)
        self.assertEqual(8, result.result)
        self.assertEqual(JobState.SUCCEEDED, self.queue.get("job").state)

    def test_handler_error_is_recorded_as_failure(self) -> None:
        self.queue.enqueue({"kind": "explode"}, job_id="job")

        def explode(payload):
            raise RuntimeError("boom")

        result = Scheduler(
            self.queue, {"explode": explode}, worker_id="worker"
        ).run_once()
        self.assertEqual("failed", result.status)
        self.assertIn("RuntimeError: boom", result.error)
        self.assertIn("RuntimeError: boom", self.queue.get("job").last_error)

    def test_unknown_handler_is_a_normal_failure(self) -> None:
        self.queue.enqueue({"kind": "unknown"}, job_id="job")
        result = Scheduler(
            self.queue, {"known": lambda payload: None}, worker_id="worker"
        ).run_once()
        self.assertEqual("failed", result.status)

    def test_idle_queue_stops_batch(self) -> None:
        scheduler = Scheduler(
            self.queue, {"known": lambda payload: None}, worker_id="worker"
        )
        self.assertEqual([], scheduler.run_until_idle())


if __name__ == "__main__":
    unittest.main()
