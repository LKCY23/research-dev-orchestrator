from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from task_budget import (  # noqa: E402
    TaskBudgetError,
    assess_task_budget,
    attempt_budget_binding_reasons,
    attempt_budget_receipt_reasons,
    normalize_task_budget,
    validate_assessment,
    write_assessment_immutable,
)
from supervisor import AttemptDeadline  # noqa: E402


def iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class TaskBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.task = Path(self.temporary.name) / "T001"
        (self.task / "attempts").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_json(self, path: Path, payload: dict) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(payload, indent=2).encode() + b"\n"
        path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def policy(self, **limits: int | float) -> None:
        self.write_json(
            self.task / "EXECUTION_POLICY.json",
            {"schema_version": 2, "task_budget": limits or None},
        )

    def attempt(
        self,
        attempt_id: str,
        *,
        execution_seconds: float,
        cost_usd: float | None,
        wall_seconds: int = 100,
        overall_elapsed: float | None = None,
        finalization_started: float | None = None,
    ) -> None:
        attempt = self.task / "attempts" / attempt_id
        self.write_json(
            attempt / "ATTEMPT.json",
            {
                "artifact_protocol_version": 2,
                "attempt_id": attempt_id,
                "task_id": "T001",
                "backend_id": "claude-code",
                "phase": "execution",
                "state": "invalid_handoff",
            },
        )
        started = 1_750_000_000.0
        deadline = {
            "schema_version": 1,
            "started_at": iso(started),
            "started_at_epoch": started,
            "execution_deadline_at": iso(started + wall_seconds),
            "execution_deadline_at_epoch": started + wall_seconds,
            "attempt_wall_seconds": wall_seconds,
            "finalization_grace_seconds": 90,
            "reminder_seconds": 60,
        }
        deadline_sha = self.write_json(attempt / "runtime" / "DEADLINE.json", deadline)
        usage = None
        if cost_usd is not None:
            usage = {
                "totals": {"cost_usd": cost_usd},
                "observed_metrics": ["cost_usd"],
                "source_events": ["result"],
                "budget_exceeded": None,
            }
        self.write_json(
            attempt / "supervisor-result.json",
            {
                "exit_code": 1,
                "deadline_sha256": deadline_sha,
                "attempt_started_at_epoch": started,
                "execution_deadline_at_epoch": started + wall_seconds,
                "elapsed_seconds": overall_elapsed or execution_seconds,
                "execution_elapsed_seconds": execution_seconds,
                "finalization_started": finalization_started is not None,
                "usage": usage,
            },
        )

    def test_policy_accepts_partial_positive_limits_and_rejects_unsafe_values(self) -> None:
        self.assertEqual({"max_attempts": 2}, normalize_task_budget({"max_attempts": 2}))
        self.assertEqual(
            {"max_cost_usd": 1.5}, normalize_task_budget({"max_cost_usd": 1.5})
        )
        for invalid in ({}, {"max_attempts": 0}, {"max_cost_usd": float("nan")}, {"x": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(TaskBudgetError):
                normalize_task_budget(invalid)

    def test_absent_budget_preserves_unbounded_admission(self) -> None:
        self.policy()
        (self.task / "attempts" / "orphan").mkdir()
        assessment = assess_task_budget(
            self.task, requested_attempt_wall_seconds=90, next_attempt_id="A001"
        )
        self.assertFalse(assessment["enabled"])
        self.assertTrue(assessment["admission"]["allowed"])
        self.assertEqual(90, assessment["admission"]["attempt_wall_seconds"])
        self.assertEqual([], assessment["source_attempts"])

    def test_legacy_protocol_ignores_v2_task_budget(self) -> None:
        self.policy(max_attempts=1)
        assessment = assess_task_budget(
            self.task,
            requested_attempt_wall_seconds=90,
            next_attempt_id="A001",
            artifact_protocol_version=1,
        )
        self.assertFalse(assessment["enabled"])
        self.assertEqual(1, assessment["artifact_protocol_version"])

    def test_cross_attempt_accounting_bounds_next_wall_and_cost(self) -> None:
        self.policy(max_attempts=3, max_execution_seconds=100, max_cost_usd=5)
        self.attempt("A001", execution_seconds=20, cost_usd=1)
        self.attempt("A002", execution_seconds=30, cost_usd=1.5)

        first = assess_task_budget(
            self.task, requested_attempt_wall_seconds=90, next_attempt_id="A003"
        )
        second = assess_task_budget(
            self.task, requested_attempt_wall_seconds=90, next_attempt_id="A003"
        )

        self.assertEqual(first, second)
        self.assertEqual(
            {"attempts": 2, "execution_seconds": 50.0, "cost_usd": 2.5},
            first["consumed"],
        )
        self.assertEqual(
            {"attempts": 1, "execution_seconds": 50.0, "cost_usd": 2.5},
            first["remaining"],
        )
        self.assertEqual(50.0, first["admission"]["attempt_wall_seconds"])
        self.assertEqual(2.5, first["admission"]["max_cost_usd"])
        self.assertTrue(first["admission"]["allowed"])

    def test_finalization_grace_is_not_charged(self) -> None:
        self.policy(max_execution_seconds=60)
        self.attempt(
            "A001",
            execution_seconds=40,
            cost_usd=None,
            overall_elapsed=95,
            finalization_started=40,
        )
        assessment = assess_task_budget(self.task, requested_attempt_wall_seconds=60)
        self.assertEqual(40.0, assessment["consumed"]["execution_seconds"])
        self.assertEqual(20.0, assessment["remaining"]["execution_seconds"])

    def test_supervisor_execution_clock_stops_at_finalization(self) -> None:
        deadline = AttemptDeadline(
            attempt_started_epoch=100,
            execution_deadline_epoch=200,
            finalization_grace_seconds=90,
            reminder_seconds=60,
            execution_deadline_monotonic=100,
            finalization_started_epoch=140,
            finalization_deadline_epoch=290,
            finalization_deadline_monotonic=190,
        )
        self.assertEqual(40, deadline.execution_elapsed_seconds(now_epoch=250))

    def test_attempt_limit_counts_failures_but_not_uncreated_preflight(self) -> None:
        self.policy(max_attempts=1)
        (self.task / "attempts" / "preflight-only").mkdir()
        self.attempt("A001", execution_seconds=1, cost_usd=None)
        assessment = assess_task_budget(self.task, requested_attempt_wall_seconds=10)
        self.assertFalse(assessment["admission"]["allowed"])
        self.assertEqual(1, assessment["consumed"]["attempts"])
        self.assertEqual("budget", assessment["admission"]["blocker_type"])

    def test_missing_cost_observation_fails_closed(self) -> None:
        self.policy(max_cost_usd=5)
        self.attempt("A001", execution_seconds=1, cost_usd=None)
        assessment = assess_task_budget(self.task, requested_attempt_wall_seconds=10)
        self.assertIsNone(assessment["consumed"]["cost_usd"])
        self.assertFalse(assessment["admission"]["allowed"])
        self.assertEqual("observation_missing", assessment["admission"]["reasons"][0]["code"])

    def test_missing_or_unbound_execution_receipt_fails_closed(self) -> None:
        self.policy(max_execution_seconds=50)
        self.attempt("A001", execution_seconds=10, cost_usd=None)
        receipt_path = self.task / "attempts" / "A001" / "supervisor-result.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["deadline_sha256"] = "0" * 64
        self.write_json(receipt_path, receipt)
        assessment = assess_task_budget(self.task, requested_attempt_wall_seconds=20)
        self.assertFalse(assessment["admission"]["allowed"])
        self.assertIsNone(assessment["consumed"]["execution_seconds"])
        self.assertEqual("observation_missing", assessment["admission"]["reasons"][0]["code"])

    def test_eager_zero_without_a_cost_source_event_fails_closed(self) -> None:
        self.policy(max_cost_usd=5)
        self.attempt("A001", execution_seconds=1, cost_usd=0)
        receipt_path = self.task / "attempts" / "A001" / "supervisor-result.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["usage"]["source_events"] = ["assistant"]
        self.write_json(receipt_path, receipt)
        assessment = assess_task_budget(self.task, requested_attempt_wall_seconds=10)
        self.assertFalse(assessment["admission"]["allowed"])
        self.assertIsNone(assessment["consumed"]["cost_usd"])

    def test_freeze_binds_admission_to_one_attempt(self) -> None:
        self.policy(max_attempts=2)
        assessment = assess_task_budget(
            self.task, requested_attempt_wall_seconds=10, next_attempt_id="A001"
        )
        attempt = self.task / "attempts" / "A001"
        path, digest = write_assessment_immutable(attempt, assessment)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual(assessment, validate_assessment(json.loads(path.read_text())))
        with self.assertRaises(TaskBudgetError):
            write_assessment_immutable(attempt, assessment)

    def test_attempt_binding_detects_snapshot_tampering(self) -> None:
        self.policy(max_attempts=2)
        assessment = assess_task_budget(
            self.task, requested_attempt_wall_seconds=10, next_attempt_id="A001"
        )
        attempt_dir = self.task / "attempts" / "A001"
        budget_path, budget_sha = write_assessment_immutable(attempt_dir, assessment)
        self.write_json(
            attempt_dir / "TASK_INPUTS.json",
            {
                "inputs": {
                    "execution_policy": {
                        "sha256": assessment["execution_policy_sha256"]
                    }
                }
            },
        )
        self.write_json(
            attempt_dir / "runtime" / "BACKEND_PROFILE.json",
            {
                "task_budget": {
                    "assessment_sha256": assessment["assessment_sha256"]
                }
            },
        )
        attempt = {
            "attempt_id": "A001",
            "task_budget_ref": "runtime/TASK_BUDGET.json",
            "task_budget_sha256": budget_sha,
            "task_budget_assessment_sha256": assessment["assessment_sha256"],
        }
        self.assertEqual([], attempt_budget_binding_reasons(attempt_dir, attempt))
        budget_path.write_text("{}\n", encoding="utf-8")
        self.assertTrue(attempt_budget_binding_reasons(attempt_dir, attempt))

    def test_terminal_receipt_is_required_for_each_metered_dimension(self) -> None:
        self.policy(max_execution_seconds=50, max_cost_usd=5)
        assessment = assess_task_budget(
            self.task, requested_attempt_wall_seconds=20, next_attempt_id="A001"
        )
        attempt_dir = self.task / "attempts" / "A001"
        write_assessment_immutable(attempt_dir, assessment)
        self.attempt("A001", execution_seconds=10, cost_usd=1)
        attempt = json.loads((attempt_dir / "ATTEMPT.json").read_text())
        attempt["task_budget_ref"] = "runtime/TASK_BUDGET.json"
        self.assertEqual([], attempt_budget_receipt_reasons(attempt_dir, attempt))
        receipt_path = attempt_dir / "supervisor-result.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["usage"]["source_events"] = ["assistant"]
        self.write_json(receipt_path, receipt)
        reasons = attempt_budget_receipt_reasons(attempt_dir, attempt)
        self.assertTrue(any("cost observation" in reason for reason in reasons))

    def test_cli_cross_attempt_fixture(self) -> None:
        self.policy(max_attempts=3, max_execution_seconds=100)
        self.attempt("A001", execution_seconds=25, cost_usd=None)
        self.attempt("A002", execution_seconds=35, cost_usd=None)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "task_budget_cli.py"),
                "assess",
                "--task-dir",
                str(self.task),
                "--attempt-wall-seconds",
                "90",
                "--next-attempt-id",
                "A003",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(40.0, payload["remaining"]["execution_seconds"])
        self.assertEqual(40.0, payload["admission"]["attempt_wall_seconds"])


if __name__ == "__main__":
    unittest.main()
