import os
import time
import unittest

from supervisor import pid_alive, run_supervised


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


if __name__ == "__main__":
    unittest.main()
