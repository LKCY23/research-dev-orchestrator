import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import rdo
from strategy import DEFAULT_EXECUTION_POLICY


class PreviewPromptTests(unittest.TestCase):
    def fixture(self, root: Path) -> Path:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        collab = root / ".agent-collab"
        collab.mkdir()
        (collab / "rdo.toml").write_text(
            "[worker]\nbackend = \"codex\"\nagent_name = \"codex-worker\"\n",
            encoding="utf-8",
        )
        task = root / ".agent-collab" / "runs" / "run" / "tasks" / "T001-preview"
        attempt = task / "attempts" / "A001"
        attempt.mkdir(parents=True)
        (attempt / "ATTEMPT.json").write_text(
            json.dumps(
                {
                    "attempt_id": "A001",
                    "state": "invalid_handoff",
                    "outcome": "execution_failed",
                    "handoff_state": None,
                }
            ),
            encoding="utf-8",
        )
        (task / "STATUS.json").write_text(
            json.dumps(
                {
                    "task_id": "T001-preview",
                    "state": "blocked",
                    "profile": "direct",
                    "artifact_protocol_version": 2,
                    "worktree": ".agent-worktrees/T001-preview",
                    "current_attempt_id": "A001",
                    "assigned_worker": {
                        "backend_id": "codex",
                        "agent_name": "codex-worker",
                        "backend_session_id": "session-1",
                    },
                }
            ),
            encoding="utf-8",
        )
        (task / "TASK.md").write_text("Preview task.\n", encoding="utf-8")
        (task / "CONTEXT.md").write_text("Preview context.\n", encoding="utf-8")
        (task / "ACCEPTANCE.md").write_text(
            "## Behavioral Checks\n\n- Preserve the source commit.\n",
            encoding="utf-8",
        )
        (task / "EXECUTION_POLICY.json").write_text(
            json.dumps(DEFAULT_EXECUTION_POLICY), encoding="utf-8"
        )
        return task

    @staticmethod
    def snapshot(task: Path) -> dict[Path, bytes]:
        return {
            path.relative_to(task): path.read_bytes()
            for path in task.rglob("*")
            if path.is_file()
        }

    def render(self, task: Path, *extra: str) -> dict:
        arguments = rdo.build_parser().parse_args(
            ["task", "preview-prompt", "--task-dir", str(task), *extra]
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, arguments.func(arguments))
        return json.loads(output.getvalue())

    def test_auto_resume_preview_is_compact_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = self.fixture(Path(temporary))
            before = self.snapshot(task)

            payload = self.render(task)

            self.assertEqual(before, self.snapshot(task))
            self.assertEqual("preflight_candidate", payload["selection_stage"])
            self.assertFalse(payload["byte_exact"])
            self.assertEqual("resume", payload["execution_mode_candidate"])
            self.assertEqual("compact_resume", payload["prompt_mode"])
            self.assertEqual("not_used_compact_resume", payload["dependency_projection"])
            self.assertIn("# Worker Resume Prompt", payload["prompt"])
            self.assertIn("Worktree is not materialized yet", payload["prompt"])
            self.assertNotIn("\n## TASK.md\n", payload["prompt"])
            encoded = payload["prompt"].encode("utf-8")
            self.assertEqual(len(encoded), payload["prompt_bytes"])
            self.assertEqual(
                hashlib.sha256(encoded).hexdigest(), payload["prompt_sha256"]
            )

    def test_explicit_start_preview_contains_full_frozen_packet(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = self.fixture(Path(temporary))

            payload = self.render(task, "--execution-mode", "start")

            self.assertEqual("start", payload["execution_mode_candidate"])
            self.assertEqual("full", payload["prompt_mode"])
            self.assertIn("\n## TASK.md\n", payload["prompt"])


if __name__ == "__main__":
    unittest.main()
