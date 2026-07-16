import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from collect_status import validate_merged_task
from rdo import task_merge, task_review
from worktree_fingerprint import fingerprint


def git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


class TaskMergeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.root, check=True, capture_output=True)
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "RDO Test")
        (self.root / "file.txt").write_text("base\n")
        git(self.root, "add", "file.txt")
        git(self.root, "commit", "-m", "base")

        self.run = self.root / ".agent-collab" / "runs" / "run-1"
        self.task = self.run / "tasks" / "T101-example"
        (self.task / "reviews").mkdir(parents=True)
        (self.task / "attempts").mkdir()
        (self.task / "logs").mkdir()
        (self.run / "RUN.json").write_text(json.dumps({
            "run_id": "run-1", "target_branch": "main",
        }))
        (self.run / "EVENTS.ndjson").write_text("")

        self.task_worktree = self.root / ".agent-worktrees" / "T101-example"
        git(self.root, "branch", "agent/T101-example")
        subprocess.run(
            ["git", "worktree", "add", str(self.task_worktree), "agent/T101-example"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        (self.task_worktree / "file.txt").write_text("task\n")
        git(self.task_worktree, "add", "file.txt")
        git(self.task_worktree, "commit", "-m", "task")

        status = {
            "task_id": "T101-example",
            "profile": "delegated",
            "state": "review",
            "previous_state": "running",
            "owner": "worker",
            "branch": "agent/T101-example",
            "worktree": ".agent-worktrees/T101-example",
            "updated_at": "2026-07-14T00:00:00Z",
            "needs_coordinator": False,
            "summary": "",
            "blocking_reason": "",
            "blocker_type": "",
            "current_attempt_id": "A001",
            "assigned_worker": {"worker_id": "W001"},
            "evidence": {"commands_run": [], "logs": [], "passed": True},
            "state_history": [
                {"from": "pending", "to": "running", "actor": "dispatch", "at": "2026-07-14T00:00:00Z"},
                {"from": "running", "to": "review", "actor": "dispatch", "at": "2026-07-14T00:01:00Z"},
            ],
        }
        (self.task / "STATUS.json").write_text(json.dumps(status))
        (self.task / "EVIDENCE.md").write_text("# Evidence\n\nPassed.\n")
        (self.task / "HANDOFF.json").write_text(json.dumps({"requested_state": "review"}))
        self.findings = self.task / "reviews" / "findings.md"
        self.findings.write_text("# Findings\n\nNo findings.\n")
        self.approve()

    def tearDown(self):
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(self.task_worktree)],
            cwd=self.root,
            check=False,
            capture_output=True,
        )
        self.temporary.cleanup()

    def approve(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return task_review(argparse.Namespace(
                task_dir=str(self.task),
                decision="approved",
                reviewer="codex",
                findings_file=str(self.findings),
                note=[],
            ))

    def merge(self, **overrides):
        values = {
            "task_dir": str(self.task),
            "target_worktree": str(self.root),
            "expected_commit": "",
            "verify_command": [],
            "verification_timeout": 5,
            "coordinator": "codex",
        }
        values.update(overrides)
        with contextlib.redirect_stdout(io.StringIO()):
            return task_merge(argparse.Namespace(**values))

    def merged_events(self):
        return [
            json.loads(line)
            for line in (self.run / "EVENTS.ndjson").read_text().splitlines()
            if json.loads(line).get("event") == "task_merged"
        ]

    def test_approval_binds_exact_git_commit(self):
        decision = json.loads((self.task / "reviews" / "DECISION-v001.json").read_text())
        self.assertEqual(decision["approved_commit"], git(self.task_worktree, "rev-parse", "HEAD"))
        self.assertEqual(decision["target_branch"], "main")
        self.assertTrue(decision["evidence_sha256"])

    def test_fast_forward_merge_updates_status_and_event(self):
        self.assertEqual(self.merge(), 0)
        source = git(self.task_worktree, "rev-parse", "HEAD")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), source)
        self.assertEqual(json.loads((self.task / "STATUS.json").read_text())["state"], "merged")
        self.assertEqual(self.merged_events()[0]["commit"], source)
        status = json.loads((self.task / "STATUS.json").read_text())
        events = [json.loads(line) for line in (self.run / "EVENTS.ndjson").read_text().splitlines()]
        violations, warnings = validate_merged_task(
            self.root,
            self.task,
            status,
            {"target_branch": "main"},
            events,
        )
        self.assertEqual(violations, [])
        self.assertEqual(warnings, [])

    def test_repeated_merge_is_idempotent(self):
        self.assertEqual(self.merge(), 0)
        self.assertEqual(self.merge(), 0)
        self.assertEqual(len(self.merged_events()), 1)

    def test_completed_merge_remains_idempotent_after_task_worktree_cleanup(self):
        self.assertEqual(self.merge(), 0)
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(self.task_worktree)],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        self.assertEqual(self.merge(), 0)
        self.assertEqual(len(self.merged_events()), 1)

    def test_recovers_when_git_was_already_merged(self):
        source = git(self.task_worktree, "rev-parse", "HEAD")
        git(self.root, "merge", "--ff-only", source)
        self.assertEqual(self.merge(), 0)
        self.assertEqual(json.loads((self.task / "STATUS.json").read_text())["state"], "merged")
        self.assertEqual(len(self.merged_events()), 1)

    def test_approval_accepts_task_commit_already_contained_in_target(self):
        source = git(self.task_worktree, "rev-parse", "HEAD")
        git(self.root, "merge", "--ff-only", source)
        (self.root / "target-only.txt").write_text("later target work\n")
        git(self.root, "add", "target-only.txt")
        git(self.root, "commit", "-m", "later target work")
        status_path = self.task / "STATUS.json"
        status = json.loads(status_path.read_text())
        status.update(state="review", previous_state="running", owner="worker")
        status_path.write_text(json.dumps(status))

        self.assertEqual(self.approve(), 0)

        decision = json.loads(
            (self.task / "reviews" / "DECISION-v002.json").read_text()
        )
        self.assertEqual(source, decision["approved_commit"])
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), decision["target_commit_at_review"])

    def test_rejects_task_commit_created_after_approval(self):
        (self.task_worktree / "file.txt").write_text("changed after review\n")
        git(self.task_worktree, "add", "file.txt")
        git(self.task_worktree, "commit", "-m", "unreviewed")
        with self.assertRaisesRegex(SystemExit, "changed after coordinator approval"):
            self.merge()

    def test_rejects_dirty_target_worktree(self):
        (self.root / "file.txt").write_text("dirty target\n")
        with self.assertRaisesRegex(SystemExit, "target worktree has non-RDO changes"):
            self.merge()

    def test_rejects_non_fast_forward_target_advance(self):
        (self.root / "target-only.txt").write_text("advance\n")
        git(self.root, "add", "target-only.txt")
        git(self.root, "commit", "-m", "target advance")
        with self.assertRaisesRegex(SystemExit, "cannot be fast-forward merged"):
            self.merge()

    def test_failed_post_merge_verification_is_recorded_as_merged(self):
        result = self.merge(verify_command=["python3 -c 'raise SystemExit(3)'"])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads((self.task / "STATUS.json").read_text())["state"], "merged")
        event = self.merged_events()[0]
        self.assertFalse(event["verification"]["passed"])
        self.assertEqual(event["verification"]["results"][0]["exit_code"], 3)

    def test_timed_out_post_merge_verification_kills_descendants(self):
        helper = self.task / "logs" / "spawn-descendant.py"
        helper.write_text(
            "import pathlib, subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', "
            "\"import pathlib,time; time.sleep(0.8); pathlib.Path('late.txt').write_text('late')\"])\n"
            "time.sleep(10)\n"
        )
        result = self.merge(
            verify_command=[f"{sys.executable} {helper}"],
            verification_timeout=0.2,
        )
        self.assertEqual(result, 1)
        time.sleep(1.0)
        self.assertFalse((self.root / "late.txt").exists())
        verification = self.merged_events()[0]["verification"]["results"][0]
        self.assertTrue(verification["timed_out"])
        self.assertEqual(verification["surviving_pids"], [])

    def test_verified_direct_task_uses_attempt_fingerprint(self):
        status_path = self.task / "STATUS.json"
        status = json.loads(status_path.read_text())
        status.update(profile="direct", state="verified", previous_state="running")
        status_path.write_text(json.dumps(status))
        attempt = self.task / "attempts" / "A001"
        (attempt / "runtime").mkdir(parents=True)
        (attempt / "ATTEMPT.json").write_text(json.dumps({
            "state": "completed", "handoff_valid": True, "handoff_state": "verified",
            "verified_commit": git(self.task_worktree, "rev-parse", "HEAD"),
        }))
        (attempt / "runtime" / "worktree-after.json").write_text(
            json.dumps(fingerprint(self.task_worktree))
        )
        self.assertEqual(self.merge(), 0)
        self.assertEqual(json.loads(status_path.read_text())["state"], "merged")

    def test_verified_direct_task_rejects_mode_only_commit_after_handoff(self):
        status_path = self.task / "STATUS.json"
        status = json.loads(status_path.read_text())
        status.update(profile="direct", state="verified", previous_state="running")
        status_path.write_text(json.dumps(status))
        attempt = self.task / "attempts" / "A001"
        (attempt / "runtime").mkdir(parents=True)
        verified_commit = git(self.task_worktree, "rev-parse", "HEAD")
        (attempt / "ATTEMPT.json").write_text(json.dumps({
            "state": "completed",
            "handoff_valid": True,
            "handoff_state": "verified",
            "verified_commit": verified_commit,
        }))
        (attempt / "runtime" / "worktree-after.json").write_text(
            json.dumps(fingerprint(self.task_worktree))
        )

        file_path = self.task_worktree / "file.txt"
        os.chmod(file_path, file_path.stat().st_mode | 0o111)
        git(self.task_worktree, "add", "file.txt")
        git(self.task_worktree, "commit", "-m", "mode-only change after handoff")
        self.assertEqual(
            json.loads((attempt / "runtime" / "worktree-after.json").read_text())["sha256"],
            fingerprint(self.task_worktree)["sha256"],
        )
        self.assertNotEqual(
            json.loads((attempt / "runtime" / "worktree-after.json").read_text())[
                "semantic_sha256"
            ],
            fingerprint(self.task_worktree)["semantic_sha256"],
        )
        with self.assertRaisesRegex(SystemExit, "HEAD changed after verified handoff"):
            self.merge()


if __name__ == "__main__":
    unittest.main()
