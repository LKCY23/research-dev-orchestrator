import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from clean_restart import prepare_clean_restart


class CleanRestartTests(unittest.TestCase):
    def test_restart_preserves_old_workspace_and_creates_clean_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "file.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()

            old_worktree = root / ".agent-worktrees" / "T001"
            old_worktree.parent.mkdir()
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", "agent/T001", str(old_worktree), base],
                cwd=root,
                check=True,
            )
            (old_worktree / "file.txt").write_text("partial\n", encoding="utf-8")

            task = root / ".agent-collab" / "runs" / "run" / "tasks" / "T001"
            (task / "attempts" / "A001").mkdir(parents=True)
            (task / "attempts" / "A001" / "ATTEMPT.json").write_text(
                json.dumps({"attempt_id": "A001", "state": "terminated"}),
                encoding="utf-8",
            )
            (task / "STATUS.json").write_text(
                json.dumps(
                    {
                        "task_id": "T001",
                        "state": "blocked",
                        "current_attempt_id": "A001",
                        "branch": "agent/T001",
                        "worktree": ".agent-worktrees/T001",
                    }
                ),
                encoding="utf-8",
            )

            receipt = prepare_clean_restart(
                repo_root=root,
                task_dir=task,
                attempt_id="A002",
                task_base_commit=base,
                new_branch="agent/T001-restart-A002",
                new_worktree=".agent-worktrees/T001--A002",
            )
            repeated = prepare_clean_restart(
                repo_root=root,
                task_dir=task,
                attempt_id="A002",
                task_base_commit=base,
                new_branch="agent/T001-restart-A002",
                new_worktree=".agent-worktrees/T001--A002",
            )

            new_worktree = root / ".agent-worktrees" / "T001--A002"
            status = json.loads((task / "STATUS.json").read_text())
            self.assertEqual(receipt, repeated)
            self.assertEqual("partial\n", (old_worktree / "file.txt").read_text())
            self.assertEqual("base\n", (new_worktree / "file.txt").read_text())
            self.assertEqual("agent/T001-restart-A002", status["branch"])
            self.assertEqual(".agent-worktrees/T001--A002", status["worktree"])
            self.assertEqual(1, status["workspace_generation"])
            self.assertEqual("A001", receipt["parent_attempt_id"])


if __name__ == "__main__":
    unittest.main()
