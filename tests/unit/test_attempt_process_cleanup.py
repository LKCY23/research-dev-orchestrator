import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from protocol_cli import cmd_terminate_attempt_processes


class AttemptProcessCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_path = Path(self.temporary.name) / "supervisor.json"
        self.state = {
            "worker_pid": 52001,
            "worker_pgid": 52001,
            "worker_start_identity": "Tue Sep 22 12:00:00 2026",
            "supervision_token": "a" * 32,
            "observed_pids": [52001, 52002],
            "observed_pgids": [52001],
        }
        self.write_state()

    def write_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def cleanup(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cmd_terminate_attempt_processes(
                argparse.Namespace(supervisor_state=str(self.state_path))
            )
        return code, json.loads(output.getvalue())

    def test_verified_live_root_uses_identity_checked_cleanup(self):
        result = SimpleNamespace(
            identity_verified=True,
            targeted_pids=(52001, 52002),
            targeted_pgids=(52001,),
            surviving_pids=(),
            cleanup_verified=True,
            cleanup_failure_reason=None,
        )
        with (
            patch("protocol_cli.terminate_current_supervision", return_value=result) as current,
            patch("protocol_cli.terminate_processes") as fallback,
        ):
            code, payload = self.cleanup()
        self.assertEqual(0, code)
        self.assertTrue(payload["terminated"])
        current.assert_called_once_with(
            52001, 52001, self.state["worker_start_identity"], "a" * 32
        )
        fallback.assert_not_called()

    def test_absent_or_reused_root_never_targets_historical_process_ids(self):
        def terminate(pgids, pids, **kwargs):
            self.assertEqual(set(), pgids)
            self.assertEqual(set(), pids)
            self.assertIsNone(kwargs.get("root_pid"))
            self.assertEqual("a" * 32, kwargs["supervision_token"])
            kwargs["observed_pids"].add(53001)
            kwargs["observed_pgids"].add(53001)
            return ()

        for reason in ("worker_root_not_running", "worker_root_pid_reused", "worker_root_pgid_mismatch"):
            with (
                self.subTest(reason=reason),
                patch("protocol_cli.terminate_current_supervision", return_value=SimpleNamespace(
                    identity_verified=False, identity_failure_reason=reason,
                )),
                patch("protocol_cli.terminate_processes", side_effect=terminate) as fallback,
            ):
                code, payload = self.cleanup()
                self.assertEqual(0, code)
                self.assertTrue(payload["terminated"])
                self.assertEqual([53001], payload["observed_pids"])
                fallback.assert_called_once()

    def test_unverifiable_token_scan_fails_even_without_reported_survivors(self):
        def terminate(*args, **kwargs):
            kwargs["cleanup_observation"].update(verified=False, reason="process_table_unavailable")
            return ()

        with (
            patch("protocol_cli.terminate_current_supervision", return_value=SimpleNamespace(identity_verified=False)),
            patch("protocol_cli.terminate_processes", side_effect=terminate),
        ):
            code, payload = self.cleanup()
        self.assertEqual(2, code)
        self.assertFalse(payload["terminated"])
        self.assertFalse(payload["cleanup_verified"])
        self.assertEqual("process_table_unavailable", payload["reason"])

    def test_survivors_are_not_reported_as_successful_cleanup(self):
        with patch("protocol_cli.terminate_current_supervision", return_value=SimpleNamespace(
            identity_verified=True,
            targeted_pids=(52001, 52002),
            targeted_pgids=(52001,),
            surviving_pids=(52002,),
            cleanup_verified=False,
            cleanup_failure_reason="surviving_processes",
        )):
            code, payload = self.cleanup()
        self.assertEqual(2, code)
        self.assertFalse(payload["terminated"])
        self.assertEqual([52002], payload["surviving_pids"])

    def test_invalid_or_missing_token_never_signals_processes(self):
        for token in (None, "", 123, "not-a-token"):
            with self.subTest(token=token):
                self.state["supervision_token"] = token
                self.write_state()
                with (
                    patch("protocol_cli.terminate_current_supervision") as current,
                    patch("protocol_cli.terminate_processes") as fallback,
                ):
                    code, payload = self.cleanup()
                self.assertEqual(2, code)
                self.assertFalse(payload["terminated"])
                current.assert_not_called()
                fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
