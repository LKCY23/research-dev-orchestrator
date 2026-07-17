import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import rdo


class TaskResumeTests(unittest.TestCase):
    def fixture(self, root: Path, *, state: str = "changes_requested") -> Path:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        task = (
            root
            / ".agent-collab"
            / "runs"
            / "run-1"
            / "tasks"
            / "T001-resume"
        )
        (task / "attempts" / "A001").mkdir(parents=True)
        (task / "STATUS.json").write_text(
            json.dumps(
                {
                    "task_id": "T001-resume",
                    "artifact_protocol_version": 2,
                    "profile": "delegated",
                    "state": state,
                    "current_attempt_id": "A001",
                    "assigned_worker": {
                        "backend_id": "codex",
                        "backend_session_id": "session-1",
                    },
                }
            ),
            encoding="utf-8",
        )
        (task / "attempts" / "A001" / "ATTEMPT.json").write_text(
            json.dumps(
                {
                    "task_id": "T001-resume",
                    "attempt_id": "A001",
                    "state": "completed",
                }
            ),
            encoding="utf-8",
        )
        return task

    def invoke(self, task: Path, *options: str) -> tuple[int, dict]:
        args = rdo.build_parser().parse_args(
            ["task", "resume", "--task-dir", str(task), *options]
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = args.func(args)
        return code, json.loads(output.getvalue())

    def test_resume_maps_options_and_reports_actual_attempt_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self.fixture(root)
            observed: dict[str, object] = {}

            def dispatch(command, dispatch_root):
                observed["command"] = command
                observed["root"] = dispatch_root
                status_path = task / "STATUS.json"
                status = json.loads(status_path.read_text(encoding="utf-8"))
                self.assertEqual("changes_requested", status["state"])
                attempt = task / "attempts" / "A002"
                attempt.mkdir()
                (attempt / "ATTEMPT.json").write_text(
                    json.dumps(
                        {
                            "task_id": "T001-resume",
                            "attempt_id": "A002",
                            "parent_attempt_id": "A001",
                            "requested_execution_mode": "resume",
                            "execution_mode": "start",
                            "resume_fallback_reason": "session_missing",
                            "backend_id": "codex",
                            "phase": "execution",
                            "state": "completed",
                            "outcome": "completed",
                            "exit_code": 0,
                            "runtime": {"backend": "tmux"},
                        }
                    ),
                    encoding="utf-8",
                )
                status.update(state="review", current_attempt_id="A002")
                status_path.write_text(json.dumps(status), encoding="utf-8")
                return 0

            with patch("rdo._run_task_dispatch", side_effect=dispatch):
                code, result = self.invoke(
                    task,
                    "--worker-backend",
                    "codex",
                    "--runtime-backend",
                    "tmux",
                    "--io-mode",
                    "human",
                    "--permission-mode",
                    "yolo",
                    "--agent-name",
                    "worker-a",
                    "--session-id",
                    "session-1",
                    "--worker-id",
                    "W-1",
                    "--execution-mode",
                    "resume",
                    "--phase",
                    "execution",
                )

            self.assertEqual(0, code)
            command = observed["command"]
            self.assertEqual("dispatch_agent.sh", Path(command[0]).name)
            self.assertEqual(
                [
                    "run-1",
                    "T001-resume",
                    "--worker",
                    "codex",
                    "--runtime",
                    "tmux",
                    "--io",
                    "human",
                    "--permission",
                    "yolo",
                    "--agent-name",
                    "worker-a",
                    "--session-id",
                    "session-1",
                    "--worker-id",
                    "W-1",
                    "--execution-mode",
                    "resume",
                    "--phase",
                    "execution",
                ],
                command[1:],
            )
            self.assertEqual(root.resolve(), Path(observed["root"]).resolve())
            self.assertEqual(0, result["dispatch_exit_code"])
            self.assertTrue(result["attempt_created"])
            self.assertEqual("A001", result["previous_attempt_id"])
            self.assertEqual("A002", result["attempt_id"])
            self.assertEqual("resume", result["requested_execution_mode"])
            self.assertEqual("start", result["execution_mode"])
            self.assertEqual("session_missing", result["resume_fallback_reason"])
            self.assertEqual("codex", result["backend_id"])
            self.assertEqual("tmux", result["runtime_backend"])
            self.assertEqual("execution", result["phase"])

    def test_resume_inherits_defaults_and_propagates_pre_attempt_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self.fixture(root, state="blocked")
            with patch(
                "rdo._run_task_dispatch",
                return_value=7,
            ) as dispatch:
                code, result = self.invoke(task)

            self.assertEqual(7, code)
            command = dispatch.call_args.args[0]
            self.assertEqual(["run-1", "T001-resume"], command[1:])
            self.assertFalse(result["attempt_created"])
            self.assertEqual("auto", result["requested_execution_mode"])
            self.assertEqual(7, result["dispatch_exit_code"])

    def test_resume_result_is_bound_to_the_new_child_not_mutable_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self.fixture(root, state="blocked")

            def dispatch(_command, _dispatch_root):
                for attempt_id, parent_id, backend in (
                    ("A002", "A001", "codex"),
                    ("A003", "A002", "opencode"),
                ):
                    attempt = task / "attempts" / attempt_id
                    attempt.mkdir()
                    (attempt / "ATTEMPT.json").write_text(
                        json.dumps(
                            {
                                "task_id": "T001-resume",
                                "attempt_id": attempt_id,
                                "parent_attempt_id": parent_id,
                                "requested_execution_mode": "resume",
                                "execution_mode": "resume",
                                "backend_id": backend,
                                "phase": "execution",
                                "state": "completed",
                                "outcome": "completed",
                                "exit_code": 0,
                                "runtime": {"backend": "plain"},
                            }
                        ),
                        encoding="utf-8",
                    )
                status_path = task / "STATUS.json"
                status = json.loads(status_path.read_text(encoding="utf-8"))
                status.update(state="blocked", current_attempt_id="A003")
                status_path.write_text(json.dumps(status), encoding="utf-8")
                return 0

            with patch("rdo._run_task_dispatch", side_effect=dispatch):
                code, result = self.invoke(task)

            self.assertEqual(0, code)
            self.assertEqual("A002", result["attempt_id"])
            self.assertEqual("codex", result["backend_id"])
            self.assertEqual("attempts/A002/ATTEMPT.json", result["selection_source"])

    def test_resume_rejects_invalid_state_lock_and_identity_before_dispatch(self) -> None:
        cases = (
            ("pending", False, False, "requires blocked or changes_requested"),
            ("blocked", True, False, "dispatch lock exists"),
            ("blocked", False, True, "STATUS.task_id does not match"),
        )
        for state, locked, wrong_identity, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task = self.fixture(root, state=state)
                if locked:
                    (task / ".dispatch-lock").mkdir()
                if wrong_identity:
                    status_path = task / "STATUS.json"
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    status["task_id"] = "T999-wrong"
                    status_path.write_text(json.dumps(status), encoding="utf-8")
                before = {
                    path.relative_to(task): path.read_bytes()
                    for path in task.rglob("*")
                    if path.is_file()
                }
                with patch("rdo._run_task_dispatch") as dispatch:
                    with self.assertRaisesRegex(SystemExit, error):
                        self.invoke(task)
                dispatch.assert_not_called()
                after = {
                    path.relative_to(task): path.read_bytes()
                    for path in task.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(before, after)

    def test_dispatch_helper_inherits_environment_and_streams_child_output(self) -> None:
        command = ["/dispatch_agent.sh", "run", "task"]
        root = Path("/repo")
        with patch(
            "rdo.subprocess.run",
            return_value=SimpleNamespace(returncode=3),
        ) as run:
            self.assertEqual(3, rdo._run_task_dispatch(command, root))
        self.assertEqual(command, run.call_args.args[0])
        self.assertEqual(root, run.call_args.kwargs["cwd"])
        self.assertFalse(run.call_args.kwargs["check"])
        self.assertIs(run.call_args.kwargs["stdout"], rdo.sys.stderr)
        self.assertNotIn("env", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
