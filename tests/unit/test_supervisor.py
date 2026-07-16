import hashlib
import json
import sys
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from supervisor import (
    AttemptDeadline,
    current_termination_targets,
    load_or_create_attempt_deadline,
    pid_alive,
    run_supervised,
    terminate_processes,
)


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

    def test_natural_parent_exit_cleans_reparented_setsid_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / "late.txt"
            child = (
                "import pathlib,time; time.sleep(0.6); "
                f"pathlib.Path({str(sentinel)!r}).write_text('late')"
            )
            parent = (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)"
            )
            result = run_supervised(
                [sys.executable, "-c", parent],
                timeout_seconds=2,
                grace_seconds=0.05,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.assertEqual(0, result.exit_code)
            self.assertGreaterEqual(len(result.observed_pids), 2)
            time.sleep(0.75)
            self.assertFalse(sentinel.exists())
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
            timeout_seconds=0.1,
            grace_seconds=0.05,
            finalization_started=lambda: True,
            finalization_timeout_seconds=0.15,
        )
        self.assertTrue(result.timed_out)
        self.assertTrue(result.finalization_timed_out)
        self.assertEqual(124, result.exit_code)
        self.assertFalse(any(pid_alive(pid) for pid in result.observed_pids))

    def test_finalization_entry_before_hard_deadline_gets_full_grace(self):
        started_monotonic = time.monotonic()
        started_epoch = time.time()

        def finalization_epoch() -> float | None:
            return (
                started_epoch + 0.15
                if time.monotonic() - started_monotonic >= 0.15
                else None
            )

        result = run_supervised(
            ["/bin/sh", "-c", "sleep 0.32"],
            timeout_seconds=0.2,
            grace_seconds=0.05,
            finalization_started=finalization_epoch,
            finalization_timeout_seconds=0.3,
        )
        self.assertFalse(result.timed_out)
        self.assertTrue(result.finalization_started)
        self.assertIsNone(result.timeout_phase)
        self.assertGreater(result.elapsed_seconds, 0.2)

    def test_finalization_entry_is_latched_and_cannot_be_reset(self):
        emitted = False
        def one_shot_epoch() -> float | None:
            nonlocal emitted
            if emitted:
                return None
            emitted = True
            return time.time()

        result = run_supervised(
            ["/bin/sh", "-c", "sleep 30 & wait"],
            timeout_seconds=0.1,
            grace_seconds=0.05,
            finalization_started=one_shot_epoch,
            finalization_timeout_seconds=0.2,
        )
        self.assertTrue(result.timed_out)
        self.assertEqual("finalization", result.timeout_phase)
        self.assertTrue(result.finalization_timed_out)
        self.assertGreaterEqual(result.elapsed_seconds, 0.28)

    def test_early_finalization_does_not_shorten_remaining_attempt_time(self):
        result = run_supervised(
            ["/bin/sh", "-c", "sleep 0.25"],
            timeout_seconds=0.35,
            grace_seconds=0.05,
            finalization_started=lambda: True,
            finalization_timeout_seconds=0.1,
        )
        self.assertFalse(result.timed_out)
        self.assertEqual(0, result.exit_code)
        self.assertGreater(result.elapsed_seconds, 0.2)

    def test_publication_grace_crossing_deadline_remains_successful(self):
        started = time.monotonic()
        published_at = time.time() + 0.1
        result = run_supervised(
            ["/bin/sh", "-c", "sleep 30 & wait"],
            timeout_seconds=0.2,
            grace_seconds=0.05,
            completion_grace_seconds=0.15,
            completion_requested=lambda: (
                published_at if time.monotonic() - started >= 0.1 else None
            ),
        )
        self.assertTrue(result.completion_requested)
        self.assertFalse(result.timed_out)
        self.assertEqual(0, result.exit_code)
        self.assertGreater(result.elapsed_seconds, 0.2)

    def test_natural_exit_near_deadline_is_not_retroactively_timed_out(self):
        result = run_supervised(
            ["/bin/sleep", "0.12"],
            timeout_seconds=0.2,
            grace_seconds=0.05,
        )
        self.assertFalse(result.timed_out)
        self.assertEqual(0, result.exit_code)

    def test_small_deadline_overrun_is_not_accepted_as_success(self):
        result = run_supervised(
            ["/bin/sleep", "0.26"],
            timeout_seconds=0.2,
            grace_seconds=0.05,
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)

    def test_deadline_file_is_reused_without_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            deadline_path = Path(temporary) / "DEADLINE.json"
            first = load_or_create_attempt_deadline(
                deadline_path,
                attempt_timeout_seconds=0.3,
                finalization_grace_seconds=0.2,
                reminder_seconds=0.1,
            )
            time.sleep(0.12)
            second = load_or_create_attempt_deadline(
                deadline_path,
                attempt_timeout_seconds=0.3,
                finalization_grace_seconds=0.2,
                reminder_seconds=0.1,
            )
            self.assertEqual(
                first["execution_deadline_at_epoch"],
                second["execution_deadline_at_epoch"],
            )
            state = AttemptDeadline.from_payload(second).state()
            self.assertLess(state["remaining_seconds"], 0.25)

    def test_deadline_file_rejects_tampered_arithmetic_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deadline_path = root / "DEADLINE.json"
            payload = load_or_create_attempt_deadline(
                deadline_path,
                attempt_timeout_seconds=0.3,
                finalization_grace_seconds=0.2,
                reminder_seconds=0.1,
            )
            payload["execution_deadline_at_epoch"] += 100
            deadline_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not equal"):
                load_or_create_attempt_deadline(
                    deadline_path,
                    attempt_timeout_seconds=0.3,
                    finalization_grace_seconds=0.2,
                    reminder_seconds=0.1,
                )

    def test_result_keeps_the_deadline_digest_loaded_before_worker_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deadline_path = root / "DEADLINE.json"
            load_or_create_attempt_deadline(
                deadline_path,
                attempt_timeout_seconds=1,
                finalization_grace_seconds=0.5,
                reminder_seconds=0.1,
            )
            original_digest = hashlib.sha256(deadline_path.read_bytes()).hexdigest()
            replacement = json.loads(deadline_path.read_text(encoding="utf-8"))
            replacement["started_at_epoch"] += 10
            replacement["execution_deadline_at_epoch"] += 10
            child = (
                "import json,pathlib; "
                f"pathlib.Path({str(deadline_path)!r}).write_text("
                f"json.dumps({replacement!r})+'\\n')"
            )
            result = run_supervised(
                [sys.executable, "-c", child],
                timeout_seconds=1,
                finalization_timeout_seconds=0.5,
                deadline_reminder_seconds=0.1,
                deadline_path=deadline_path,
            )
            self.assertEqual(0, result.exit_code)
            self.assertEqual(original_digest, result.deadline_sha256)
            self.assertNotEqual(
                original_digest,
                hashlib.sha256(deadline_path.read_bytes()).hexdigest(),
            )

            deadline_path.unlink()
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            deadline_path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                load_or_create_attempt_deadline(
                    deadline_path,
                    attempt_timeout_seconds=0.3,
                    finalization_grace_seconds=0.2,
                    reminder_seconds=0.1,
                )

    def test_state_contains_structured_deadline_reminder(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "supervisor.json"
            result = run_supervised(
                ["/bin/sh", "-c", "sleep 0.05"],
                timeout_seconds=0.3,
                state_path=state_path,
                deadline_reminder_seconds=0.5,
            )
            self.assertEqual(0, result.exit_code)
            state = json.loads(state_path.read_text())
            self.assertEqual(
                "attempt_deadline_approaching",
                state["deadline"]["reminder"]["code"],
            )

    def test_signal_handler_cannot_spawn_an_escaping_detached_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / "late.txt"
            child = (
                "import pathlib,time; time.sleep(0.7); "
                f"pathlib.Path({str(sentinel)!r}).write_text('late')"
            )
            worker = (
                "import signal,subprocess,sys,time; "
                "spawned=[False]; "
                "signal.signal(signal.SIGINT, lambda *_: "
                "(subprocess.Popen([sys.executable,'-c',"
                f"{child!r}], start_new_session=True), spawned.__setitem__(0,True))); "
                "\nwhile True: time.sleep(0.05)"
            )
            result = run_supervised(
                [sys.executable, "-c", worker],
                timeout_seconds=0.2,
                grace_seconds=0.1,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.assertTrue(result.timed_out)
            time.sleep(0.8)
            self.assertFalse(sentinel.exists())
            self.assertEqual((), result.surviving_pids)

    def test_nested_supervision_preserves_outer_cleanup_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "late.txt"
            inner_script = root / "inner.py"
            inner_script.write_text(
                "\n".join(
                    [
                        "import signal, subprocess, sys, time",
                        f"child = \"import pathlib,time; time.sleep(.6); pathlib.Path({str(sentinel)!r}).write_text('late')\"",
                        "signal.signal(signal.SIGINT, lambda *_: subprocess.Popen([sys.executable, '-c', child], start_new_session=True))",
                        "while True: time.sleep(.05)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            outer_script = root / "outer.py"
            outer_script.write_text(
                "\n".join(
                    [
                        "import pathlib, sys",
                        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2] / 'scripts')!r})",
                        "from supervisor import run_supervised",
                        f"run_supervised([sys.executable, {str(inner_script)!r}], timeout_seconds=30, grace_seconds=.1)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = run_supervised(
                [sys.executable, str(outer_script)],
                timeout_seconds=0.25,
                grace_seconds=0.1,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.assertTrue(result.timed_out)
            self.assertTrue(result.cleanup_verified)
            time.sleep(0.75)
            self.assertFalse(sentinel.exists())
            self.assertEqual((), result.surviving_pids)

    def test_unavailable_process_table_is_reported_as_unverified_cleanup(self):
        with patch("supervisor._process_table", side_effect=OSError):
            result = run_supervised(
                ["/bin/sh", "-c", "sleep 30"],
                timeout_seconds=0.1,
                grace_seconds=0.05,
            )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.cleanup_verified)
        self.assertEqual("process_table_unavailable", result.cleanup_failure_reason)

    def test_process_table_loss_during_cleanup_is_sticky_failure(self):
        calls = 0

        def process_table():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {49001: (1, 49001)}
            raise OSError("transient ps failure")

        observation = {"verified": True, "reason": None}
        with patch("supervisor._process_table", side_effect=process_table):
            survivors = terminate_processes(
                {49001},
                {49001},
                root_pid=49001,
                grace_seconds=0.01,
                cleanup_observation=observation,
            )
        self.assertEqual((), survivors)
        self.assertFalse(observation["verified"])
        self.assertEqual(
            "process_table_unavailable_during_cleanup",
            observation["reason"],
        )


if __name__ == "__main__":
    unittest.main()
