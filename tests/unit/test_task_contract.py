#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from task_contract import (  # noqa: E402
    ImmutableArtifactError,
    TaskContractError,
    assert_resume_inputs_unchanged,
    build_task_inputs_from_readiness,
    build_task_inputs_payload,
    compare_task_inputs,
    evaluate_task_readiness,
    parse_acceptance_markdown,
    parse_context_markdown,
    parse_task_markdown,
    payload_sha256,
    resolve_dependencies,
    sha256_bytes,
    validate_execution_policy_v2,
    validate_task_inputs_payload,
    write_task_inputs_immutable,
)


COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def task_markdown(dependencies: list[dict[str, str]] | None = None) -> str:
    machine = json.dumps(
        {"schema_version": 2, "dependencies": dependencies or []},
        indent=2,
    )
    return f"""# Task T001

## Objective

Implement the bounded behavior.

## Deliverables

- `src/feature.py`

## Invariants

- Existing callers remain compatible.

## Non-goals

- No scheduler redesign.

## Dependencies

```json rdo-task-dependencies
{machine}
```
"""


def context_markdown() -> str:
    return """# Context

## Frozen Decisions

- The public interface is already settled.

## Required Interfaces

- `run(value)` returns a result.

## Local Code Map

- `src/feature.py` contains the implementation.

## Necessary Background

- This task follows the existing adapter pattern.
"""


def acceptance_markdown(
    *,
    required_commands: list[dict[str, object]] | None = None,
    required_outputs: list[str] | None = None,
    pre_merge_commands: list[dict[str, object]] | None = None,
    post_merge_commands: list[dict[str, object]] | None = None,
) -> str:
    command = {
        "id": "unit",
        "argv": ["python3", "-m", "unittest", "tests.unit.test_feature"],
        "cwd": ".",
        "timeout_seconds": 120,
    }
    machine = json.dumps(
        {
            "schema_version": 2,
            "required_commands": required_commands if required_commands is not None else [command],
            "required_outputs": required_outputs if required_outputs is not None else ["src/feature.py"],
            "pre_merge_commands": pre_merge_commands or [],
            "post_merge_commands": post_merge_commands or [],
        },
        indent=2,
    )
    return f"""# Acceptance

```json rdo-acceptance-contract
{machine}
```

## Behavioral Checks

- Invalid values are rejected.

## Merge Preconditions

- Review is complete.

## Blocked Conditions

- Required upstream behavior is unavailable.

## Pre-Merge Checks

- Run the structured required checks.

## Post-Merge Checks

- None.
"""


def policy(profile: str = "direct") -> dict[str, object]:
    return {
        "schema_version": 2,
        "strategy_required": profile == "full",
        "attempt_wall_seconds": 2700,
        "max_workflows": 6,
        "max_workflow_instances": 12,
        "max_parallel_workflows": 2,
        "max_subagents": 4,
        "max_parallel_subagents": 2,
        "default_command_seconds": 120,
        "max_enumerated_cases": 10000,
        "allow_unbounded_search": False,
        "allowed_paths": ["src"],
        "read_paths": ["src", "tests", "docs"],
        "forbidden_paths": ["private"],
        "context_sources": ["docs/DESIGN.md"],
    }


def source_bytes(*, changed_task: bool = False) -> dict[str, bytes]:
    task = task_markdown()
    if changed_task:
        task = task.replace("bounded behavior", "revised behavior")
    return {
        "TASK.md": task.encode(),
        "CONTEXT.md": context_markdown().encode(),
        "ACCEPTANCE.md": acceptance_markdown().encode(),
        "EXECUTION_POLICY.json": (json.dumps(policy(), indent=2) + "\n").encode(),
    }


class TaskMarkdownTests(unittest.TestCase):
    def test_parses_machine_dependencies(self) -> None:
        parsed = parse_task_markdown(
            task_markdown([{"task_id": "T000-base", "required_state": "merged"}]),
            task_id="T001",
        )
        self.assertEqual(
            parsed["dependencies"],
            [{"task_id": "T000-base", "required_state": "merged"}],
        )
        self.assertEqual(
            list(parsed["sections"]),
            ["Objective", "Deliverables", "Invariants", "Non-goals", "Dependencies"],
        )

    def test_rejects_missing_duplicate_and_extra_sections(self) -> None:
        valid = task_markdown()
        with self.assertRaisesRegex(TaskContractError, "missing required sections"):
            parse_task_markdown(valid.replace("## Invariants", "### Invariants"), task_id="T001")
        with self.assertRaisesRegex(TaskContractError, "duplicate section"):
            parse_task_markdown(valid + "\n## Objective\nAgain.\n", task_id="T001")
        with self.assertRaisesRegex(TaskContractError, "unsupported sections"):
            parse_task_markdown(valid + "\n## Profile\nDirect.\n", task_id="T001")

    def test_rejects_placeholders_and_legacy_controls(self) -> None:
        with self.assertRaisesRegex(TaskContractError, "template placeholder"):
            parse_task_markdown(task_markdown().replace("Implement", "{{GOAL}} Implement"), task_id="T001")
        with self.assertRaisesRegex(TaskContractError, "RDO_TEMPLATE_INCOMPLETE"):
            parse_task_markdown(
                task_markdown().replace("Implement", "RDO_TEMPLATE_INCOMPLETE: Implement"),
                task_id="T001",
            )
        with self.assertRaisesRegex(TaskContractError, "must not define profile"):
            parse_task_markdown(task_markdown().replace("Implement", "profile: direct\n\nImplement"), task_id="T001")

    def test_rejects_dependency_prose_invalid_state_duplicate_and_self_reference(self) -> None:
        with self.assertRaisesRegex(TaskContractError, "must contain only"):
            parse_task_markdown(
                task_markdown().replace(
                    "```json rdo-task-dependencies",
                    "Read this first.\n```json rdo-task-dependencies",
                ),
                task_id="T001",
            )
        with self.assertRaisesRegex(TaskContractError, "required_state must be 'merged'"):
            parse_task_markdown(
                task_markdown([{"task_id": "T002", "required_state": "verified"}]),
                task_id="T001",
            )
        with self.assertRaisesRegex(TaskContractError, "repeats dependency"):
            parse_task_markdown(
                task_markdown(
                    [
                        {"task_id": "T002", "required_state": "merged"},
                        {"task_id": "T002", "required_state": "merged"},
                    ]
                ),
                task_id="T001",
            )
        with self.assertRaisesRegex(TaskContractError, "must not reference the current task"):
            parse_task_markdown(
                task_markdown([{"task_id": "T001", "required_state": "merged"}]),
                task_id="T001",
            )


class ContextMarkdownTests(unittest.TestCase):
    def test_accepts_only_the_non_normative_capsule_sections(self) -> None:
        parsed = parse_context_markdown(context_markdown())
        self.assertEqual(
            list(parsed["sections"]),
            [
                "Frozen Decisions",
                "Required Interfaces",
                "Local Code Map",
                "Necessary Background",
            ],
        )

    def test_rejects_source_index_extra_sections_and_placeholders(self) -> None:
        with self.assertRaisesRegex(TaskContractError, "Source Index"):
            parse_context_markdown(context_markdown() + "\n## Source Index\n- `docs/X.md`\n")
        with self.assertRaisesRegex(TaskContractError, "unsupported sections"):
            parse_context_markdown(context_markdown() + "\n## Deliverables\n- More work.\n")
        with self.assertRaisesRegex(TaskContractError, "template placeholder"):
            parse_context_markdown(context_markdown().replace("adapter pattern", "path/to/file"))


class AcceptanceMarkdownTests(unittest.TestCase):
    def test_parses_stable_machine_contract(self) -> None:
        parsed = parse_acceptance_markdown(
            acceptance_markdown(
                pre_merge_commands=[
                    {
                        "id": "premerge",
                        "argv": ["python3", "scripts/premerge.py"],
                        "cwd": "tools/./checks",
                        "timeout_seconds": 30,
                    }
                ]
            )
        )
        contract = parsed["contract"]
        self.assertEqual(contract["required_commands"][0]["argv"][0], "python3")
        self.assertEqual(contract["pre_merge_commands"][0]["cwd"], "tools/checks")
        self.assertEqual(contract["required_outputs"], ["src/feature.py"])

    def test_rejects_missing_or_duplicate_machine_block(self) -> None:
        valid = acceptance_markdown()
        with self.assertRaisesRegex(TaskContractError, "must contain one"):
            parse_acceptance_markdown(valid.replace("rdo-acceptance-contract", "wrong-name"))
        machine_start = valid.index("```json rdo-acceptance-contract")
        machine_end = valid.index("```", machine_start + 3) + 3
        block = valid[machine_start:machine_end]
        with self.assertRaisesRegex(TaskContractError, "multiple"):
            parse_acceptance_markdown(valid + "\n" + block + "\n")

    def test_rejects_unexecutable_commands(self) -> None:
        cases = [
            ({"id": "unit", "argv": "pytest", "cwd": ".", "timeout_seconds": 1}, "argv"),
            ({"id": "Bad ID", "argv": ["pytest"], "cwd": ".", "timeout_seconds": 1}, "id"),
            ({"id": "unit", "argv": ["pytest"], "cwd": "/tmp", "timeout_seconds": 1}, "relative"),
            ({"id": "unit", "argv": ["pytest"], "cwd": "../tmp", "timeout_seconds": 1}, "traverse"),
            ({"id": "unit", "argv": ["pytest"], "cwd": ".", "timeout_seconds": True}, "positive integer"),
            ({"id": "unit", "argv": ["pytest"], "cwd": ".", "timeout_seconds": 0}, "positive integer"),
        ]
        for command, message in cases:
            with self.subTest(command=command), self.assertRaisesRegex(TaskContractError, message):
                parse_acceptance_markdown(acceptance_markdown(required_commands=[command]))

    def test_rejects_duplicate_command_ids_across_phases(self) -> None:
        duplicate = {
            "id": "unit",
            "argv": ["python3", "pre.py"],
            "cwd": ".",
            "timeout_seconds": 10,
        }
        with self.assertRaisesRegex(TaskContractError, "repeats command id"):
            parse_acceptance_markdown(
                acceptance_markdown(pre_merge_commands=[duplicate])
            )

    def test_rejects_missing_commands_and_invalid_outputs(self) -> None:
        with self.assertRaisesRegex(TaskContractError, "at least one required command"):
            parse_acceptance_markdown(acceptance_markdown(required_commands=[]))
        for output in ("/tmp/output", "../output", "."):
            with self.subTest(output=output), self.assertRaises(TaskContractError):
                parse_acceptance_markdown(acceptance_markdown(required_outputs=[output]))


class ExecutionPolicyTests(unittest.TestCase):
    def test_validates_profile_and_normalizes_paths(self) -> None:
        value = policy("delegated")
        value["allowed_paths"] = ["src/./feature"]
        value["read_paths"] = ["src", "tests/", "docs"]
        parsed = validate_execution_policy_v2(value, profile="delegated")
        self.assertEqual(parsed["allowed_paths"], ["src/feature"])
        self.assertEqual(parsed["read_paths"], ["src", "tests", "docs"])

    def test_strategy_required_is_bound_to_profile(self) -> None:
        for profile_name, wrong_value in (("direct", True), ("delegated", True), ("full", False)):
            value = policy(profile_name)
            value["strategy_required"] = wrong_value
            with self.subTest(profile=profile_name), self.assertRaisesRegex(
                TaskContractError, "strategy_required"
            ):
                validate_execution_policy_v2(value, profile=profile_name)

    def test_rejects_invalid_paths_and_boundary_conflicts(self) -> None:
        mutations = [
            ("allowed_paths", ["/src"], "relative"),
            ("read_paths", ["../src"], "traverse"),
            ("context_sources", ["C:\\design.md"], "relative"),
            ("allowed_paths", ["other"], "outside read_paths"),
            ("allowed_paths", ["private/key"], "overlaps forbidden"),
            ("read_paths", ["src", "private"], "inside forbidden"),
            ("context_sources", ["private/design.md"], "is forbidden"),
            ("context_sources", ["other/design.md"], "outside read_paths"),
        ]
        for field, replacement, message in mutations:
            value = policy()
            value[field] = replacement
            with self.subTest(field=field, replacement=replacement), self.assertRaisesRegex(
                TaskContractError, message
            ):
                validate_execution_policy_v2(value, profile="direct")

    def test_broad_read_scope_may_have_a_deterministic_forbidden_subtree(self) -> None:
        value = policy("direct")
        value["read_paths"] = ["."]
        value["forbidden_paths"] = ["private"]
        parsed = validate_execution_policy_v2(value, profile="direct")
        self.assertEqual(["."], parsed["read_paths"])
        self.assertEqual(["private"], parsed["forbidden_paths"])

    def test_optional_task_budget_is_normalized_and_invalid_limits_are_rejected(self) -> None:
        value = policy()
        value["task_budget"] = {
            "max_attempts": 3,
            "max_execution_seconds": 7200,
            "max_cost_usd": 4,
        }
        parsed = validate_execution_policy_v2(value, profile="direct")
        self.assertEqual(4.0, parsed["task_budget"]["max_cost_usd"])
        for invalid in ({}, {"max_attempts": 0}, {"max_cost_usd": -1}):
            value = policy()
            value["task_budget"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(TaskContractError):
                validate_execution_policy_v2(value, profile="direct")

    def test_rejects_schema_one_and_duplicate_paths(self) -> None:
        value = policy()
        value["schema_version"] = 1
        with self.assertRaisesRegex(TaskContractError, "schema_version must be 2"):
            validate_execution_policy_v2(value, profile="direct")
        value = policy()
        value["read_paths"] = ["src", "src/"]
        with self.assertRaisesRegex(TaskContractError, "duplicate path"):
            validate_execution_policy_v2(value, profile="direct")


class DependencyAndReadinessTests(unittest.TestCase):
    def test_dependency_resolution_requires_merged_exact_commit(self) -> None:
        dependencies = [{"task_id": "T000", "required_state": "merged"}]
        resolved = resolve_dependencies(
            dependencies,
            task_id="T001",
            resolver={"T000": {"state": "merged", "commit": COMMIT_A}},
        )
        self.assertEqual(resolved[0]["commit"], COMMIT_A)
        with self.assertRaisesRegex(TaskContractError, "requires state"):
            resolve_dependencies(
                dependencies,
                task_id="T001",
                resolver={"T000": {"state": "approved", "commit": COMMIT_A}},
            )
        with self.assertRaisesRegex(TaskContractError, "exact full commit"):
            resolve_dependencies(
                dependencies,
                task_id="T001",
                resolver={"T000": {"state": "merged", "commit": "abc1234"}},
            )

    def test_readiness_validates_all_four_files_and_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            (task_dir / "TASK.md").write_text(
                task_markdown([{"task_id": "T000", "required_state": "merged"}])
            )
            (task_dir / "CONTEXT.md").write_text(context_markdown())
            (task_dir / "ACCEPTANCE.md").write_text(acceptance_markdown())
            (task_dir / "EXECUTION_POLICY.json").write_text(json.dumps(policy()))
            result = evaluate_task_readiness(
                task_dir,
                task_id="T001",
                profile="direct",
                dependency_resolver={"T000": {"state": "merged", "commit": COMMIT_A}},
            )
            self.assertTrue(result.ready, result.errors)
            self.assertEqual(result.resolved_dependencies[0]["commit"], COMMIT_A)

            (task_dir / "CONTEXT.md").unlink()
            incomplete = evaluate_task_readiness(
                task_dir,
                task_id="T001",
                profile="direct",
                dependency_resolver={"T000": {"state": "pending", "commit": COMMIT_A}},
            )
            self.assertFalse(incomplete.ready)
            self.assertTrue(any("CONTEXT.md is missing" in error for error in incomplete.errors))
            self.assertTrue(any("requires state" in error for error in incomplete.errors))

    def test_readiness_rejects_canonical_symlink_and_missing_context_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "task"
            task_dir.mkdir()
            (task_dir / "TASK.md").write_text(task_markdown())
            external_context = root / "outside-context.md"
            external_context.write_text(context_markdown())
            (task_dir / "CONTEXT.md").symlink_to(external_context)
            (task_dir / "ACCEPTANCE.md").write_text(acceptance_markdown())
            (task_dir / "EXECUTION_POLICY.json").write_text(json.dumps(policy()))

            result = evaluate_task_readiness(
                task_dir,
                task_id="T001",
                profile="direct",
                context_root=root,
            )
            self.assertFalse(result.ready)
            self.assertTrue(any("CONTEXT.md" in error and "non-symlink" in error for error in result.errors))
            self.assertTrue(any("docs/DESIGN.md" in error and "must exist" in error for error in result.errors))

    def test_readiness_cross_checks_acceptance_paths_against_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "task"
            task_dir.mkdir()
            (task_dir / "TASK.md").write_text(task_markdown())
            (task_dir / "CONTEXT.md").write_text(context_markdown())
            command = {
                "id": "unit",
                "argv": ["true"],
                "cwd": "unreadable",
                "timeout_seconds": 10,
            }
            (task_dir / "ACCEPTANCE.md").write_text(
                acceptance_markdown(
                    required_commands=[command],
                    required_outputs=["docs/out.txt"],
                )
            )
            value = policy()
            value["context_sources"] = []
            (task_dir / "EXECUTION_POLICY.json").write_text(json.dumps(value))

            result = evaluate_task_readiness(
                task_dir,
                task_id="T001",
                profile="direct",
                context_root=root,
            )
            self.assertFalse(result.ready)
            self.assertTrue(any("required output 'docs/out.txt' is outside" in error for error in result.errors))
            self.assertTrue(any("cwd 'unreadable' is outside" in error for error in result.errors))


class TaskInputsTests(unittest.TestCase):
    def _payload(
        self,
        *,
        attempt_id: str = "A001",
        sources: dict[str, bytes] | None = None,
        base: str = COMMIT_A,
        dependency_commit: str | None = None,
        generated_at: str = "2026-07-15T00:00:00Z",
    ) -> dict[str, object]:
        dependencies = []
        if dependency_commit:
            dependencies = [
                {
                    "task_id": "T000",
                    "required_state": "merged",
                    "commit": dependency_commit,
                }
            ]
        return build_task_inputs_payload(
            task_id="T001",
            attempt_id=attempt_id,
            source_bytes=sources or source_bytes(),
            task_base_commit=base,
            resolved_dependencies=dependencies,
            generated_at=generated_at,
        )

    def test_stable_contract_excludes_attempt_identity_and_timestamp(self) -> None:
        first = self._payload(attempt_id="A001", generated_at="2026-07-15T00:00:00Z")
        second = self._payload(attempt_id="A002", generated_at="2026-07-16T00:00:00Z")
        self.assertEqual(first["contract_sha256"], second["contract_sha256"])
        self.assertTrue(compare_task_inputs(first, second)["matches"])
        self.assertEqual(first["inputs"]["task"]["ref"], "TASK.md")

    def test_digest_binds_raw_inputs_base_and_dependency_commits(self) -> None:
        first = self._payload(dependency_commit=COMMIT_A)
        changed_input = self._payload(sources=source_bytes(changed_task=True), dependency_commit=COMMIT_A)
        changed_base = self._payload(base=COMMIT_B, dependency_commit=COMMIT_A)
        changed_dependency = self._payload(dependency_commit=COMMIT_B)
        comparison = compare_task_inputs(first, changed_input)
        self.assertFalse(comparison["matches"])
        self.assertEqual(comparison["changed_inputs"], ["task"])
        self.assertTrue(compare_task_inputs(first, changed_base)["task_base_commit_changed"])
        self.assertTrue(
            compare_task_inputs(first, changed_dependency)["resolved_dependencies_changed"]
        )

    def test_validation_detects_tampered_contract_digest(self) -> None:
        value = self._payload()
        value["inputs"]["task"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(TaskContractError, "contract_sha256"):
            validate_task_inputs_payload(value)

    def test_ordinary_resume_drift_requires_revision_task(self) -> None:
        first = self._payload()
        unchanged = self._payload(attempt_id="A002")
        assert_resume_inputs_unchanged(first, unchanged)
        changed = self._payload(attempt_id="A002", sources=source_bytes(changed_task=True))
        with self.assertRaisesRegex(TaskContractError, "create a revision task"):
            assert_resume_inputs_unchanged(first, changed)

    def test_builds_only_from_ready_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            for filename, data in source_bytes().items():
                (task_dir / filename).write_bytes(data)
            readiness = evaluate_task_readiness(
                task_dir,
                task_id="T001",
                profile="direct",
            )
            payload = build_task_inputs_from_readiness(
                readiness,
                task_id="T001",
                attempt_id="A001",
                task_base_commit=COMMIT_A,
            )
            self.assertEqual(payload["task_id"], "T001")

            incomplete = copy.copy(readiness)
            object.__setattr__(incomplete, "ready", False)
            object.__setattr__(incomplete, "errors", ("not ready",))
            with self.assertRaisesRegex(TaskContractError, "not dispatch-ready"):
                build_task_inputs_from_readiness(
                    incomplete,
                    task_id="T001",
                    attempt_id="A002",
                    task_base_commit=COMMIT_A,
                )

    def test_immutable_publication_binds_exact_file_and_refuses_overwrite(self) -> None:
        value = self._payload()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts" / "A001" / "TASK_INPUTS.json"
            digest = write_task_inputs_immutable(path, value)
            self.assertEqual(digest, sha256_bytes(path.read_bytes()))
            original = path.read_bytes()
            with self.assertRaises(ImmutableArtifactError):
                write_task_inputs_immutable(path, self._payload(attempt_id="A002"))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
