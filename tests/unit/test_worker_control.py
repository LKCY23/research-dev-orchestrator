import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rdo
from supervisor import SupervisionTerminationResult
from tmux_lifecycle import TmuxLifecycleError


class WorkerControlTests(unittest.TestCase):
    def fixture(self, root: Path, *, supervisor_state: str = "running") -> Path:
        run = root / ".agent-collab" / "runs" / "run-1"
        task = run / "tasks" / "T001"
        runtime = task / "attempts" / "A001" / "runtime"
        runtime.mkdir(parents=True)
        lock = task / ".dispatch-lock"
        lock.mkdir()
        (lock / "attempt_id").write_text("A001\n", encoding="utf-8")
        (lock / "tmux_session").write_text("rdo-one\n", encoding="utf-8")
        (run / "EVENTS.ndjson").write_text("", encoding="utf-8")
        (task / "STATUS.json").write_text(
            json.dumps(
                {
                    "task_id": "T001",
                    "state": "running",
                    "current_attempt_id": "A001",
                }
            ),
            encoding="utf-8",
        )
        (runtime / "supervisor.json").write_text(
            json.dumps(
                {
                    "state": supervisor_state,
                    "worker_pid": 53001,
                    "worker_pgid": 53001,
                    "worker_start_identity": "Sat Jul 18 13:56:49 2026",
                    "supervision_token": "b" * 32,
                    "observed_pids": [111, 222],
                    "observed_pgids": [111],
                }
            ),
            encoding="utf-8",
        )
        (runtime.parent / "ATTEMPT.json").write_text(
            json.dumps(
                {
                    "task_id": "T001",
                    "attempt_id": "A001",
                    "state": "running",
                    "runtime": {
                        "backend": "tmux",
                        "tmux_session": "rdo-one",
                    },
                }
            ),
            encoding="utf-8",
        )
        (runtime / "TMUX_SESSION.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "task_id": "T001",
                    "attempt_id": "A001",
                    "session_id": "$1",
                    "created_at_epoch": 17,
                    "session_name": "rdo-one",
                }
            ),
            encoding="utf-8",
        )
        return task

    def run_terminate(self, task: Path) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = rdo.control(
                argparse.Namespace(
                    task_dir=str(task),
                    worker_action="terminate",
                )
            )
        return code, json.loads(output.getvalue())

    def events(self, task: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in (task.parent.parent / "EVENTS.ndjson").read_text().splitlines()
        ]

    def test_message_revalidates_receipt_and_targets_immutable_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = self.fixture(Path(temporary))
            expected = {
                "session_id": "$1",
                "created_at_epoch": 17,
                "session_name": "rdo-one",
            }
            with patch.object(
                rdo,
                "revalidate_live_tmux_identity",
                return_value=expected,
            ) as revalidate, patch.object(rdo.subprocess, "run") as send:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = rdo.control(
                        argparse.Namespace(
                            task_dir=str(task),
                            worker_action="message",
                            text="continue",
                        )
                    )
            self.assertEqual(0, code)
            self.assertEqual(2, revalidate.call_count)
            self.assertEqual("$1", json.loads(output.getvalue())["session_id"])
            self.assertEqual("$1", send.call_args_list[0].args[0][3])
            self.assertEqual("$1", send.call_args_list[1].args[0][3])

    def test_interrupt_fails_closed_when_live_tmux_identity_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = self.fixture(Path(temporary))
            with patch.object(
                rdo,
                "revalidate_live_tmux_identity",
                side_effect=TmuxLifecycleError("live tmux identity does not match"),
            ), patch.object(rdo.subprocess, "run") as send:
                with self.assertRaisesRegex(SystemExit, "identity does not match"):
                    rdo.control(
                        argparse.Namespace(
                            task_dir=str(task),
                            worker_action="interrupt",
                        )
                    )
            send.assert_not_called()

    def test_terminate_uses_current_launch_identity_not_historical_pids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = self.fixture(Path(temporary))
            receipt = SupervisionTerminationResult(
                identity_verified=True,
                identity_failure_reason=None,
                root_running=True,
                targeted_pids=(53001, 53002),
                targeted_pgids=(53001,),
                surviving_pids=(),
                cleanup_verified=True,
                cleanup_failure_reason=None,
            )
            with patch.object(
                rdo,
                "terminate_current_supervision",
                return_value=receipt,
            ) as terminate:
                code, payload = self.run_terminate(task)
            self.assertEqual(0, code)
            self.assertEqual("terminated", payload["status"])
            terminate.assert_called_once_with(
                53001,
                53001,
                "Sat Jul 18 13:56:49 2026",
                "b" * 32,
            )
            self.assertEqual("worker_terminated", self.events(task)[0]["event"])

    def test_terminate_returns_failure_when_identity_cannot_be_proved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = self.fixture(Path(temporary))
            receipt = SupervisionTerminationResult(
                identity_verified=False,
                identity_failure_reason="worker_root_not_running",
                root_running=False,
                targeted_pids=(),
                targeted_pgids=(),
                surviving_pids=(),
                cleanup_verified=False,
                cleanup_failure_reason="worker_root_not_running",
            )
            with patch.object(
                rdo,
                "terminate_current_supervision",
                return_value=receipt,
            ):
                code, payload = self.run_terminate(task)
            self.assertEqual(1, code)
            self.assertEqual("identity_unverified", payload["status"])
            self.assertEqual(
                "worker_termination_failed",
                self.events(task)[0]["event"],
            )

    def test_terminate_does_not_reuse_terminal_supervisor_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = self.fixture(Path(temporary), supervisor_state="completed")
            with patch.object(rdo, "terminate_current_supervision") as terminate:
                code, payload = self.run_terminate(task)
            self.assertEqual(1, code)
            self.assertEqual("not_running", payload["status"])
            terminate.assert_not_called()
            self.assertEqual(
                "worker_termination_failed",
                self.events(task)[0]["event"],
            )


if __name__ == "__main__":
    unittest.main()
