import argparse
import json
import tempfile
import unittest
from pathlib import Path

from protocol_cli import _failed_attempt_outcome, cmd_reconcile_dispatch_exit


class AttemptOutcomeTests(unittest.TestCase):
    def write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def make_active(self, root: Path) -> tuple[Path, Path, Path]:
        task = root / "tasks" / "T001"
        attempt = task / "attempts" / "A001"
        status = task / "STATUS.json"
        self.write_json(
            status,
            {
                "task_id": "T001",
                "state": "running",
                "previous_state": "pending",
                "owner": "worker",
                "current_attempt_id": "A001",
                "state_history": [
                    {
                        "from": "pending",
                        "to": "running",
                        "actor": "dispatch",
                        "at": "2026-07-16T00:00:00Z",
                    }
                ],
            },
        )
        metadata = {
            "attempt_id": "A001",
            "task_id": "T001",
            "phase": "execution",
            "state": "running",
            "outcome": None,
            "handoff_valid": None,
            "handoff_state": None,
            "ended_at": None,
            "exit_code": None,
        }
        self.write_json(attempt / "ATTEMPT.json", metadata)
        self.write_json(attempt / "runtime" / "DISPATCH_ATTEMPT.json", metadata)
        return task, attempt, status

    def reconcile(
        self,
        root: Path,
        task: Path,
        attempt: Path,
        status: Path,
        *,
        dispatch_exit_code: int = 9,
    ) -> int:
        return cmd_reconcile_dispatch_exit(
            argparse.Namespace(
                status_path=str(status),
                task_dir=str(task),
                attempt_path=str(attempt / "ATTEMPT.json"),
                attempt_id="A001",
                startup_path=str(attempt / "runtime" / "STARTUP.json"),
                supervisor_result=str(attempt / "supervisor-result.json"),
                timeout_marker=str(attempt / "runtime" / "DISPATCH_TIMEOUT.json"),
                cleanup_result=str(attempt / "runtime" / "CLEANUP.json"),
                dispatch_exit_code=str(dispatch_exit_code),
                run_dir="",
                run_id="",
                task_id="T001",
            )
        )

    def test_startup_failure_reconciles_to_environment_blocker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, attempt, status = self.make_active(root)
            self.write_json(
                attempt / "runtime" / "STARTUP.json",
                {
                    "state": "worker_startup_failed",
                    "failure": {
                        "code": "session_not_found",
                        "message": "session is missing",
                    },
                },
            )
            self.assertEqual(0, self.reconcile(root, task, attempt, status))
            metadata = json.loads((attempt / "ATTEMPT.json").read_text())
            task_status = json.loads(status.read_text())
            self.assertEqual("invalid_handoff", metadata["state"])
            self.assertEqual("startup_failed", metadata["outcome"])
            self.assertEqual("blocked", task_status["state"])
            self.assertEqual("environment", task_status["blocker_type"])
            self.assertEqual(0, self.reconcile(root, task, attempt, status))

    def test_timeout_reconciles_to_budget_blocker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, attempt, status = self.make_active(root)
            self.write_json(
                attempt / "supervisor-result.json",
                {"timed_out": True, "exit_code": 124},
            )
            self.assertEqual(0, self.reconcile(root, task, attempt, status))
            metadata = json.loads((attempt / "ATTEMPT.json").read_text())
            task_status = json.loads(status.read_text())
            self.assertEqual("timed_out_unfinalized", metadata["outcome"])
            self.assertEqual(124, metadata["exit_code"])
            self.assertEqual("budget", task_status["blocker_type"])

    def test_abnormal_dispatch_exit_after_startup_is_execution_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, attempt, status = self.make_active(root)
            self.write_json(
                attempt / "runtime" / "STARTUP.json",
                {"state": "worker_started"},
            )
            self.assertEqual(0, self.reconcile(root, task, attempt, status))
            metadata = json.loads((attempt / "ATTEMPT.json").read_text())
            self.assertEqual("execution_failed", metadata["outcome"])
            self.assertEqual("blocked", json.loads(status.read_text())["state"])

    def test_missing_attempt_is_recovered_from_dispatch_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, attempt, status = self.make_active(root)
            (attempt / "ATTEMPT.json").unlink()
            self.assertEqual(0, self.reconcile(root, task, attempt, status))
            metadata = json.loads((attempt / "ATTEMPT.json").read_text())
            self.assertTrue(metadata["recovered_from_dispatch_snapshot"])
            self.assertEqual("execution_failed", metadata["outcome"])
            self.assertEqual("blocked", json.loads(status.read_text())["state"])

    def test_corrupt_attempt_is_quarantined_and_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, attempt, status = self.make_active(root)
            (attempt / "ATTEMPT.json").write_text("{bad json", encoding="utf-8")
            self.assertEqual(0, self.reconcile(root, task, attempt, status))
            metadata = json.loads((attempt / "ATTEMPT.json").read_text())
            self.assertTrue(metadata["recovered_from_dispatch_snapshot"])
            self.assertEqual(1, len(list(attempt.glob("ATTEMPT.corrupt-*.json"))))

    def test_cleanup_survivors_retain_an_irrecoverable_blocker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, attempt, status = self.make_active(root)
            self.write_json(
                attempt / "runtime" / "CLEANUP.json",
                {
                    "terminated": True,
                    "observed_pids": [101, 102],
                    "observed_pgids": [101],
                    "surviving_pids": [102],
                },
            )
            self.assertEqual(3, self.reconcile(root, task, attempt, status))
            metadata = json.loads((attempt / "ATTEMPT.json").read_text())
            snapshot = json.loads(
                (attempt / "runtime" / "DISPATCH_ATTEMPT.json").read_text()
            )
            task_status = json.loads(status.read_text())
            self.assertEqual([102], metadata["cleanup_failure"]["surviving_pids"])
            self.assertEqual(metadata, snapshot)
            self.assertEqual("irrecoverable", task_status["blocker_type"])
            self.assertIn("dispatch lock was retained", task_status["blocking_reason"])

    def test_legacy_template_does_not_turn_execution_failure_into_invalid_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "tasks" / "T001"
            attempt = task / "attempts" / "A001"
            self.write_json(
                attempt / "ATTEMPT.json",
                {"attempt_id": "A001", "artifact_protocol_version": 1},
            )
            self.write_json(task / "HANDOFF.json", {"_template": True})
            self.assertEqual(
                "execution_failed",
                _failed_attempt_outcome(
                    task_dir=task,
                    attempt_id="A001",
                    startup={"state": "worker_started"},
                    supervisor=None,
                    exit_code=1,
                ),
            )
            self.write_json(
                task / "HANDOFF.json",
                {"_template": False, "requested_state": "review"},
            )
            self.assertEqual(
                "invalid_handoff",
                _failed_attempt_outcome(
                    task_dir=task,
                    attempt_id="A001",
                    startup={"state": "worker_started"},
                    supervisor=None,
                    exit_code=1,
                ),
            )


if __name__ == "__main__":
    unittest.main()
