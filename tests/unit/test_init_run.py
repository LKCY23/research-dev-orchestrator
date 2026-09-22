from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INIT_RUN = ROOT / "scripts" / "init_run.py"
LOCAL_EXCLUDE_RULES = ("/.agent-collab/", "/.agent-worktrees/")


class InitRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_directory.name)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "init-run@example.com"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Init Run Test"],
            cwd=self.repo,
            check=True,
        )
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "fixture"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_init(self, run_id: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(INIT_RUN),
                "--run-id",
                run_id,
                "--project-slug",
                "fixture",
                "--objective",
                "exercise run initialization",
                "--target-branch",
                "main",
            ],
            cwd=self.repo,
            check=check,
            capture_output=True,
            text=True,
        )

    def exclude_path(self) -> Path:
        raw = subprocess.check_output(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=self.repo,
            text=True,
        ).strip()
        path = Path(raw)
        return path if path.is_absolute() else self.repo / path

    def test_initialization_adds_idempotent_local_excludes_and_keeps_status_clean(self) -> None:
        self.run_init("run-one")
        self.run_init("run-two")

        exclude_text = self.exclude_path().read_text(encoding="utf-8")
        for rule in LOCAL_EXCLUDE_RULES:
            self.assertEqual(exclude_text.count(rule), 1)
        self.assertTrue((self.repo / ".agent-collab" / "runs" / "run-one").is_dir())
        self.assertTrue((self.repo / ".agent-collab" / "runs" / "run-two").is_dir())
        status = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=self.repo,
            text=True,
        )
        self.assertEqual(status, "")

    def test_initialization_refuses_already_tracked_local_state_before_scaffolding(self) -> None:
        tracked = self.repo / ".agent-collab" / "existing.txt"
        tracked.parent.mkdir()
        tracked.write_text("tracked by mistake\n", encoding="utf-8")
        subprocess.run(["git", "add", ".agent-collab"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "track local state"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

        result = self.run_init("blocked-run", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RDO local state is already tracked by Git", result.stderr)
        self.assertIn("git rm -r --cached", result.stderr)
        self.assertFalse(
            (self.repo / ".agent-collab" / "runs" / "blocked-run").exists()
        )
        exclude_text = self.exclude_path().read_text(encoding="utf-8")
        for rule in LOCAL_EXCLUDE_RULES:
            self.assertNotIn(rule, exclude_text)
