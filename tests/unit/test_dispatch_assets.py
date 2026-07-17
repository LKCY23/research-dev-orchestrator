import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dispatch_assets import render_worker_prompt
from strategy import DEFAULT_EXECUTION_POLICY


class DispatchAssetsTests(unittest.TestCase):
    def test_compact_resume_uses_delta_instead_of_repeating_frozen_packet(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            attempt = task / "attempts" / "A001"
            attempt.mkdir(parents=True)
            (attempt / "ATTEMPT.json").write_text(
                json.dumps(
                    {
                        "state": "invalid_handoff",
                        "outcome": "timed_out_unfinalized",
                        "handoff_state": None,
                    }
                ),
                encoding="utf-8",
            )
            (task / "STATUS.json").write_text(
                json.dumps(
                    {
                        "task_id": "T101-example",
                        "state": "blocked",
                        "profile": "direct",
                        "artifact_protocol_version": 2,
                        "current_attempt_id": "A001",
                        "blocking_reason": "Finish the bounded API fix.",
                    }
                ),
                encoding="utf-8",
            )
            (task / "TASK.md").write_text(
                "UNIQUE FULL TASK CONTENT " * 200, encoding="utf-8"
            )
            (task / "CONTEXT.md").write_text(
                "UNIQUE FULL CONTEXT CONTENT " * 200, encoding="utf-8"
            )
            (task / "ACCEPTANCE.md").write_text(
                """# Acceptance

```json rdo-acceptance-contract
{"schema_version":2,"required_commands":[{"id":"focused","argv":["true"],"cwd":".","timeout_seconds":10}],"required_outputs":["result.txt"],"pre_merge_commands":[],"post_merge_commands":[]}
```

## Behavioral Checks

- The resumed worker preserves completed work and proves the focused behavior.

## Merge Preconditions

- Focused check passes.

## Blocked Conditions

- Required input is unavailable.

## Pre-Merge Checks

- None.

## Post-Merge Checks

- None.
""",
                encoding="utf-8",
            )
            (task / "EXECUTION_POLICY.json").write_text(
                json.dumps(DEFAULT_EXECUTION_POLICY), encoding="utf-8"
            )
            arguments = dict(
                worktree_path="/tmp/not-materialized-worktree",
                task_dir=task,
                status_path=task / "STATUS.json",
                attempt_dir=task / "attempts" / "A002",
                worker_backend="claude-code",
                phase="execution",
            )

            full = render_worker_prompt(**arguments)
            compact = render_worker_prompt(
                **arguments,
                prompt_mode="compact_resume",
                prompt_mode_reason="backend_session_resume_preflight_passed",
            )

            self.assertIn("# Worker Resume Prompt", compact)
            self.assertIn("## Current Source State", compact)
            self.assertIn("## Remaining Work", compact)
            self.assertIn("Parent outcome: timed_out_unfinalized", compact)
            self.assertIn("## Critical Proof Obligations", compact)
            self.assertIn("Required acceptance check IDs: [\"focused\"]", compact)
            self.assertNotIn("\n## TASK.md\n", compact)
            self.assertNotIn("\n## CONTEXT.md\n", compact)
            self.assertNotIn("UNIQUE FULL TASK CONTENT", compact)
            self.assertLess(len(compact), len(full))

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
            self.assertIn("strategy scaffold --attempt-dir", prompt)
            self.assertIn("strategy draft --attempt-dir", prompt)
            self.assertIn("strategy submit --task-dir", prompt)
            self.assertIn("--draft", prompt)

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
            self.assertIn("strategy preflight --attempt-dir", prompt)
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

    def test_v2_prompt_is_input_complete_without_protocol_file_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            (task / "STATUS.json").write_text(
                json.dumps(
                    {
                        "task_id": "T101-example",
                        "profile": "direct",
                        "artifact_protocol_version": 2,
                    }
                ),
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
                attempt_dir=task / "attempts" / "A001",
                worker_backend="claude-code",
                phase="execution",
            )

            self.assertIn("## EXECUTION_POLICY.json", prompt)
            self.assertIn("frozen and fully embedded below", prompt)
            self.assertIn("do not re-read task-dir copies or TASK_INPUTS.json", prompt)
            self.assertIn("do not bypass the policy with Bash, Python, cat", prompt)
            self.assertNotIn("- STATUS_PATH:", prompt)
            self.assertNotIn("- TASK_INPUTS_PATH:", prompt)
            self.assertNotIn("- EVIDENCE_PATH:", prompt)
            self.assertNotIn("- HANDOFF_READY_PATH:", prompt)

    def test_full_execution_embeds_strategy_resume_summary_and_complete_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            (task / "STATUS.json").write_text(
                json.dumps(
                    {
                        "task_id": "T101-example",
                        "profile": "full",
                        "artifact_protocol_version": 2,
                    }
                ),
                encoding="utf-8",
            )
            (task / "EXECUTION_POLICY.json").write_text(
                json.dumps(DEFAULT_EXECUTION_POLICY), encoding="utf-8"
            )
            for name in ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md"):
                (task / name).write_text(name, encoding="utf-8")
            strategy = {
                "strategy_id": "T101-example-S001",
                "workflows": [
                    {
                        "workflow_id": "WF-reused",
                        "resume": {
                            "from_attempt": "A000",
                            "from_workflow": "WF-old",
                            "mode": "reuse",
                        },
                    },
                    {
                        "workflow_id": "WF-revalidate",
                        "resume": {
                            "from_attempt": "A000",
                            "from_workflow": "WF-check",
                            "mode": "revalidate",
                        },
                    },
                    {"workflow_id": "WF-new"},
                ],
            }
            strategy_path = task / "strategy.json"
            strategy_path.write_text(json.dumps(strategy), encoding="utf-8")
            attempt = task / "attempts" / "A001"

            prompt = render_worker_prompt(
                worktree_path="/tmp/worktree",
                task_dir=task,
                status_path=task / "STATUS.json",
                attempt_dir=attempt,
                worker_backend="claude-code",
                phase="execution",
                strategy_path=str(strategy_path),
            )

            self.assertIn("## Approved Strategy (embedded, exact)", prompt)
            self.assertIn('carried_forward_workflows = ["WF-reused"]', prompt)
            self.assertIn(
                'remaining_workflows = ["WF-revalidate", "WF-new"]', prompt
            )
            self.assertIn(
                f"workflow start --attempt-dir {attempt} --workflow-id WF-revalidate --instance-id WF-revalidate-I001",
                prompt,
            )
            self.assertIn(
                f"workflow complete --attempt-dir {attempt} --workflow-id WF-new --instance-id WF-new-I001",
                prompt,
            )
            self.assertNotIn("--workflow-id WF-reused --instance-id", prompt)
            self.assertIn("Do not read", prompt)
            self.assertIn("RESUME_CONTEXT.json", prompt)
            self.assertIn("Do not run the same acceptance argv", prompt)

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
