import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from artifact_bundle import publish_bundle
from completion import write_completion
from supervisor import pid_alive
from task_contract import TASK_INPUT_FILENAMES, build_task_inputs_payload


ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = ROOT / "scripts" / "supervise_attempt.py"


class InteractiveAttemptSupervisorTests(unittest.TestCase):
    def write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    def make_v2_publication(self, root: Path) -> tuple[Path, Path]:
        task = root / "tasks" / "T002"
        attempt = task / "attempts" / "A001"
        (attempt / "runtime").mkdir(parents=True)
        self.write_json(
            task / "STATUS.json",
            {
                "artifact_protocol_version": 2,
                "task_id": "T002",
                "state": "running",
                "current_attempt_id": "A001",
            },
        )
        self.write_json(
            attempt / "TASK_INPUTS.json",
            build_task_inputs_payload(
                task_id="T002",
                attempt_id="A001",
                source_bytes={name: f"{name}\n".encode() for name in TASK_INPUT_FILENAMES},
                task_base_commit="a" * 40,
                resolved_dependencies=[],
            ),
        )
        digest = hashlib.sha256((attempt / "TASK_INPUTS.json").read_bytes()).hexdigest()
        self.write_json(
            attempt / "ATTEMPT.json",
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_id": "T002",
                "attempt_id": "A001",
                "state": "running",
                "task_inputs_ref": "TASK_INPUTS.json",
                "task_inputs_sha256": digest,
            },
        )
        publish_bundle(
            attempt,
            requested_state="blocked",
            summary="Waiting for an external prerequisite.",
            conditional_blocker={
                "blocker_type": "external_dependency",
                "reason": "The required service is unavailable.",
            },
            direct_self_review={
                "performed": False,
                "passed": False,
                "summary": "",
                "findings": [],
            },
            expected_task_id="T002",
            expected_attempt_id="A001",
        )
        return task, attempt

    def command(
        self,
        root: Path,
        task: Path,
        attempt: Path,
        shell_command: str,
        *,
        protocol_version: int = 2,
    ) -> list[str]:
        return [
            sys.executable,
            str(SUPERVISOR),
            "--timeout-seconds",
            "2",
            "--grace-seconds",
            "0.05",
            "--handoff-grace-seconds",
            "0.05",
            "--result",
            str(attempt / "supervisor-result.json"),
            "--cwd",
            str(root),
            "--shell-command",
            shell_command,
            "--artifact-protocol-version",
            str(protocol_version),
            "--task-dir",
            str(task),
            "--attempt-id",
            "A001",
        ]

    def test_valid_v2_ready_stops_interactive_worker_and_cleans_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, attempt = self.make_v2_publication(root)
            result = subprocess.run(
                self.command(root, task, attempt, "sleep 30 & wait"),
                capture_output=True,
                text=True,
                timeout=8,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads((attempt / "supervisor-result.json").read_text())
            self.assertTrue(payload["completion_requested"])
            self.assertTrue(payload["handoff_ready"]["valid"])
            self.assertFalse(payload["publication_invalidated"])
            self.assertEqual([], payload["surviving_pids"])
            self.assertFalse(any(pid_alive(pid) for pid in payload["observed_pids"]))

    def test_stale_v2_ready_does_not_stop_interactive_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, attempt = self.make_v2_publication(root)
            status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
            status["current_attempt_id"] = "A002"
            self.write_json(task / "STATUS.json", status)
            sentinel = root / "worker-finished"
            shell_command = f"sleep 0.35; touch {shlex.quote(str(sentinel))}"
            result = subprocess.run(
                self.command(root, task, attempt, shell_command),
                capture_output=True,
                text=True,
                timeout=8,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(sentinel.exists())
            payload = json.loads((attempt / "supervisor-result.json").read_text())
            self.assertFalse(payload["completion_requested"])
            self.assertFalse(payload["handoff_ready"]["valid"])
            self.assertTrue(
                any("current task attempt" in reason for reason in payload["handoff_ready"]["reasons"])
            )

    def test_interactive_supervisor_still_accepts_legacy_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "tasks" / "T001"
            attempt = task / "attempts" / "A001"
            attempt.mkdir(parents=True)
            self.write_json(
                task / "STATUS.json",
                {
                    "artifact_protocol_version": 1,
                    "task_id": "T001",
                    "state": "planning",
                    "current_attempt_id": "A001",
                },
            )
            self.write_json(
                attempt / "ATTEMPT.json",
                {"attempt_id": "A001", "state": "running", "phase": "planning"},
            )
            self.write_json(
                task / "HANDOFF.json",
                {"requested_state": "strategy_review", "strategy_sha256": "abc"},
            )
            write_completion(
                task,
                attempt_id="A001",
                phase="planning",
                requested_state="strategy_review",
                strategy_sha256="abc",
            )
            result = subprocess.run(
                self.command(
                    root,
                    task,
                    attempt,
                    "sleep 30 & wait",
                    protocol_version=1,
                ),
                capture_output=True,
                text=True,
                timeout=8,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads((attempt / "supervisor-result.json").read_text())
            self.assertTrue(payload["completion_requested"])
            self.assertTrue(payload["completion"]["valid"])


if __name__ == "__main__":
    unittest.main()
