import time
import unittest

from supervisor import current_termination_targets, pid_alive, run_supervised


class SupervisorTests(unittest.TestCase):
    def test_timeout_kills_descendants(self):
        result = run_supervised(
            ["/bin/sh", "-c", "sleep 30 & wait"],
            timeout_seconds=0.25,
            grace_seconds=0.05,
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)
        time.sleep(0.05)
        self.assertFalse(any(pid_alive(pid) for pid in result.observed_pids))
        self.assertEqual((), result.surviving_pids)

    def test_completion_signal_quiesces_worker_and_normalizes_exit(self):
        started = time.monotonic()
        result = run_supervised(
            ["/bin/sh", "-c", "sleep 30 & wait"],
            timeout_seconds=5,
            grace_seconds=0.05,
            completion_grace_seconds=0,
            completion_requested=lambda: time.monotonic() - started > 0.15,
        )
        self.assertTrue(result.completion_requested)
        self.assertFalse(result.timed_out)
        self.assertEqual(0, result.exit_code)
        self.assertNotEqual(0, result.child_exit_code)
        self.assertFalse(any(pid_alive(pid) for pid in result.observed_pids))
        self.assertEqual((), result.surviving_pids)

    def test_termination_uses_current_descendants_not_historical_pids(self):
        historical_pid = 41003
        current_table = {
            41001: (1, 41001),
            41002: (41001, 41001),
            historical_pid: (1, historical_pid),
        }
        pids, pgids = current_termination_targets(41001, current_table)
        self.assertEqual({41001, 41002}, pids)
        self.assertEqual({41001}, pgids)
        self.assertNotIn(historical_pid, pids)

    def test_finalization_timeout_stops_worker(self):
        result = run_supervised(
            ["/bin/sh", "-c", "sleep 30 & wait"],
            timeout_seconds=5,
            grace_seconds=0.05,
            finalization_started=lambda: True,
            finalization_timeout_seconds=0.15,
        )
        self.assertTrue(result.timed_out)
        self.assertTrue(result.finalization_timed_out)
        self.assertEqual(124, result.exit_code)
        self.assertFalse(any(pid_alive(pid) for pid in result.observed_pids))


if __name__ == "__main__":
    unittest.main()
