import copy
import tempfile
import unittest
from pathlib import Path

from protocol import write_json
from strategy import (
    DEFAULT_EXECUTION_POLICY,
    StrategyValidationError,
    canonical_digest,
    load_approved_strategy,
    review_strategy,
    submit_strategy,
)


def strategy_payload(task_id: str, revision: int = 1, supersedes=None):
    return {
        "schema_version": 2,
        "backend_id": "claude-code",
        "strategy_id": f"{task_id}-S{revision:03d}",
        "task_id": task_id,
        "revision": revision,
        "supersedes": supersedes,
        "objective": "Implement and verify the task",
        "global_budget": {
            "wall_seconds": 60,
            "max_workflows": 1,
            "max_workflow_instances": 1,
            "max_parallel_workflows": 1,
            "max_subagents": 1,
            "max_parallel_subagents": 1,
        },
        "workflows": [{
            "workflow_id": "WF-implementation",
            "kind": "implementation",
            "purpose": "Implement the requested change",
            "depends_on": [],
            "required": True,
            "executor": {
                "mode": "primary_worker",
                "write_access": True,
                "max_agents": 0,
                "max_parallel": 0,
                "allowed_paths": ["src/"],
            },
            "budget": {"wall_seconds": 60, "command_seconds": 10, "max_enumerated_cases": 10, "max_instances": 1},
            "completion": {"evidence": "tests pass"},
            "on_timeout": "block",
        }],
        "runtime_change_policy": {
            "allow_new_instances_of_approved_workflows": True,
            "require_revision_for_new_workflow_kind": True,
            "require_revision_for_budget_increase": True,
            "allow_unbounded_search": False,
        },
        "completion_gate": {
            "required_workflows_complete": True,
            "acceptance_commands_pass": True,
            "optional_workflows_may_timeout": True,
        },
    }


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.task = Path(self.temporary.name)
        (self.task / "strategy").mkdir()
        write_json(self.task / "STATUS.json", {"task_id": "T001-test"})
        write_json(self.task / "EXECUTION_POLICY.json", copy.deepcopy(DEFAULT_EXECUTION_POLICY))

    def tearDown(self):
        self.temporary.cleanup()

    def test_approval_binds_exact_digest(self):
        payload = strategy_payload("T001-test")
        _, digest = submit_strategy(self.task, payload)
        review_strategy(self.task, 1, decision="approved", reviewer="test")
        loaded, review = load_approved_strategy(self.task)
        self.assertEqual(digest, canonical_digest(loaded))
        self.assertEqual(digest, review["strategy_sha256"])

    def test_revisions_are_sequential_and_immutable(self):
        first = strategy_payload("T001-test")
        submit_strategy(self.task, first)
        with self.assertRaises(StrategyValidationError):
            submit_strategy(self.task, first)
        third = strategy_payload("T001-test", 3, first["strategy_id"])
        with self.assertRaises(StrategyValidationError):
            submit_strategy(self.task, third)

    def test_policy_rejects_excess_budget(self):
        payload = strategy_payload("T001-test")
        payload["global_budget"]["wall_seconds"] = DEFAULT_EXECUTION_POLICY["attempt_wall_seconds"] + 1
        with self.assertRaises(StrategyValidationError):
            submit_strategy(self.task, payload)

    def test_strategy_must_match_planning_attempt_backend(self):
        attempt_id = "A001-codex"
        attempt_dir = self.task / "attempts" / attempt_id
        attempt_dir.mkdir(parents=True)
        write_json(attempt_dir / "ATTEMPT.json", {"backend_id": "codex"})
        write_json(self.task / "STATUS.json", {
            "task_id": "T001-test",
            "state": "planning",
            "current_attempt_id": attempt_id,
        })
        with self.assertRaises(StrategyValidationError):
            submit_strategy(self.task, strategy_payload("T001-test"))


if __name__ == "__main__":
    unittest.main()
