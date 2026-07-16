from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import rdo
from artifact_bundle import artifact_binding, file_sha256, load_bundle, load_command_records
from strategy import DEFAULT_EXECUTION_POLICY
from task_contract import (
    build_task_inputs_from_readiness,
    evaluate_task_readiness,
    write_task_inputs_immutable,
)
from worktree_fingerprint import fingerprint
from protocol_cli import _validate_v2_handoff
from strategy import review_strategy, submit_strategy


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=cwd, text=True).strip()


class RdoArtifactV2Tests(unittest.TestCase):
    task_id = "T101-v2"
    attempt_id = "A001"

    def write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def task_markdown(self) -> str:
        return """# Task

## Objective

Implement and verify one committed result.

## Deliverables

- A committed result artifact.

## Invariants

- The task branch remains clean at handoff.

## Non-goals

- No unrelated repository changes.

## Dependencies

```json rdo-task-dependencies
{
  "schema_version": 2,
  "dependencies": []
}
```
"""

    def context_markdown(self) -> str:
        return """# Context

This file is non-normative.

## Frozen Decisions

Use the existing result format.

## Required Interfaces

The result is plain UTF-8 text.

## Local Code Map

`result.txt` is the task output.

## Necessary Background

No additional background is needed.
"""

    def acceptance_markdown(
        self,
        required_commands: list[dict[str, object]],
        *,
        required_outputs: list[str] | None = None,
        pre_merge_commands: list[dict[str, object]] | None = None,
        post_merge_commands: list[dict[str, object]] | None = None,
    ) -> str:
        contract = {
            "schema_version": 2,
            "required_commands": required_commands,
            "required_outputs": required_outputs or ["result.txt"],
            "pre_merge_commands": pre_merge_commands or [],
            "post_merge_commands": post_merge_commands or [],
        }
        return (
            "# Acceptance\n\n"
            "```json rdo-acceptance-contract\n"
            + json.dumps(contract, indent=2)
            + "\n```\n\n"
            "## Behavioral Checks\n\n"
            "- The committed result has the expected content.\n\n"
            "## Merge Preconditions\n\n"
            "- The exact handoff commit is reviewed.\n\n"
            "## Blocked Conditions\n\n"
            "- Block if the deterministic checks cannot run.\n\n"
            "## Pre-Merge Checks\n\n"
            "- Run the declared pre-merge commands.\n\n"
            "## Post-Merge Checks\n\n"
            "- Run the declared post-merge commands.\n"
        )

    def command(self, check_id: str, script: str = "print('checked')") -> dict[str, object]:
        return {
            "id": check_id,
            "argv": [sys.executable, "-c", script],
            "cwd": ".",
            "timeout_seconds": 5,
        }

    def make_fixture(
        self,
        *,
        profile: str = "direct",
        required_commands: list[dict[str, object]] | None = None,
        create_output: bool = True,
        required_outputs: list[str] | None = None,
        allowed_paths: list[str] | None = None,
        forbidden_paths: list[str] | None = None,
        pre_merge_commands: list[dict[str, object]] | None = None,
        post_merge_commands: list[dict[str, object]] | None = None,
    ) -> SimpleNamespace:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        git(root, "config", "user.email", "artifact-v2@example.com")
        git(root, "config", "user.name", "Artifact V2 Test")
        (root / "base.txt").write_text("base\n", encoding="utf-8")
        git(root, "add", "base.txt")
        git(root, "commit", "-m", "base")
        base_commit = git(root, "rev-parse", "HEAD")

        branch = f"agent/{self.task_id}"
        worktree = root / ".agent-worktrees" / self.task_id
        git(root, "branch", branch)
        subprocess.run(
            ["git", "worktree", "add", str(worktree), branch],
            cwd=root,
            check=True,
            capture_output=True,
        )

        def cleanup() -> None:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=root,
                check=False,
                capture_output=True,
            )
            temporary.cleanup()

        self.addCleanup(cleanup)
        before = fingerprint(worktree)
        (worktree / "change.txt").write_text("task change\n", encoding="utf-8")
        if create_output:
            (worktree / "result.txt").write_text("done\n", encoding="utf-8")
        git(worktree, "add", ".")
        git(worktree, "commit", "-m", "task result")
        source_commit = git(worktree, "rev-parse", "HEAD")

        run = root / ".agent-collab" / "runs" / "run-v2"
        task = run / "tasks" / self.task_id
        attempt = task / "attempts" / self.attempt_id
        (attempt / "runtime").mkdir(parents=True)
        (task / "reviews").mkdir(parents=True)
        (task / "logs").mkdir(parents=True)
        self.write_json(
            run / "RUN.json",
            {
                "protocol_version": "research-dev-orchestrator/v0.6",
                "run_id": "run-v2",
                "target_branch": "main",
            },
        )
        (run / "EVENTS.ndjson").write_text("", encoding="utf-8")

        commands = required_commands or [self.command("unit")]
        (task / "TASK.md").write_text(self.task_markdown(), encoding="utf-8")
        (task / "CONTEXT.md").write_text(self.context_markdown(), encoding="utf-8")
        (task / "ACCEPTANCE.md").write_text(
            self.acceptance_markdown(
                commands,
                required_outputs=required_outputs,
                pre_merge_commands=pre_merge_commands,
                post_merge_commands=post_merge_commands,
            ),
            encoding="utf-8",
        )
        policy = copy.deepcopy(DEFAULT_EXECUTION_POLICY)
        policy.update(
            schema_version=2,
            strategy_required=profile == "full",
            allowed_paths=allowed_paths or ["."],
            read_paths=["."],
            forbidden_paths=forbidden_paths or [],
            context_sources=[],
        )
        self.write_json(task / "EXECUTION_POLICY.json", policy)
        status = {
            "artifact_protocol_version": 2,
            "task_id": self.task_id,
            "profile": profile,
            "state": "running",
            "previous_state": "pending",
            "owner": "worker",
            "branch": branch,
            "worktree": str(worktree),
            "updated_at": "2026-07-15T00:00:00Z",
            "needs_coordinator": False,
            "summary": "",
            "blocking_reason": "",
            "blocker_type": "",
            "current_attempt_id": self.attempt_id,
            "assigned_worker": {"worker_id": "W001"},
            "evidence": {"commands_run": [], "logs": [], "passed": None},
            "state_history": [],
        }
        self.write_json(task / "STATUS.json", status)

        readiness = evaluate_task_readiness(
            task,
            task_id=self.task_id,
            profile=profile,
            dependency_resolver={},
        )
        self.assertTrue(readiness.ready, readiness.errors)
        task_inputs = build_task_inputs_from_readiness(
            readiness,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            task_base_commit=base_commit,
        )
        task_inputs_sha256 = write_task_inputs_immutable(
            attempt / "TASK_INPUTS.json",
            task_inputs,
        )
        self.write_json(
            attempt / "ATTEMPT.json",
            {
                "artifact_protocol_version": 2,
                "schema_version": 2,
                "task_id": self.task_id,
                "attempt_id": self.attempt_id,
                "state": "running",
                "phase": "execution",
                "task_inputs_ref": "TASK_INPUTS.json",
                "task_inputs_sha256": task_inputs_sha256,
                "runtime": {"cwd": str(worktree)},
            },
        )
        self.write_json(attempt / "runtime" / "worktree-before.json", before)
        dispatch_lock = task / ".dispatch-lock"
        dispatch_lock.mkdir()
        (dispatch_lock / "attempt_id").write_text(self.attempt_id + "\n", encoding="utf-8")
        (task / "LOCK").write_text(
            f"task_id: {self.task_id}\nattempt_id: {self.attempt_id}\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            root=root,
            run=run,
            task=task,
            attempt=attempt,
            worktree=worktree,
            branch=branch,
            base_commit=base_commit,
            source_commit=source_commit,
            commands=commands,
        )

    def check(self, fixture: SimpleNamespace, check_id: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return rdo.check_command(
                argparse.Namespace(
                    attempt_dir=str(fixture.attempt),
                    check_id=check_id,
                    workflow_id="",
                    instance_id="",
                )
            )

    def finalize(
        self,
        fixture: SimpleNamespace,
        state: str,
        **overrides: object,
    ) -> int:
        values: dict[str, object] = {
            "task_dir": "",
            "attempt_dir": str(fixture.attempt),
            "state": state,
            "summary": "Implemented and verified the frozen task contract.",
            "summary_file": "",
            "command": [],
            "file": [],
            "limitation": [],
            "self_review_passed": state == "verified",
            "self_review_summary": "Reviewed the final diff and structured check records.",
            "self_review_finding": [],
            "self_review_fix": [],
            "blocker_type": "",
            "blocking_reason": "",
            "auto_derive": True,
        }
        values.update(overrides)
        with contextlib.redirect_stdout(io.StringIO()):
            return rdo.handoff(argparse.Namespace(**values))

    def mark_handoff_completed(self, fixture: SimpleNamespace, state: str) -> None:
        status = json.loads((fixture.task / "STATUS.json").read_text(encoding="utf-8"))
        status.update(previous_state="running", state=state, owner="dispatch")
        self.write_json(fixture.task / "STATUS.json", status)
        attempt = json.loads((fixture.attempt / "ATTEMPT.json").read_text(encoding="utf-8"))
        attempt.update(
            state="completed",
            handoff_valid=True,
            handoff_state=state,
            verified_commit=(fixture.source_commit if state == "verified" else None),
        )
        self.write_json(fixture.attempt / "ATTEMPT.json", attempt)
        shutil.rmtree(fixture.task / ".dispatch-lock")
        (fixture.task / "LOCK").unlink()

    def review(self, fixture: SimpleNamespace, decision: str = "approved") -> int:
        findings = fixture.task / "reviews" / "findings.md"
        findings.write_text("# Findings\n\nNo unresolved findings.\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            return rdo.task_review(
                argparse.Namespace(
                    task_dir=str(fixture.task),
                    decision=decision,
                    reviewer="codex",
                    findings_file=str(findings),
                    note=[],
                )
            )

    def merge(self, fixture: SimpleNamespace, **overrides: object) -> int:
        values: dict[str, object] = {
            "task_dir": str(fixture.task),
            "target_worktree": str(fixture.root),
            "expected_commit": fixture.source_commit,
            "verify_command": [],
            "verification_timeout": 5,
            "coordinator": "codex",
        }
        values.update(overrides)
        with contextlib.redirect_stdout(io.StringIO()):
            return rdo.task_merge(argparse.Namespace(**values))

    def test_check_executes_only_a_frozen_id_and_writes_structured_record(self) -> None:
        fixture = self.make_fixture()
        with self.assertRaisesRegex(SystemExit, "unknown required acceptance check id"):
            self.check(fixture, "free-form-command")
        self.assertFalse((fixture.attempt / "runtime" / "COMMANDS.ndjson").exists())

        self.assertEqual(0, self.check(fixture, "unit"))
        records = load_command_records(fixture.attempt)
        self.assertEqual(1, len(records))
        record = records[0].payload
        definition = fixture.commands[0]
        self.assertEqual(definition["argv"], record["argv"])
        self.assertEqual(definition["cwd"], record["cwd"])
        self.assertEqual(definition["timeout_seconds"], record["timeout_seconds"])
        self.assertEqual("required_commands", record["category"])
        self.assertEqual("unit", record["check_id"])
        self.assertEqual(0, record["exit_code"])
        self.assertFalse(record["timed_out"])
        self.assertEqual([], record["surviving_processes"])
        self.assertEqual(
            json.loads((fixture.attempt / "TASK_INPUTS.json").read_text())["inputs"]["acceptance"]["sha256"],
            record["acceptance_contract_sha256"],
        )
        for prefix in ("stdout", "stderr"):
            log = fixture.attempt / record[f"{prefix}_ref"]
            self.assertTrue(log.is_file())
            self.assertEqual(file_sha256(log), record[f"{prefix}_sha256"])

    def test_finalize_requires_all_checks_required_outputs_and_a_clean_commit(self) -> None:
        checks = [self.command("unit"), self.command("smoke")]
        fixture = self.make_fixture(required_commands=checks)
        self.assertEqual(0, self.check(fixture, "unit"))
        with self.assertRaisesRegex(SystemExit, "required check 'smoke'"):
            self.finalize(fixture, "verified")
        self.assertEqual(0, self.check(fixture, "smoke"))
        (fixture.worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "committed and clean"):
            self.finalize(fixture, "verified")
        (fixture.worktree / "dirty.txt").unlink()

        missing = self.make_fixture(create_output=False)
        self.assertEqual(0, self.check(missing, "unit"))
        with self.assertRaisesRegex(SystemExit, "required outputs are missing"):
            self.finalize(missing, "verified")

    def test_direct_finalize_publishes_verified_self_review_and_digest_closure(self) -> None:
        fixture = self.make_fixture(profile="direct")
        self.assertEqual(0, self.check(fixture, "unit"))
        self.assertEqual(0, self.finalize(fixture, "verified"))

        bundle = load_bundle(
            fixture.attempt,
            expected_task_id=self.task_id,
            expected_attempt_id=self.attempt_id,
            expected_requested_state="verified",
            expected_source_commit=fixture.source_commit,
        )
        self.assertTrue(bundle.handoff["direct_self_review"]["performed"])
        self.assertTrue(bundle.handoff["direct_self_review"]["passed"])
        self.assertEqual("verified", bundle.ready["requested_state"])
        self.assertEqual(bundle.handoff_sha256, bundle.ready["handoff_sha256"])
        self.assertEqual(bundle.evidence_sha256, bundle.ready["evidence_sha256"])
        self.assertEqual(
            ["unit"],
            [record["check_id"] for record in bundle.evidence["command_records"]],
        )
        self.assertEqual(
            ["result.txt"],
            [item["path"] for item in bundle.evidence["required_outputs"]],
        )
        self.assertEqual(
            fixture.source_commit,
            git(fixture.worktree, "rev-parse", "HEAD"),
        )
        self.assertEqual("running", json.loads((fixture.task / "STATUS.json").read_text())["state"])
        with self.assertRaisesRegex(SystemExit, "handoff is already published"):
            self.check(fixture, "unit")

    def test_finalize_waits_for_inflight_check_before_freezing_evidence(self) -> None:
        coordination = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, coordination, True)
        started = coordination / "check-started"
        release = coordination / "check-release"
        script = (
            "import pathlib,time; "
            f"started=pathlib.Path({str(started)!r}); release=pathlib.Path({str(release)!r}); "
            "started.write_text('started'); "
            "\nwhile not release.exists(): time.sleep(0.01)"
        )
        fixture = self.make_fixture(required_commands=[self.command("unit", script)])

        check_result: list[object] = []
        finalize_result: list[object] = []

        def run_check() -> None:
            try:
                check_result.append(self.check(fixture, "unit"))
            except BaseException as exc:  # captured for the main test thread
                check_result.append(exc)

        def run_finalize() -> None:
            try:
                finalize_result.append(self.finalize(fixture, "verified"))
            except BaseException as exc:  # captured for the main test thread
                finalize_result.append(exc)

        check_thread = threading.Thread(target=run_check)
        check_thread.start()
        deadline = time.monotonic() + 2
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(started.exists(), "blocking check did not start")

        finalize_thread = threading.Thread(target=run_finalize)
        finalize_thread.start()
        time.sleep(0.1)
        self.assertTrue(finalize_thread.is_alive(), "finalize did not wait for the active check")
        release.write_text("release\n", encoding="utf-8")
        check_thread.join(3)
        finalize_thread.join(3)
        self.assertEqual([0], check_result)
        self.assertEqual([0], finalize_result)
        self.assertEqual(1, len(load_bundle(fixture.attempt).evidence["command_records"]))

    def test_profile_boundaries_and_free_text_evidence_are_rejected(self) -> None:
        direct = self.make_fixture(profile="direct")
        with self.assertRaisesRegex(SystemExit, "requires 'verified' finalization"):
            self.finalize(direct, "review")

        delegated = self.make_fixture(profile="delegated")
        with self.assertRaisesRegex(SystemExit, "requires 'review' finalization"):
            self.finalize(delegated, "verified")
        with self.assertRaisesRegex(SystemExit, "forbids free-text --command evidence"):
            self.finalize(delegated, "review", command=["pytest (109 passed)"])

        self.assertEqual(0, self.check(delegated, "unit"))
        self.assertEqual(0, self.finalize(delegated, "review"))
        bundle = load_bundle(delegated.attempt, expected_requested_state="review")
        self.assertFalse(bundle.handoff["direct_self_review"]["performed"])
        self.assertFalse(bundle.handoff["direct_self_review"]["passed"])

    def test_finalized_bundle_cannot_be_overwritten_and_ready_binds_exact_bytes(self) -> None:
        fixture = self.make_fixture()
        self.assertEqual(0, self.check(fixture, "unit"))
        self.assertEqual(0, self.finalize(fixture, "verified"))
        original = load_bundle(fixture.attempt)
        original_ready = (fixture.attempt / "runtime" / "HANDOFF_READY.json").read_bytes()
        with self.assertRaisesRegex(SystemExit, "immutable"):
            self.finalize(fixture, "verified", summary="A conflicting replacement summary.")
        current = load_bundle(fixture.attempt)
        self.assertEqual(original.handoff_sha256, current.handoff_sha256)
        self.assertEqual(original.evidence_sha256, current.evidence_sha256)
        self.assertEqual(
            original_ready,
            (fixture.attempt / "runtime" / "HANDOFF_READY.json").read_bytes(),
        )
        binding = artifact_binding(current)
        self.assertEqual(file_sha256(fixture.attempt / "HANDOFF.json"), binding["handoff_sha256"])
        self.assertEqual(file_sha256(fixture.attempt / "EVIDENCE.json"), binding["evidence_sha256"])
        self.assertEqual(
            file_sha256(fixture.attempt / "runtime" / "HANDOFF_READY.json"),
            binding["ready_sha256"],
        )

    def test_review_approval_and_merge_bind_the_exact_attempt_artifacts(self) -> None:
        merge_check = self.command(
            "pre-merge",
            "import pathlib; assert pathlib.Path('result.txt').read_text() == 'done\\n'",
        )
        post_check = self.command(
            "post-merge",
            "import pathlib; assert pathlib.Path('result.txt').read_text() == 'done\\n'",
        )
        fixture = self.make_fixture(
            profile="delegated",
            pre_merge_commands=[merge_check],
            post_merge_commands=[post_check],
        )
        self.assertEqual(0, self.check(fixture, "unit"))
        self.assertEqual(0, self.finalize(fixture, "review"))
        bundle = load_bundle(fixture.attempt)
        frozen_binding = artifact_binding(bundle)
        self.mark_handoff_completed(fixture, "review")

        self.assertEqual(0, self.review(fixture, "approved"))
        decision = json.loads(
            (fixture.task / "reviews" / "DECISION-v001.json").read_text(encoding="utf-8")
        )
        self.assertEqual(frozen_binding, decision["artifact_binding"])
        self.assertEqual(fixture.source_commit, decision["approved_commit"])
        with self.assertRaisesRegex(SystemExit, "forbids free --verify-command"):
            self.merge(fixture, verify_command=["pytest -q"])

        self.assertEqual(0, self.merge(fixture))
        self.assertEqual(fixture.source_commit, git(fixture.root, "rev-parse", "HEAD"))
        status = json.loads((fixture.task / "STATUS.json").read_text(encoding="utf-8"))
        self.assertEqual("merged", status["state"])
        merged_events = [
            json.loads(line)
            for line in (fixture.run / "EVENTS.ndjson").read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "task_merged"
        ]
        self.assertEqual(1, len(merged_events))
        self.assertEqual(frozen_binding, merged_events[0]["artifact_binding"])
        self.assertTrue(merged_events[0]["verification"]["passed"])
        self.assertEqual("pre-merge", merged_events[0]["verification"]["pre_merge"]["phase"])
        self.assertEqual("post-merge", merged_events[0]["verification"]["post_merge"]["phase"])

    def test_merge_rejects_artifact_bytes_changed_after_approval(self) -> None:
        fixture = self.make_fixture(profile="delegated")
        self.assertEqual(0, self.check(fixture, "unit"))
        self.assertEqual(0, self.finalize(fixture, "review"))
        self.mark_handoff_completed(fixture, "review")
        self.assertEqual(0, self.review(fixture, "approved"))

        evidence_path = fixture.attempt / "EVIDENCE.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["changed_paths"].append("tampered.txt")
        self.write_json(evidence_path, evidence)
        with self.assertRaisesRegex(SystemExit, "bundle is invalid|artifact binding is invalid"):
            self.merge(fixture)

    def test_post_merge_check_cannot_rewrite_target_head(self) -> None:
        destructive = self.command(
            "post-reset",
            "import subprocess; subprocess.run(['git','reset','--hard','HEAD^'], check=True)",
        )
        fixture = self.make_fixture(
            profile="delegated",
            post_merge_commands=[destructive],
        )
        self.assertEqual(0, self.check(fixture, "unit"))
        self.assertEqual(0, self.finalize(fixture, "review"))
        self.mark_handoff_completed(fixture, "review")
        self.assertEqual(0, self.review(fixture, "approved"))

        self.assertEqual(1, self.merge(fixture))
        status = json.loads((fixture.task / "STATUS.json").read_text())
        self.assertEqual("approved", status["state"])
        merged_events = [
            json.loads(line)
            for line in (fixture.run / "EVENTS.ndjson").read_text().splitlines()
            if json.loads(line).get("event") == "task_merged"
        ]
        self.assertEqual([], merged_events)
        self.assertFalse(
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", fixture.source_commit, "HEAD"],
                cwd=fixture.root,
                check=False,
            ).returncode
            == 0
        )

    def test_merge_requires_review_pointer_decision_digest(self) -> None:
        fixture = self.make_fixture(profile="delegated")
        self.assertEqual(0, self.check(fixture, "unit"))
        self.assertEqual(0, self.finalize(fixture, "review"))
        self.mark_handoff_completed(fixture, "review")
        self.assertEqual(0, self.review(fixture, "approved"))
        pointer_path = fixture.task / "reviews" / "CURRENT_TASK_REVIEW.json"
        pointer = json.loads(pointer_path.read_text())
        pointer.pop("decision_sha256")
        self.write_json(pointer_path, pointer)
        with self.assertRaisesRegex(SystemExit, "requires a lowercase decision_sha256"):
            self.merge(fixture)

    def test_merged_replay_does_not_require_the_source_worktree(self) -> None:
        fixture = self.make_fixture(profile="delegated")
        self.assertEqual(0, self.check(fixture, "unit"))
        self.assertEqual(0, self.finalize(fixture, "review"))
        self.mark_handoff_completed(fixture, "review")
        self.assertEqual(0, self.review(fixture, "approved"))
        self.assertEqual(0, self.merge(fixture))
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(fixture.worktree)],
            cwd=fixture.root,
            check=True,
            capture_output=True,
        )
        self.assertEqual(0, self.merge(fixture))

    def test_merged_status_recovers_missing_event_and_interrupted_tail(self) -> None:
        fixture = self.make_fixture(profile="delegated")
        self.assertEqual(0, self.check(fixture, "unit"))
        self.assertEqual(0, self.finalize(fixture, "review"))
        self.mark_handoff_completed(fixture, "review")
        self.assertEqual(0, self.review(fixture, "approved"))
        git(fixture.root, "merge", "--ff-only", fixture.source_commit)
        rdo.transition(fixture.task, "merged", "coordinator")
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(fixture.worktree)],
            cwd=fixture.root,
            check=True,
            capture_output=True,
        )
        with (fixture.run / "EVENTS.ndjson").open("ab") as handle:
            handle.write(b'{"event":"task_merged","task_id":')

        self.assertEqual(1, self.merge(fixture))
        events = [
            json.loads(line)
            for line in (fixture.run / "EVENTS.ndjson").read_text().splitlines()
            if line.strip()
        ]
        merged = [item for item in events if item.get("event") == "task_merged"]
        self.assertEqual(1, len(merged))
        self.assertFalse(merged[0]["verification"]["passed"])
        self.assertTrue(merged[0]["verification"]["recovered"])
        quarantined = list(
            (fixture.run / "diagnostics").glob("EVENTS-interrupted-tail-*.json")
        )
        self.assertEqual(1, len(quarantined))

    def test_task_review_rejects_worker_publication_before_dispatch_acceptance(self) -> None:
        fixture = self.make_fixture(profile="delegated")
        self.assertEqual(0, self.check(fixture, "unit"))
        self.assertEqual(0, self.finalize(fixture, "review"))
        status = json.loads((fixture.task / "STATUS.json").read_text())
        status.update(previous_state="running", state="review", owner="worker")
        self.write_json(fixture.task / "STATUS.json", status)

        with self.assertRaisesRegex(SystemExit, "dispatch lock exists"):
            self.review(fixture, "approved")
        shutil.rmtree(fixture.task / ".dispatch-lock")
        with self.assertRaisesRegex(SystemExit, "completed, valid dispatcher handoff"):
            self.review(fixture, "approved")
        self.assertFalse(any((fixture.task / "reviews").glob("DECISION-v*.json")))

    def test_event_reader_rejects_malformed_complete_record(self) -> None:
        fixture = self.make_fixture()
        (fixture.run / "EVENTS.ndjson").write_bytes(b'{"event":\n')
        with self.assertRaisesRegex(SystemExit, "line 1 is malformed"):
            rdo.existing_task_merged_event(fixture.task)

    def test_finalize_rejects_required_output_not_tracked_by_source_commit(self) -> None:
        fixture = self.make_fixture(required_outputs=["ignored.out"])
        (fixture.worktree / ".gitignore").write_text("ignored.out\n", encoding="utf-8")
        (fixture.worktree / "ignored.out").write_text("ephemeral\n", encoding="utf-8")
        git(fixture.worktree, "add", ".gitignore")
        git(fixture.worktree, "commit", "-m", "declare ignored output")
        fixture.source_commit = git(fixture.worktree, "rev-parse", "HEAD")
        self.assertEqual(0, self.check(fixture, "unit"))
        with self.assertRaisesRegex(SystemExit, "not bound to source_commit"):
            self.finalize(fixture, "verified")

    def test_finalize_rejects_mode_only_change_outside_frozen_write_policy(self) -> None:
        fixture = self.make_fixture(
            allowed_paths=["change.txt", "result.txt"],
            forbidden_paths=["base.txt"],
        )
        base = fixture.worktree / "base.txt"
        base.chmod(base.stat().st_mode | 0o111)
        git(fixture.worktree, "add", "base.txt")
        git(fixture.worktree, "commit", "-m", "forbidden mode change")
        fixture.source_commit = git(fixture.worktree, "rev-parse", "HEAD")
        self.assertEqual(0, self.check(fixture, "unit"))
        with self.assertRaisesRegex(SystemExit, "violates the frozen write policy"):
            self.finalize(fixture, "verified")

    def test_worktree_snapshots_detect_mode_only_changes(self) -> None:
        fixture = self.make_fixture()
        before = fixture.attempt / "runtime" / "mode-before.json"
        after = fixture.attempt / "runtime" / "mode-after.json"
        self.write_json(before, fingerprint(fixture.worktree))
        result = fixture.worktree / "result.txt"
        result.chmod(result.stat().st_mode | 0o111)
        self.write_json(after, fingerprint(fixture.worktree))
        self.assertEqual(
            json.loads(before.read_text())["sha256"],
            json.loads(after.read_text())["sha256"],
        )
        self.assertEqual(["result.txt"], rdo._snapshot_changed_paths(before, after))

    def test_non_full_task_cannot_create_or_review_strategy_artifacts(self) -> None:
        from tests.unit.test_strategy import strategy_payload

        fixture = self.make_fixture(profile="delegated")
        candidate = fixture.attempt / "strategy.json"
        self.write_json(candidate, strategy_payload(self.task_id))
        with self.assertRaisesRegex(SystemExit, "requires profile='full'"):
            rdo.strategy_submit(
                argparse.Namespace(
                    task_dir=str(fixture.task),
                    file=str(candidate),
                    strategy_action="submit",
                )
            )
        self.assertFalse(fixture.task.joinpath("strategy", "STRATEGY-v001.json").exists())

        status = json.loads((fixture.task / "STATUS.json").read_text(encoding="utf-8"))
        status["state"] = "strategy_review"
        self.write_json(fixture.task / "STATUS.json", status)
        shutil.rmtree(fixture.task / ".dispatch-lock")
        with self.assertRaisesRegex(SystemExit, "requires profile='full'"):
            rdo.strategy_review(
                argparse.Namespace(
                    task_dir=str(fixture.task),
                    revision=1,
                    strategy_action="approve",
                    reviewer="coordinator",
                    note=[],
                )
            )

    def test_full_execution_attempt_can_pause_for_strategy_revision(self) -> None:
        from tests.unit.test_strategy import strategy_payload

        fixture = self.make_fixture(profile="full")
        (fixture.task / "strategy").mkdir()
        first = strategy_payload(self.task_id)
        first["workflows"][0]["executor"]["allowed_paths"] = ["."]
        submit_strategy(fixture.task, first)
        first_review = review_strategy(
            fixture.task,
            1,
            decision="approved",
            reviewer="coordinator",
        )
        attempt_metadata = json.loads((fixture.attempt / "ATTEMPT.json").read_text())
        attempt_metadata.update(
            strategy_id=first["strategy_id"],
            strategy_revision=1,
            strategy_sha256=first_review["strategy_sha256"],
        )
        self.write_json(fixture.attempt / "ATTEMPT.json", attempt_metadata)
        self.write_json(
            fixture.attempt / "runtime" / "BACKEND_PROFILE.json",
            {
                "strategy_id": first["strategy_id"],
                "strategy_revision": 1,
                "strategy_sha256": first_review["strategy_sha256"],
            },
        )
        second = strategy_payload(self.task_id, 2, first["strategy_id"])
        second["workflows"][0]["executor"]["allowed_paths"] = ["."]
        candidate = fixture.attempt / "strategy-v2.json"
        self.write_json(candidate, second)
        status = json.loads((fixture.task / "STATUS.json").read_text())
        status["state_history"] = [
            {
                "from": "pending",
                "to": "running",
                "actor": "dispatch",
                "at": "2026-07-15T00:00:00Z",
            }
        ]
        self.write_json(fixture.task / "STATUS.json", status)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                0,
                rdo.strategy_submit(
                    argparse.Namespace(
                        task_dir=str(fixture.task),
                        file=str(candidate),
                        strategy_action="revise",
                    )
                ),
            )
        bundle = load_bundle(
            fixture.attempt,
            expected_requested_state="strategy_review",
            expected_source_commit=fixture.source_commit,
        )
        self.assertEqual(["change.txt", "result.txt"], bundle.evidence["changed_paths"])
        validation = _validate_v2_handoff(
            json.loads((fixture.task / "STATUS.json").read_text()),
            task_dir=fixture.task,
            attempt_id=self.attempt_id,
            exit_code_raw="0",
            worktree=fixture.worktree,
        )
        self.assertTrue(validation.valid, validation.reasons)

    def test_strategy_review_rejects_publication_before_dispatch_acceptance(self) -> None:
        from tests.unit.test_strategy import strategy_payload

        fixture = self.make_fixture(profile="full")
        (fixture.task / "strategy").mkdir()
        first = strategy_payload(self.task_id)
        first["workflows"][0]["executor"]["allowed_paths"] = ["."]
        submit_strategy(fixture.task, first)
        first_review = review_strategy(
            fixture.task,
            1,
            decision="approved",
            reviewer="coordinator",
        )
        attempt_metadata = json.loads((fixture.attempt / "ATTEMPT.json").read_text())
        attempt_metadata.update(
            strategy_id=first["strategy_id"],
            strategy_revision=1,
            strategy_sha256=first_review["strategy_sha256"],
        )
        self.write_json(fixture.attempt / "ATTEMPT.json", attempt_metadata)
        self.write_json(
            fixture.attempt / "runtime" / "BACKEND_PROFILE.json",
            {
                "strategy_id": first["strategy_id"],
                "strategy_revision": 1,
                "strategy_sha256": first_review["strategy_sha256"],
            },
        )
        second = strategy_payload(self.task_id, 2, first["strategy_id"])
        second["workflows"][0]["executor"]["allowed_paths"] = ["."]
        candidate = fixture.attempt / "strategy-v2.json"
        self.write_json(candidate, second)
        status = json.loads((fixture.task / "STATUS.json").read_text())
        status["state_history"] = [
            {
                "from": "pending",
                "to": "running",
                "actor": "dispatch",
                "at": "2026-07-15T00:00:00Z",
            }
        ]
        self.write_json(fixture.task / "STATUS.json", status)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                0,
                rdo.strategy_submit(
                    argparse.Namespace(
                        task_dir=str(fixture.task),
                        file=str(candidate),
                        strategy_action="revise",
                    )
                ),
            )
        status = json.loads((fixture.task / "STATUS.json").read_text())
        status.update(previous_state="running", state="strategy_review", owner="worker")
        self.write_json(fixture.task / "STATUS.json", status)
        shutil.rmtree(fixture.task / ".dispatch-lock")

        with self.assertRaisesRegex(SystemExit, "completed, valid dispatcher handoff"):
            rdo.strategy_review(
                argparse.Namespace(
                    task_dir=str(fixture.task),
                    revision=2,
                    strategy_action="approve",
                    reviewer="coordinator",
                    note=[],
                )
            )
        self.assertFalse((fixture.task / "strategy" / "REVIEW-v002.json").exists())


if __name__ == "__main__":
    unittest.main()
