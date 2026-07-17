from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from artifact_bundle import artifact_binding, publish_bundle, validate_task_inputs_binding
from context_broker import bounded_text
from dependency_context import (
    DependencyContextError,
    build_dependency_context,
    write_dependency_context_immutable,
)
from dispatch_assets import render_worker_prompt
from task_contract import build_task_inputs_payload, write_task_inputs_immutable


COMMIT = "a" * 40


def task_text(task_id: str, dependency: str | None = None) -> str:
    dependencies = (
        [{"task_id": dependency, "required_state": "merged"}]
        if dependency
        else []
    )
    return f"""# Task

## Objective

Implement {task_id}.

## Deliverables

- Bounded implementation.

## Invariants

- Preserve the contract.

## Non-goals

- No unrelated work.

## Dependencies

```json rdo-task-dependencies
{json.dumps({"schema_version": 2, "dependencies": dependencies}, indent=2)}
```
"""


CONTEXT = """# Context

## Frozen Decisions

- Keep deterministic retrieval.

## Required Interfaces

- INTERFACE DETAIL: SupervisorResult exposes cleanup_verified.

## Local Code Map

- `scripts/supervisor.py` owns the result.

## Necessary Background

- No additional background.
"""


ACCEPTANCE = """# Acceptance

```json rdo-acceptance-contract
{"schema_version":2,"required_commands":[{"id":"unit","argv":["true"],"cwd":".","timeout_seconds":10}],"required_outputs":[],"pre_merge_commands":[],"post_merge_commands":[]}
```

## Behavioral Checks

- The behavior is deterministic.

## Merge Preconditions

- Checks pass.

## Blocked Conditions

- Inputs are unavailable.

## Pre-Merge Checks

- None.

## Post-Merge Checks

- None.
"""


POLICY = {
    "schema_version": 2,
    "strategy_required": False,
    "attempt_wall_seconds": 120,
    "max_workflows": 2,
    "max_workflow_instances": 2,
    "max_parallel_workflows": 1,
    "max_subagents": 1,
    "max_parallel_subagents": 1,
    "default_command_seconds": 30,
    "max_enumerated_cases": 100,
    "allow_unbounded_search": False,
    "allowed_paths": ["src"],
    "read_paths": ["src"],
    "forbidden_paths": [],
    "context_sources": [],
}


class DependencyContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / ".agent-collab" / "runs" / "R001"
        self.predecessor = self.run_dir / "tasks" / "T000"
        self.task = self.run_dir / "tasks" / "T001"
        self.predecessor_attempt = self.predecessor / "attempts" / "A001-worker"
        self.attempt = self.task / "attempts" / "A001-worker"
        self.predecessor_attempt.mkdir(parents=True)
        self.attempt.mkdir(parents=True)
        self._write_inputs(self.predecessor, "T000")
        self._write_inputs(self.task, "T001", dependency="T000")
        (self.task / "CONTEXT.md").write_text(
            CONTEXT.replace(
                "INTERFACE DETAIL: SupervisorResult exposes cleanup_verified.",
                "CURRENT TASK INTERFACE: consume the predecessor contract.",
            ),
            encoding="utf-8",
        )
        self._publish_predecessor()
        self._freeze_current_context()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def _write_inputs(self, task: Path, task_id: str, dependency: str | None = None) -> None:
        task.mkdir(parents=True, exist_ok=True)
        (task / "TASK.md").write_text(task_text(task_id, dependency), encoding="utf-8")
        (task / "CONTEXT.md").write_text(CONTEXT, encoding="utf-8")
        (task / "ACCEPTANCE.md").write_text(ACCEPTANCE, encoding="utf-8")
        self._write_json(task / "EXECUTION_POLICY.json", POLICY)

    @staticmethod
    def _source_bytes(task: Path) -> dict[str, bytes]:
        return {
            name: (task / name).read_bytes()
            for name in ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md", "EXECUTION_POLICY.json")
        }

    def _publish_predecessor(self) -> None:
        inputs = build_task_inputs_payload(
            task_id="T000",
            attempt_id=self.predecessor_attempt.name,
            source_bytes=self._source_bytes(self.predecessor),
            task_base_commit=COMMIT,
            resolved_dependencies=[],
        )
        inputs_sha = write_task_inputs_immutable(
            self.predecessor_attempt / "TASK_INPUTS.json",
            inputs,
        )
        self._write_json(
            self.predecessor_attempt / "ATTEMPT.json",
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_id": "T000",
                "attempt_id": self.predecessor_attempt.name,
                "task_inputs_ref": "TASK_INPUTS.json",
                "task_inputs_sha256": inputs_sha,
                "state": "completed",
                "handoff_valid": True,
                "handoff_state": "review",
            },
        )
        bundle = publish_bundle(
            self.predecessor_attempt,
            requested_state="review",
            summary="SHORT OUTCOME: bounded supervision is complete.",
            known_limitations=["KNOWN LIMITATION: Darwin enforcement is separate."],
            direct_self_review={
                "performed": False,
                "passed": False,
                "summary": "",
                "findings": [],
            },
            source_commit=COMMIT,
            changed_paths=["scripts/supervisor.py", "tests/unit/test_supervisor.py"],
        )
        self._write_json(
            self.predecessor / "STATUS.json",
            {
                "task_id": "T000",
                "artifact_protocol_version": 2,
                "profile": "delegated",
                "state": "merged",
                "current_attempt_id": self.predecessor_attempt.name,
            },
        )
        event = {
            "at": "2026-07-17T00:00:00Z",
            "actor": "coordinator",
            "event": "task_merged",
            "run_id": "R001",
            "task_id": "T000",
            "commit": COMMIT,
            "attempt_id": self.predecessor_attempt.name,
            "artifact_binding": artifact_binding(bundle),
            "verification": {"passed": True},
        }
        (self.run_dir / "EVENTS.ndjson").write_text(
            json.dumps(event) + "\n",
            encoding="utf-8",
        )

    def _freeze_current_context(self) -> None:
        core = build_task_inputs_payload(
            task_id="T001",
            attempt_id=self.attempt.name,
            source_bytes=self._source_bytes(self.task),
            task_base_commit=COMMIT,
            resolved_dependencies=[
                {"task_id": "T000", "required_state": "merged", "commit": COMMIT}
            ],
        )
        manifest = build_dependency_context(attempt_dir=self.attempt, task_inputs=core)
        assert manifest is not None
        _path, manifest_sha = write_dependency_context_immutable(self.attempt, manifest)
        inputs = build_task_inputs_payload(
            task_id="T001",
            attempt_id=self.attempt.name,
            source_bytes=self._source_bytes(self.task),
            task_base_commit=COMMIT,
            resolved_dependencies=[
                {"task_id": "T000", "required_state": "merged", "commit": COMMIT}
            ],
            dependency_context_binding={
                "ref": "runtime/DEPENDENCY_CONTEXT.json",
                "sha256": manifest_sha,
            },
        )
        inputs_sha = write_task_inputs_immutable(self.attempt / "TASK_INPUTS.json", inputs)
        self._write_json(
            self.attempt / "ATTEMPT.json",
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_id": "T001",
                "attempt_id": self.attempt.name,
                "task_inputs_ref": "TASK_INPUTS.json",
                "task_inputs_sha256": inputs_sha,
            },
        )
        worktree = self.root / "worktree"
        (worktree / "src").mkdir(parents=True)
        self._write_json(
            self.attempt / "runtime" / "READ_POLICY.json",
            {
                "schema_version": 1,
                "repo_root": str(self.root),
                "worktree": str(worktree),
                "write_paths": ["src"],
                "read_paths": ["src"],
                "forbidden_paths": [],
                "context_sources": [],
                "denied_roots": [],
                "large_markdown_bytes": 16384,
                "section_max_bytes": 16384,
            },
        )

    def broker(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "context_broker.py"),
                "--policy",
                str(self.attempt / "runtime" / "READ_POLICY.json"),
                *arguments,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_manifest_is_short_and_bound_to_the_merged_artifact(self) -> None:
        manifest = json.loads(
            (self.attempt / "runtime" / "DEPENDENCY_CONTEXT.json").read_text()
        )
        entry = manifest["dependencies"][0]
        self.assertEqual("dependency:T000", entry["alias"])
        self.assertEqual(COMMIT, entry["merged_commit"])
        self.assertIn("SHORT OUTCOME", entry["prompt"]["summary_excerpt"])
        self.assertNotIn("INTERFACE DETAIL", json.dumps(manifest))
        validate_task_inputs_binding(
            self.attempt,
            expected_task_id="T001",
            expected_attempt_id=self.attempt.name,
        )
        _path, repeated_sha = write_dependency_context_immutable(
            self.attempt,
            manifest,
        )
        self.assertEqual(
            hashlib.sha256(
                (self.attempt / "runtime" / "DEPENDENCY_CONTEXT.json").read_bytes()
            ).hexdigest(),
            repeated_sha,
        )

    def test_broker_indexes_fields_and_retrieves_only_the_requested_section(self) -> None:
        index = json.loads(
            self.broker("index", "--source", "dependency:T000").stdout
        )
        source = index["sources"][0]
        self.assertIn("required_interfaces", source["sections"])
        self.assertNotIn("INTERFACE DETAIL", json.dumps(index))

        result = json.loads(
            self.broker(
                "get",
                "--source",
                "dependency:T000",
                "--section",
                "required_interfaces",
                "--question",
                "What cleanup contract is exposed?",
            ).stdout
        )
        self.assertIn("INTERFACE DETAIL", result["content"])
        self.assertNotIn("SHORT OUTCOME", result["content"])
        self.assertLessEqual(len(result["content"].encode()), result["max_bytes"])

        search = json.loads(
            self.broker(
                "search",
                "--source",
                "dependency:T000",
                "--query",
                "cleanup_verified",
            ).stdout
        )
        self.assertIn("cleanup_verified", search["content"])

    def test_manifest_drift_is_rejected_before_retrieval(self) -> None:
        path = self.attempt / "runtime" / "DEPENDENCY_CONTEXT.json"
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        result = self.broker(
            "get",
            "--source",
            "dependency:T000",
            "--section",
            "summary",
            "--question",
            "What completed?",
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("digest differs", result.stderr)

    def test_prompt_render_rejects_manifest_drift_before_attempt_publication(self) -> None:
        path = self.attempt / "runtime" / "DEPENDENCY_CONTEXT.json"
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(DependencyContextError, "digest differs"):
            render_worker_prompt(
                worktree_path=str(self.root / "worktree"),
                task_dir=self.task,
                status_path=self.task / "STATUS.json",
                attempt_dir=self.attempt,
                worker_backend="claude-code",
                phase="execution",
            )

    def test_manifest_cannot_register_an_undeclared_dependency_alias(self) -> None:
        manifest_path = self.attempt / "runtime" / "DEPENDENCY_CONTEXT.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["dependencies"][0]
        entry["task_id"] = "T999"
        entry["alias"] = "dependency:T999"
        entry["artifact_binding"]["task_id"] = "T999"
        binding_payload = {
            key: value for key, value in entry.items() if key != "binding_sha256"
        }
        entry["binding_sha256"] = hashlib.sha256(
            json.dumps(
                binding_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        self._write_json(manifest_path, manifest)

        inputs_path = self.attempt / "TASK_INPUTS.json"
        inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
        inputs["dependency_context"]["sha256"] = hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest()
        self._write_json(inputs_path, inputs)
        attempt_path = self.attempt / "ATTEMPT.json"
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        attempt["task_inputs_sha256"] = hashlib.sha256(
            inputs_path.read_bytes()
        ).hexdigest()
        self._write_json(attempt_path, attempt)

        result = self.broker(
            "index",
            "--source",
            "dependency:T999",
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("aliases differ", result.stderr)

    def test_artifact_only_get_does_not_read_unrequested_context(self) -> None:
        (self.predecessor / "CONTEXT.md").write_text(
            CONTEXT + "\nDrift after merge.\n",
            encoding="utf-8",
        )
        summary = self.broker(
            "get",
            "--source",
            "dependency:T000",
            "--section",
            "summary",
            "--question",
            "What completed?",
        )
        self.assertIn("SHORT OUTCOME", summary.stdout)
        context = self.broker(
            "get",
            "--source",
            "dependency:T000",
            "--section",
            "required_interfaces",
            "--question",
            "What interface is frozen?",
            check=False,
        )
        self.assertEqual(2, context.returncode)
        self.assertIn("differs from its frozen input", context.stderr)

    def test_bounded_text_never_exceeds_the_byte_limit(self) -> None:
        rendered, clipped = bounded_text("界".encode("utf-8"), 2)
        self.assertTrue(clipped)
        self.assertLessEqual(len(rendered.encode("utf-8")), 2)

    def test_full_prompt_embeds_only_manifest_and_compact_resume_does_not_repeat_it(self) -> None:
        status_path = self.task / "STATUS.json"
        self._write_json(
            status_path,
            {
                "task_id": "T001",
                "artifact_protocol_version": 2,
                "profile": "direct",
                "state": "pending",
            },
        )
        arguments = dict(
            worktree_path=str(self.root / "worktree"),
            task_dir=self.task,
            status_path=status_path,
            attempt_dir=self.attempt,
            worker_backend="claude-code",
            phase="execution",
        )
        full = render_worker_prompt(**arguments)
        compact = render_worker_prompt(**arguments, prompt_mode="compact_resume")
        self.assertIn("## Dependency Manifest (bounded)", full)
        self.assertIn("SHORT OUTCOME", full)
        self.assertNotIn("INTERFACE DETAIL: SupervisorResult", full)
        self.assertNotIn("## Dependency Manifest (bounded)", compact)


if __name__ == "__main__":
    unittest.main()
