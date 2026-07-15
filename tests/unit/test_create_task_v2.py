from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INIT_RUN = ROOT / "scripts" / "init_run.py"
CREATE_TASK = ROOT / "scripts" / "create_task.py"


class CreateTaskV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_directory.name)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "create-task-v2@example.com"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Create Task V2 Test"],
            cwd=self.repo,
            check=True,
        )
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            [
                sys.executable,
                str(INIT_RUN),
                "--run-id",
                "v2-run",
                "--project-slug",
                "fixture",
                "--objective",
                "exercise task creation",
                "--target-branch",
                "main",
            ],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create_task(self, task_id: str, *extra: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(CREATE_TASK),
                "--run-id",
                "v2-run",
                "--task-id",
                task_id,
                "--goal",
                "Implement the v2 artifact boundary",
                "--allowed-paths",
                "src",
                "tests",
                *extra,
            ],
            cwd=self.repo,
            check=check,
            capture_output=True,
            text=True,
        )

    def task_dir(self, task_id: str) -> Path:
        return self.repo / ".agent-collab" / "runs" / "v2-run" / "tasks" / task_id

    def test_creates_v2_task_with_separated_canonical_responsibilities(self) -> None:
        self.create_task(
            "T003-work",
            "--read-paths",
            "src",
            "tests",
            "docs",
            "--forbidden-paths",
            "private",
            "--context-sources",
            "docs/DESIGN.md",
            "docs/API.md",
            "--dependencies",
            "T001-base",
            "T002-data",
            "--profile",
            "direct",
            "--branch",
            "agent/custom-task-branch",
            "--worktree",
            "/tmp/custom-task-worktree",
        )
        task_dir = self.task_dir("T003-work")

        task_text = (task_dir / "TASK.md").read_text(encoding="utf-8")
        self.assertEqual(
            re.findall(r"^## (.+)$", task_text, flags=re.MULTILINE),
            ["Objective", "Deliverables", "Invariants", "Non-goals", "Dependencies"],
        )
        self.assertIn("Implement the v2 artifact boundary", task_text)
        self.assertNotIn("direct", task_text)
        self.assertNotIn("agent/custom-task-branch", task_text)
        self.assertNotIn("/tmp/custom-task-worktree", task_text)
        self.assertNotIn("docs/DESIGN.md", task_text)
        self.assertEqual(task_text.count("```json rdo-task-dependencies"), 1)

        dependency_match = re.search(
            r"```json rdo-task-dependencies\n(?P<payload>.*?)\n```",
            task_text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(dependency_match)
        dependency_contract = json.loads(dependency_match.group("payload"))
        self.assertEqual(
            dependency_contract,
            {
                "schema_version": 2,
                "dependencies": [
                    {"task_id": "T001-base", "required_state": "merged"},
                    {"task_id": "T002-data", "required_state": "merged"},
                ],
            },
        )

        # The coordinator still must replace the semantic placeholders that
        # cannot be inferred from create_task's CLI arguments.
        self.assertEqual(task_text.count("RDO_TEMPLATE_INCOMPLETE"), 3)
        self.assertIn("RDO_TEMPLATE_INCOMPLETE", (task_dir / "CONTEXT.md").read_text(encoding="utf-8"))
        self.assertIn("RDO_TEMPLATE_INCOMPLETE", (task_dir / "ACCEPTANCE.md").read_text(encoding="utf-8"))

        status = json.loads((task_dir / "STATUS.json").read_text(encoding="utf-8"))
        self.assertEqual(status["artifact_protocol_version"], 2)
        self.assertEqual(status["profile"], "direct")
        self.assertEqual(status["branch"], "agent/custom-task-branch")
        self.assertEqual(status["worktree"], "/tmp/custom-task-worktree")

        policy = json.loads((task_dir / "EXECUTION_POLICY.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["schema_version"], 2)
        self.assertFalse(policy["strategy_required"])
        self.assertEqual(policy["allowed_paths"], ["src", "tests"])
        self.assertEqual(policy["read_paths"], ["src", "tests", "docs"])
        self.assertEqual(policy["forbidden_paths"], ["private"])
        self.assertEqual(policy["context_sources"], ["docs/DESIGN.md", "docs/API.md"])

        for obsolete_root_artifact in ("HANDOFF.md", "HANDOFF.json", "EVIDENCE.md"):
            self.assertFalse((task_dir / obsolete_root_artifact).exists(), obsolete_root_artifact)

    def test_empty_dependencies_are_explicit_and_complete(self) -> None:
        self.create_task("T004-independent")
        task_text = (self.task_dir("T004-independent") / "TASK.md").read_text(encoding="utf-8")
        dependency_match = re.search(
            r"```json rdo-task-dependencies\n(?P<payload>.*?)\n```",
            task_text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(dependency_match)
        self.assertEqual(
            json.loads(dependency_match.group("payload")),
            {"schema_version": 2, "dependencies": []},
        )
        dependencies_section = task_text.split("## Dependencies", maxsplit=1)[1]
        self.assertNotIn("RDO_TEMPLATE_INCOMPLETE", dependencies_section)

    def test_dependencies_accept_only_unique_other_task_ids(self) -> None:
        invalid_cases = {
            "T010-format": ["T001-base:merged"],
            "T011-self": ["T011-self"],
            "T012-duplicate": ["T001-base", "T001-base"],
        }
        for task_id, dependencies in invalid_cases.items():
            with self.subTest(task_id=task_id):
                result = self.create_task(
                    task_id,
                    "--dependencies",
                    *dependencies,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.task_dir(task_id).exists())


if __name__ == "__main__":
    unittest.main()
