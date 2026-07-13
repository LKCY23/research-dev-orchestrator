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


if __name__ == "__main__":
    unittest.main()
