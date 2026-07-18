import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rdo


class WorkflowCompletionGateTests(unittest.TestCase):
    def strategy(self) -> dict:
        return {
            "global_budget": {"max_parallel_workflows": 1, "max_workflow_instances": 1},
            "workflows": [
                {
                    "workflow_id": "WF-acceptance",
                    "kind": "verification",
                    "depends_on": [],
                    "required": True,
                    "budget": {
                        "wall_seconds": 600,
                        "command_seconds": 60,
                        "max_instances": 1,
                    },
                    "on_timeout": "block",
                }
            ],
            "completion_gate": {
                "required_workflows_complete": True,
                "acceptance_commands_pass": True,
                "optional_workflows_may_timeout": False,
            },
        }

    def args(self, action: str, attempt: Path) -> argparse.Namespace:
        return argparse.Namespace(
            workflow_action=action,
            attempt_dir=str(attempt),
            workflow_id="WF-acceptance",
            instance_id="WF-acceptance-I001",
        )

    def test_failed_early_gate_keeps_instance_active_for_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt = root / "task" / "attempts" / "A001"
            attempt.mkdir(parents=True)
            strategy = self.strategy()
            with patch("rdo.active_execution_attempt", return_value=(attempt, root / "task", strategy)), patch("rdo.event"):
                rdo.workflow_action(self.args("start", attempt))
                with self.assertRaisesRegex(SystemExit, "acceptance command records are missing"):
                    rdo.workflow_action(self.args("complete", attempt))
                records = rdo.workflow_events(attempt)
                self.assertEqual(["workflow_started"], [item["event"] for item in records])

                runtime = attempt / "runtime"
                with (runtime / "COMMANDS.ndjson").open("w", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "acceptance": True,
                        "command": ["pytest", "-q"],
                        "exit_code": 0,
                        "timed_out": False,
                    }) + "\n")
                rdo.workflow_action(self.args("complete", attempt))

            records = rdo.workflow_events(attempt)
            self.assertEqual(
                ["workflow_started", "workflow_completed"],
                [item["event"] for item in records],
            )
            finalization = json.loads((attempt / "runtime" / "FINALIZATION.json").read_text())
            self.assertEqual("finalizing", finalization["stage"])

    def test_failed_acceptance_record_blocks_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "A001"
            runtime = attempt / "runtime"
            runtime.mkdir(parents=True)
            (runtime / "COMMANDS.ndjson").write_text(
                json.dumps({"acceptance": True, "exit_code": 1, "timed_out": False}) + "\n",
                encoding="utf-8",
            )
            reasons = rdo.completion_gate_reasons(
                attempt,
                self.strategy(),
                completing_workflow="WF-acceptance",
            )
            self.assertIn("one or more acceptance commands failed or timed out", reasons)

    def test_workflow_and_exec_are_rejected_after_finalization_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "task"
            attempt = task / "attempts" / "A001"
            runtime = attempt / "runtime"
            runtime.mkdir(parents=True)
            (runtime / "FINALIZATION.json").write_text(
                json.dumps({"stage": "finalizing"}) + "\n",
                encoding="utf-8",
            )
            strategy = self.strategy()
            with patch(
                "rdo.active_execution_attempt",
                return_value=(attempt, task, strategy),
            ), patch("rdo.event"):
                with self.assertRaisesRegex(SystemExit, "forbidden after finalization"):
                    rdo.workflow_action(self.args("start", attempt))
                with self.assertRaisesRegex(SystemExit, "forbidden after finalization"):
                    rdo._execute_command_locked(
                        argparse.Namespace(
                            attempt_dir=str(attempt),
                            acceptance=False,
                            workflow_id="WF-acceptance",
                            instance_id="WF-acceptance-I001",
                            timeout=1,
                            cwd="",
                            command=["--", "true"],
                        )
                    )

    def test_independent_review_requires_observed_distinct_reviewers(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "A001"
            runtime = attempt / "runtime"
            reviews = runtime / "reviews"
            reviews.mkdir(parents=True)
            started_at = rdo.utc_now()
            (reviews / "one.md").write_text("No findings.\n")
            (reviews / "two.md").write_text("One minor finding.\n")
            with (runtime / "BACKEND_EVENTS.ndjson").open("w") as handle:
                handle.write(json.dumps({"at": started_at, "event": "subagent_started", "session_id": "reviewer-1"}) + "\n")
                handle.write(json.dumps({"at": started_at, "event": "backend_agent_started", "agent_id": "reviewer-2"}) + "\n")
                stopped_at = rdo.utc_now()
                handle.write(json.dumps({"at": stopped_at, "event": "subagent_stopped", "session_id": "reviewer-1"}) + "\n")
                handle.write(json.dumps({"at": stopped_at, "event": "backend_agent_stopped", "agent_id": "reviewer-2"}) + "\n")
            definition = {"review": {"mode": "independent", "required_reviewers": 2}}
            evidence = rdo.independent_review_evidence(attempt, definition, [
                f"reviewer-1={reviews / 'one.md'}",
                f"reviewer-2={reviews / 'two.md'}",
            ], workflow_id="WF-review", instance_id="I001", workflow_started_at=started_at)
            self.assertEqual({item["reviewer_id"] for item in evidence}, {"reviewer-1", "reviewer-2"})
            self.assertTrue(all(item["receipt_ref"] for item in evidence))
            (runtime / "WORKFLOWS.ndjson").write_text(
                json.dumps(
                    {
                        "event": "workflow_completed",
                        "workflow_id": "WF-review",
                        "instance_id": "I001",
                        "reviews": evidence,
                    }
                ) + "\n",
                encoding="utf-8",
            )
            refs = rdo._reviewer_evidence_refs(attempt)
            self.assertEqual(2, len(refs))
            self.assertTrue(all(item["receipt_sha256"] for item in refs))
            linked = reviews / "linked.md"
            linked.symlink_to(reviews / "one.md")
            with self.assertRaisesRegex(SystemExit, "must not traverse symlinks"):
                rdo.independent_review_evidence(
                    attempt,
                    {"review": {"mode": "independent", "required_reviewers": 1}},
                    [f"reviewer-1={linked}"],
                    workflow_id="WF-review",
                    instance_id="I-symlink",
                    workflow_started_at=started_at,
                )
            with self.assertRaisesRegex(SystemExit, "no completed lifecycle|not observed"):
                rdo.independent_review_evidence(
                    attempt, definition, [
                        f"reviewer-1={reviews / 'one.md'}",
                        f"fake={reviews / 'two.md'}",
                    ], workflow_id="WF-review", instance_id="I002", workflow_started_at=started_at
                )

    def test_independent_review_rejects_artifact_created_after_reviewer_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "A001"
            reviews = attempt / "runtime" / "reviews"
            reviews.mkdir(parents=True)
            started_at = "2026-07-18T00:00:00Z"
            stopped_at = "2026-07-18T00:00:01Z"
            (attempt / "runtime" / "BACKEND_EVENTS.ndjson").write_text(
                json.dumps({"at": started_at, "event": "subagent_started", "session_id": "reviewer-1"}) + "\n" +
                json.dumps({"at": stopped_at, "event": "subagent_stopped", "session_id": "reviewer-1"}) + "\n",
                encoding="utf-8",
            )
            artifact = reviews / "late.md"
            artifact.write_text("Written by the primary worker later.\n", encoding="utf-8")
            definition = {"review": {"mode": "independent", "required_reviewers": 1}}
            with self.assertRaisesRegex(SystemExit, "not written during"):
                rdo.independent_review_evidence(
                    attempt,
                    definition,
                    [f"reviewer-1={artifact}"],
                    workflow_id="WF-review",
                    instance_id="I001",
                    workflow_started_at=started_at,
                )

    def test_completed_review_evidence_digest_cannot_drift_before_finalize(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "task" / "attempts" / "A001"
            artifact = attempt / "runtime" / "reviews" / "review.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("No findings.\n", encoding="utf-8")
            import hashlib

            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            (attempt / "runtime" / "WORKFLOWS.ndjson").write_text(
                json.dumps(
                    {
                        "event": "workflow_completed",
                        "workflow_id": "WF-review",
                        "instance_id": "I001",
                        "reviews": [
                            {
                                "reviewer_id": "reviewer-1",
                                "artifact": str(artifact),
                                "sha256": digest,
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            artifact.write_text("Changed after completion.\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "changed after workflow completion"):
                rdo._reviewer_evidence_refs(attempt)


if __name__ == "__main__":
    unittest.main()
