import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rdo import task_revise, task_review


class TaskReviewTests(unittest.TestCase):
    def _task(self, root: Path) -> Path:
        task = root / "run-1" / "tasks" / "T101-example"
        (task / "reviews").mkdir(parents=True)
        (task / "STATUS.json").write_text(
            json.dumps(
                {
                    "task_id": "T101-example",
                    "state": "review",
                    "previous_state": "running",
                    "owner": "worker",
                    "state_history": [],
                }
            ),
            encoding="utf-8",
        )
        return task

    def test_changes_requested_records_decision_and_transitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = self._task(Path(temporary))
            findings = task / "reviews" / "findings.md"
            findings.write_text("# Findings\n\nFix the stale contract.\n", encoding="utf-8")

            result = task_review(
                argparse.Namespace(
                    task_dir=str(task),
                    decision="changes_requested",
                    reviewer="codex",
                    findings_file=str(findings),
                    note=["Focused recovery"],
                )
            )

            self.assertEqual(result, 0)
            status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "changes_requested")
            decision = json.loads(
                (task / "reviews" / "DECISION-v001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(decision["decision"], "changes_requested")
            self.assertEqual(decision["findings_path"], "reviews/findings.md")
            pointer = json.loads(
                (task / "reviews" / "CURRENT_TASK_REVIEW.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(pointer["decision_path"], "reviews/DECISION-v001.json")
            events = [
                json.loads(line)
                for line in (task.parent.parent / "EVENTS.ndjson")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [item["event"] for item in events],
                ["coordinator_reviewed", "changes_requested"],
            )

    def test_findings_must_be_task_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self._task(root)
            findings = root / "outside.md"
            findings.write_text("not task local", encoding="utf-8")

            with self.assertRaisesRegex(SystemExit, "inside the task directory"):
                task_review(
                    argparse.Namespace(
                        task_dir=str(task),
                        decision="changes_requested",
                        reviewer="codex",
                        findings_file=str(findings),
                        note=[],
                    )
                )

    def test_revise_is_a_thin_changes_requested_review_alias(self):
        arguments = argparse.Namespace(
            task_dir="/task",
            reviewer="coordinator",
            findings_file="/task/review.md",
            note=["one note"],
        )
        with patch("rdo.task_review", return_value=0) as review:
            self.assertEqual(0, task_revise(arguments))
        submitted = review.call_args.args[0]
        self.assertEqual("changes_requested", submitted.decision)
        self.assertEqual(arguments.task_dir, submitted.task_dir)
        self.assertEqual(arguments.reviewer, submitted.reviewer)
        self.assertEqual(arguments.findings_file, submitted.findings_file)
        self.assertEqual(arguments.note, submitted.note)


if __name__ == "__main__":
    unittest.main()
