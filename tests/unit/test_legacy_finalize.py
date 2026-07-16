import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rdo import _strategy_profile_is_full, handoff


class LegacyFinalizeTests(unittest.TestCase):
    def test_missing_profile_remains_full_only_for_legacy_strategy_commands(self) -> None:
        self.assertTrue(_strategy_profile_is_full(1, None))
        self.assertTrue(_strategy_profile_is_full(1, "full"))
        self.assertFalse(_strategy_profile_is_full(1, "delegated"))
        self.assertFalse(_strategy_profile_is_full(2, None))
        self.assertTrue(_strategy_profile_is_full(2, "full"))

    def test_delegated_review_freezes_source_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "run" / "tasks" / "T001-legacy"
            attempt = task / "attempts" / "A001-worker"
            (attempt / "runtime").mkdir(parents=True)
            (task / "STATUS.json").write_text(
                json.dumps(
                    {
                        "task_id": "T001-legacy",
                        "profile": "delegated",
                        "state": "running",
                        "branch": "agent/T001-legacy",
                        "current_attempt_id": "A001-worker",
                    }
                ),
                encoding="utf-8",
            )
            (attempt / "ATTEMPT.json").write_text(
                json.dumps(
                    {
                        "phase": "execution",
                        "strategy_sha256": None,
                        "runtime": {"cwd": str(root / "worktree")},
                    }
                ),
                encoding="utf-8",
            )
            commit = "a" * 40
            args = argparse.Namespace(
                task_dir=str(task),
                attempt_dir="",
                state="review",
                summary="Delegated implementation reviewed by the worker.",
                summary_file="",
                command=[],
                file=[],
                limitation=[],
                self_review_passed=False,
                self_review_finding=[],
                self_review_fix=[],
                blocker_type="",
                blocking_reason="",
                auto_derive=True,
            )

            with (
                patch("rdo.require_clean_task_worktree"),
                patch("rdo.git_output", return_value=commit),
                patch("rdo.derive_task_changed_files", return_value=[]),
            ):
                self.assertEqual(0, handoff(args))

            handoff_payload = json.loads(
                (task / "HANDOFF.json").read_text(encoding="utf-8")
            )
            completion = json.loads(
                (attempt / "COMPLETION.json").read_text(encoding="utf-8")
            )
            self.assertEqual(commit, handoff_payload["source_commit"])
            self.assertEqual(commit, completion["source_commit"])


if __name__ == "__main__":
    unittest.main()
