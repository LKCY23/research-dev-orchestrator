import json
import tempfile
import unittest
from pathlib import Path

from protocol import write_json
from resume_context import ResumeContextError, build_resume_context


class ResumeContextTests(unittest.TestCase):
    def make_fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        task = root / "runs" / "run-1" / "tasks" / "T001"
        source = task / "attempts" / "A001-claude"
        current = task / "attempts" / "A002-opencode"
        (source / "runtime").mkdir(parents=True)
        (current / "runtime").mkdir(parents=True)
        write_json(current / "ATTEMPT.json", {
            "attempt_id": "A002-opencode",
            "state": "running",
            "backend_id": "opencode",
        })
        write_json(source / "ATTEMPT.json", {
            "attempt_id": "A001-claude",
            "state": "invalid_handoff",
            "strategy_id": "T001-S001",
            "strategy_sha256": "old-strategy",
        })
        (source / "runtime" / "WORKFLOWS.ndjson").write_text(
            json.dumps({
                "event": "workflow_completed",
                "workflow_id": "WF-old-implementation",
                "instance_id": "I001",
            }) + "\n",
            encoding="utf-8",
        )
        fingerprint = {"sha256": "same-worktree", "file_count": 1, "entries": []}
        write_json(source / "runtime" / "worktree-after.json", fingerprint)
        before = current / "runtime" / "worktree-before.json"
        write_json(before, fingerprint)
        strategy = task / "strategy" / "STRATEGY-v002.json"
        strategy.parent.mkdir()
        write_json(strategy, {
            "schema_version": 2,
            "backend_id": "opencode",
            "strategy_id": "T001-S002",
            "task_id": "T001",
            "revision": 2,
            "workflows": [
                {
                    "workflow_id": "WF-implementation",
                    "required": True,
                    "resume": {
                        "from_attempt": "A001-claude",
                        "from_workflow": "WF-old-implementation",
                        "mode": "reuse",
                    },
                },
                {"workflow_id": "WF-acceptance", "required": True},
            ],
            "completion_gate": {
                "required_workflows_complete": True,
                "acceptance_commands_pass": True,
                "optional_workflows_may_timeout": False,
            },
        })
        return task, current, strategy, before

    def test_backend_switch_carries_forward_explicit_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, current, strategy, before = self.make_fixture(Path(temporary))
            payload = build_resume_context(
                task_dir=task,
                attempt_dir=current,
                strategy_path=strategy,
                current_worktree_before=before,
            )
            self.assertEqual(["WF-implementation"], payload["carried_forward_workflows"])
            self.assertEqual(["WF-acceptance"], payload["remaining_workflows"])
            records = [
                json.loads(line)
                for line in (current / "runtime" / "WORKFLOWS.ndjson").read_text().splitlines()
            ]
            self.assertEqual("workflow_carried_forward", records[0]["event"])
            self.assertEqual("A001-claude", records[0]["source_attempt_id"])
            self.assertTrue(records[0]["checkpoint_sha256"])

    def test_changed_worktree_invalidates_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, current, strategy, before = self.make_fixture(Path(temporary))
            write_json(before, {"sha256": "changed", "file_count": 1, "entries": []})
            with self.assertRaisesRegex(ResumeContextError, "no longer matches"):
                build_resume_context(
                    task_dir=task,
                    attempt_dir=current,
                    strategy_path=strategy,
                    current_worktree_before=before,
                )

    def test_acceptance_cannot_be_entirely_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, current, strategy, before = self.make_fixture(Path(temporary))
            payload = json.loads(strategy.read_text())
            payload["workflows"] = [payload["workflows"][0]]
            write_json(strategy, payload)
            with self.assertRaisesRegex(ResumeContextError, "acceptance command records are attempt-local"):
                build_resume_context(
                    task_dir=task,
                    attempt_dir=current,
                    strategy_path=strategy,
                    current_worktree_before=before,
                )


if __name__ == "__main__":
    unittest.main()
