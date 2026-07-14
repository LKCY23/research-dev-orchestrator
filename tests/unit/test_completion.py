import json
import tempfile
import unittest
from pathlib import Path

from completion import validate_completion, write_completion


class CompletionTests(unittest.TestCase):
    def make_task(self, root: Path) -> tuple[Path, Path]:
        task = root / "tasks" / "T001"
        attempt = task / "attempts" / "A001"
        attempt.mkdir(parents=True)
        (task / "STATUS.json").write_text(
            json.dumps({"task_id": "T001", "state": "planning", "current_attempt_id": "A001"}),
            encoding="utf-8",
        )
        (attempt / "ATTEMPT.json").write_text(
            json.dumps({"attempt_id": "A001", "state": "running", "phase": "planning"}),
            encoding="utf-8",
        )
        (task / "HANDOFF.json").write_text(
            json.dumps({"requested_state": "strategy_review", "strategy_sha256": "abc"}),
            encoding="utf-8",
        )
        return task, attempt

    def test_valid_completion_binds_attempt_and_handoff_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, attempt = self.make_task(Path(temporary))
            path = write_completion(
                task,
                attempt_id="A001",
                phase="planning",
                requested_state="strategy_review",
                strategy_sha256="abc",
            )
            result = validate_completion(path, task_dir=task, attempt_id="A001")
            self.assertTrue(result.valid, result.reasons)
            self.assertEqual(attempt / "COMPLETION.json", path)

    def test_changed_handoff_invalidates_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, _attempt = self.make_task(Path(temporary))
            path = write_completion(
                task,
                attempt_id="A001",
                phase="planning",
                requested_state="strategy_review",
                strategy_sha256="abc",
            )
            (task / "HANDOFF.json").write_text(
                json.dumps({"requested_state": "blocked"}), encoding="utf-8"
            )
            result = validate_completion(path, task_dir=task, attempt_id="A001")
            self.assertFalse(result.valid)
            self.assertTrue(any("handoff_sha256" in reason for reason in result.reasons))

    def test_completion_for_stale_attempt_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, _attempt = self.make_task(Path(temporary))
            path = write_completion(
                task,
                attempt_id="A001",
                phase="planning",
                requested_state="strategy_review",
                strategy_sha256="abc",
            )
            status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
            status["current_attempt_id"] = "A002"
            (task / "STATUS.json").write_text(json.dumps(status), encoding="utf-8")
            result = validate_completion(path, task_dir=task, attempt_id="A001")
            self.assertFalse(result.valid)
            self.assertTrue(any("current task attempt" in reason for reason in result.reasons))

    def test_execution_attempt_may_request_strategy_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, attempt = self.make_task(Path(temporary))
            status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
            status["state"] = "running"
            (task / "STATUS.json").write_text(json.dumps(status), encoding="utf-8")
            metadata = json.loads((attempt / "ATTEMPT.json").read_text(encoding="utf-8"))
            metadata["phase"] = "execution"
            (attempt / "ATTEMPT.json").write_text(json.dumps(metadata), encoding="utf-8")
            path = write_completion(
                task,
                attempt_id="A001",
                phase="execution",
                requested_state="strategy_review",
                strategy_sha256="abc",
            )
            result = validate_completion(path, task_dir=task, attempt_id="A001")
            self.assertTrue(result.valid, result.reasons)


if __name__ == "__main__":
    unittest.main()
