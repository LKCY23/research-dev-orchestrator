import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dispatch_assets import render_worker_prompt
from strategy import DEFAULT_EXECUTION_POLICY


class DispatchAssetsTests(unittest.TestCase):
    def test_planning_prompt_embeds_policy_bounded_strategy_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            (task / "strategy").mkdir()
            (task / "STATUS.json").write_text(
                json.dumps({"task_id": "T101-example"}), encoding="utf-8"
            )
            (task / "EXECUTION_POLICY.json").write_text(
                json.dumps(DEFAULT_EXECUTION_POLICY), encoding="utf-8"
            )
            for name in ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md"):
                (task / name).write_text(name, encoding="utf-8")

            prompt = render_worker_prompt(
                worktree_path="/tmp/worktree",
                task_dir=task,
                status_path=task / "STATUS.json",
                attempt_dir=task / "attempts" / "A001",
                worker_backend="claude-code",
                phase="planning",
            )

            self.assertIn('"schema_version": 2', prompt)
            self.assertIn('"task_id": "T101-example"', prompt)
            self.assertIn('"backend_id": "claude-code"', prompt)
            self.assertIn('"allowed_paths": [\n          "."', prompt)
            self.assertIn("do not inspect RDO source code or tests", prompt)
            self.assertIn("strategy submit --task-dir", prompt)

    def test_revision_prompt_uses_strategy_revise(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            (task / "strategy").mkdir()
            (task / "STATUS.json").write_text(
                json.dumps({"task_id": "T101-example"}), encoding="utf-8"
            )
            (task / "EXECUTION_POLICY.json").write_text(
                json.dumps(DEFAULT_EXECUTION_POLICY), encoding="utf-8"
            )
            (task / "strategy" / "STRATEGY-v001.json").write_text(
                json.dumps({"strategy_id": "T101-example-S001"}), encoding="utf-8"
            )
            for name in ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md"):
                (task / name).write_text(name, encoding="utf-8")

            prompt = render_worker_prompt(
                worktree_path="/tmp/worktree",
                task_dir=task,
                status_path=task / "STATUS.json",
                attempt_dir=task / "attempts" / "A002",
                worker_backend="codex",
                phase="planning",
            )

            self.assertIn("strategy revise --task-dir", prompt)
            self.assertIn('"revision": 2', prompt)
            self.assertIn('"supersedes": "T101-example-S001"', prompt)

    def test_execution_prompt_does_not_embed_strategy_skeleton(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            for name in ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md"):
                (task / name).write_text(name, encoding="utf-8")

            prompt = render_worker_prompt(
                worktree_path="/tmp/worktree",
                task_dir=task,
                status_path=task / "STATUS.json",
                attempt_dir=task / "attempts" / "A001",
                worker_backend="claude-code",
                phase="execution",
                strategy_path="/tmp/STRATEGY-v001.json",
            )

            self.assertNotIn("Minimal Valid Strategy Skeleton", prompt)

    def test_prompt_embeds_digest_bound_changes_requested_feedback(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            (task / "strategy").mkdir()
            reviews = task / "reviews"
            reviews.mkdir()
            findings = "# Findings\n\nCorrect the documented API status names.\n"
            findings_path = reviews / "coordinator-findings.md"
            findings_path.write_text(findings, encoding="utf-8")
            decision_path = reviews / "DECISION-v001.json"
            decision_path.write_text(
                json.dumps(
                    {
                        "revision": 1,
                        "decision": "changes_requested",
                        "reviewer": "codex",
                        "findings_path": "reviews/coordinator-findings.md",
                        "findings_sha256": hashlib.sha256(
                            findings.encode("utf-8")
                        ).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            (reviews / "CURRENT_TASK_REVIEW.json").write_text(
                json.dumps(
                    {
                        "revision": 1,
                        "decision_path": "reviews/DECISION-v001.json",
                    }
                ),
                encoding="utf-8",
            )
            (task / "STATUS.json").write_text(
                json.dumps({"task_id": "T101-example", "state": "changes_requested"}),
                encoding="utf-8",
            )
            (task / "EXECUTION_POLICY.json").write_text(
                json.dumps(DEFAULT_EXECUTION_POLICY), encoding="utf-8"
            )
            for name in ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md"):
                (task / name).write_text(name, encoding="utf-8")

            prompt = render_worker_prompt(
                worktree_path="/tmp/worktree",
                task_dir=task,
                status_path=task / "STATUS.json",
                attempt_dir=task / "attempts" / "A002",
                worker_backend="opencode",
                phase="planning",
            )

            self.assertIn("## Coordinator Feedback", prompt)
            self.assertIn("Correct the documented API status names.", prompt)
            self.assertIn("Reviewer: codex", prompt)

    def test_planning_prompt_embeds_digest_bound_strategy_review_feedback(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            strategy_dir = task / "strategy"
            strategy_dir.mkdir()
            strategy = {
                "strategy_id": "T101-example-S004",
                "task_id": "T101-example",
                "revision": 4,
            }
            strategy_path = strategy_dir / "STRATEGY-v004.json"
            strategy_path.write_text(json.dumps(strategy), encoding="utf-8")
            canonical = json.dumps(
                strategy,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            (strategy_dir / "REVIEW-v004.json").write_text(
                json.dumps(
                    {
                        "strategy_id": "T101-example-S004",
                        "strategy_sha256": hashlib.sha256(canonical).hexdigest(),
                        "decision": "changes_requested",
                        "reviewer": "codex",
                        "notes": ["Resume from the terminal execution attempt."],
                    }
                ),
                encoding="utf-8",
            )
            (task / "STATUS.json").write_text(
                json.dumps({"task_id": "T101-example", "state": "changes_requested"}),
                encoding="utf-8",
            )
            (task / "EXECUTION_POLICY.json").write_text(
                json.dumps(DEFAULT_EXECUTION_POLICY), encoding="utf-8"
            )
            for name in ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md"):
                (task / name).write_text(name, encoding="utf-8")

            prompt = render_worker_prompt(
                worktree_path="/tmp/worktree",
                task_dir=task,
                status_path=task / "STATUS.json",
                attempt_dir=task / "attempts" / "A008",
                worker_backend="opencode",
                phase="planning",
            )

            self.assertIn("## Strategy Revision Feedback", prompt)
            self.assertIn("Rejected revision: 4", prompt)
            self.assertIn("Resume from the terminal execution attempt.", prompt)


if __name__ == "__main__":
    unittest.main()
