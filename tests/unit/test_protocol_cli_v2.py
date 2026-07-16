#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from artifact_bundle import (  # noqa: E402
    artifact_binding,
    build_required_output_bindings,
    command_record_sha256,
    publish_bundle,
)
from protocol_cli import cmd_validate_handoff  # noqa: E402
from strategy import canonical_digest, review_strategy, submit_strategy  # noqa: E402
from supervisor import load_or_create_attempt_deadline  # noqa: E402
from worktree_fingerprint import fingerprint  # noqa: E402


def run(*argv: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def task_text(objective: str = "Implement the feature.") -> str:
    return f"""# Task

## Objective

{objective}

## Deliverables

- `file.txt`

## Invariants

- Existing behavior remains valid.

## Non-goals

- No unrelated changes.

## Dependencies

```json rdo-task-dependencies
{{
  "schema_version": 2,
  "dependencies": []
}}
```
"""


CONTEXT = """# Context

## Frozen Decisions

- Keep the existing public interface.

## Required Interfaces

- `file.txt` remains readable.

## Local Code Map

- `file.txt` is the implementation fixture.

## Necessary Background

- None.
"""


ACCEPTANCE = """# Acceptance

```json rdo-acceptance-contract
{
  "schema_version": 2,
  "required_commands": [
    {
      "id": "unit",
      "argv": ["true"],
      "cwd": ".",
      "timeout_seconds": 10
    }
  ],
  "required_outputs": ["file.txt"],
  "pre_merge_commands": [],
  "post_merge_commands": []
}
```

## Behavioral Checks

- The file remains readable.

## Merge Preconditions

- Structured checks pass.

## Blocked Conditions

- The repository is unavailable.

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
    "max_subagents": 4,
    "max_parallel_subagents": 2,
    "default_command_seconds": 30,
    "max_enumerated_cases": 100,
    "allow_unbounded_search": False,
    "allowed_paths": ["file.txt"],
    "read_paths": ["file.txt"],
    "forbidden_paths": [],
    "context_sources": [],
}


class ProtocolCliV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        run("git", "init", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "test@example.com", cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=self.root)
        (self.root / ".git" / "info" / "exclude").write_text(
            ".agent-collab/\n.worktrees/\n",
            encoding="utf-8",
        )
        (self.root / "file.txt").write_text("initial\n", encoding="utf-8")
        run("git", "add", "file.txt", cwd=self.root)
        run("git", "commit", "-m", "initial", cwd=self.root)
        self.base = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()

        self.run_dir = self.root / ".agent-collab" / "runs" / "R001"
        self.task_dir = self.run_dir / "tasks" / "T001"
        (self.task_dir / "attempts").mkdir(parents=True)
        (self.task_dir / "strategy").mkdir()
        self.write_json(
            self.run_dir / "RUN.json",
            {
                "run_id": "R001",
                "protocol_version": "research-dev-orchestrator/v0.6",
                "target_branch": "main",
                "base_commit": self.base,
            },
        )
        (self.run_dir / "EVENTS.ndjson").write_text(
            json.dumps(
                {
                    "at": "2026-07-15T00:00:00Z",
                    "actor": "coordinator",
                    "event": "task_created",
                    "run_id": "R001",
                    "task_id": "T001",
                    "profile": "direct",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.status = {
            "task_id": "T001",
            "artifact_protocol_version": 2,
            "profile": "direct",
            "state": "pending",
            "previous_state": None,
            "owner": "",
            "branch": "main",
            "worktree": ".worktrees/T001",
            "updated_at": "2026-07-15T00:00:00Z",
            "needs_coordinator": False,
            "summary": "",
            "blocking_reason": "",
            "blocker_type": "",
            "current_attempt_id": None,
            "assigned_worker": None,
            "evidence": {"commands_run": [], "logs": [], "passed": None},
            "state_history": [],
        }
        self.write_json(self.task_dir / "STATUS.json", self.status)
        self.write_valid_inputs()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def write_valid_inputs(self) -> None:
        (self.task_dir / "TASK.md").write_text(task_text(), encoding="utf-8")
        (self.task_dir / "CONTEXT.md").write_text(CONTEXT, encoding="utf-8")
        (self.task_dir / "ACCEPTANCE.md").write_text(ACCEPTANCE, encoding="utf-8")
        self.write_json(self.task_dir / "EXECUTION_POLICY.json", POLICY)

    def append_event(self, payload: dict[str, object]) -> None:
        record = {
            "at": "2026-07-15T00:00:00Z",
            "actor": "coordinator",
            "run_id": "R001",
            **payload,
        }
        with (self.run_dir / "EVENTS.ndjson").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def set_task_profile(self, profile: str) -> None:
        status = json.loads((self.task_dir / "STATUS.json").read_text(encoding="utf-8"))
        status["profile"] = profile
        self.write_json(self.task_dir / "STATUS.json", status)
        policy = dict(POLICY)
        policy["strategy_required"] = profile == "full"
        self.write_json(self.task_dir / "EXECUTION_POLICY.json", policy)
        events = [
            json.loads(line)
            for line in (self.run_dir / "EVENTS.ndjson").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for event in events:
            if event.get("event") == "task_created" and event.get("task_id") == "T001":
                event["profile"] = profile
        (self.run_dir / "EVENTS.ndjson").write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run(
            sys.executable,
            str(SCRIPTS / "protocol_cli.py"),
            *args,
            cwd=self.root,
            check=check,
        )

    def freeze(self, attempt_id: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.cli(
            "freeze-task-inputs",
            "--task-dir",
            str(self.task_dir),
            "--run-dir",
            str(self.run_dir),
            "--repo-root",
            str(self.root),
            "--task-id",
            "T001",
            "--attempt-id",
            attempt_id,
            "--attempt-dir",
            str(self.task_dir / "attempts" / attempt_id),
            "--profile",
            "direct",
            "--execution-mode",
            "resume" if attempt_id != "A001" else "start",
            check=check,
        )

    def create_attempt(self, attempt_id: str, inputs_digest: str) -> Path:
        attempt = self.task_dir / "attempts" / attempt_id
        self.cli(
            "create-attempt",
            "--path",
            str(attempt / "ATTEMPT.json"),
            "--artifact-protocol-version",
            "2",
            "--task-inputs-ref",
            "TASK_INPUTS.json",
            "--task-inputs-sha256",
            inputs_digest,
            "--attempt-id",
            attempt_id,
            "--task-id",
            "T001",
            "--agent-name",
            "worker",
            "--worker-id",
            "W001",
            "--phase",
            "execution",
            "--command",
            "true",
            "--cwd",
            str(self.root),
            "--backend",
            "plain",
        )
        return attempt

    @staticmethod
    def sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def source_entries(self) -> list[dict[str, object]]:
        return [
            {
                "path": item["path"],
                "kind": item["kind"],
                "mode": item["mode"],
                "sha256": item["sha256"],
            }
            for item in fingerprint(self.root)["entries"]
            if item.get("kind") != "missing"
        ]

    def prepare_finalization(
        self,
        attempt: Path,
        *,
        task_inputs_sha256: str,
    ) -> tuple[str, float]:
        runtime = attempt / "runtime"
        deadline_path = runtime / "DEADLINE.json"
        deadline = load_or_create_attempt_deadline(
            deadline_path,
            attempt_timeout_seconds=120,
            finalization_grace_seconds=90,
            reminder_seconds=30,
        )
        entries = self.source_entries()
        snapshot_path = runtime / "finalization-worktree.json"
        self.write_json(
            snapshot_path,
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_id": "T001",
                "attempt_id": attempt.name,
                "entries_sha256": canonical_digest(entries),
                "file_count": len(entries),
                "entries": entries,
            },
        )
        started_at_epoch = time.time()
        marker_path = runtime / "FINALIZATION.json"
        self.write_json(
            marker_path,
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "stage": "finalizing",
                "task_id": "T001",
                "attempt_id": attempt.name,
                "task_inputs_sha256": task_inputs_sha256,
                "started_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(started_at_epoch),
                ),
                "started_at_epoch": started_at_epoch,
                "grace_seconds": deadline["finalization_grace_seconds"],
                "deadline_at_epoch": (
                    deadline["execution_deadline_at_epoch"]
                    + deadline["finalization_grace_seconds"]
                ),
                "source_snapshot_ref": "runtime/finalization-worktree.json",
                "source_snapshot_sha256": self.sha256(snapshot_path),
                "deadline_ref": "runtime/DEADLINE.json",
                "deadline_sha256": self.sha256(deadline_path),
            },
        )
        return canonical_digest(entries), started_at_epoch

    def write_supervisor_receipt(
        self,
        attempt: Path,
        *,
        finalization_started: bool,
    ) -> Path:
        ready_path = attempt / "runtime" / "HANDOFF_READY.json"
        deadline_path = attempt / "runtime" / "DEADLINE.json"
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        deadline = json.loads(deadline_path.read_text(encoding="utf-8"))
        metadata = ready_path.stat(follow_symlinks=False)
        source_commit = ready.get("source_commit")
        active_deadline = deadline["execution_deadline_at_epoch"] + (
            deadline["finalization_grace_seconds"] if finalization_started else 0
        )
        supervisor_path = attempt / "supervisor-result.json"
        self.write_json(
            supervisor_path,
            {
                "exit_code": 0,
                "timed_out": False,
                "timeout_phase": None,
                "artifact_protocol_version": 2,
                "publication_requested": True,
                "completion_requested": True,
                "publication_invalidated": False,
                "publication_unaccepted": False,
                "late_publication": False,
                "accepted_publication_sha256": self.sha256(ready_path),
                "accepted_publication_receipt": {
                    "sha256": self.sha256(ready_path),
                    "ctime": metadata.st_ctime,
                    "ctime_ns": metadata.st_ctime_ns,
                    "mtime_ns": metadata.st_mtime_ns,
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "size": metadata.st_size,
                    "observed_at_epoch": time.time(),
                    "source": {"source_commit": source_commit},
                },
                "final_source": {
                    "source_commit": source_commit,
                    "worktree_clean": True,
                },
                "finalization_started": finalization_started,
                "finalization_timed_out": False,
                "publication": {"valid": True, "reasons": []},
                "surviving_pids": [],
                "cleanup_verified": True,
                "cleanup_failure_reason": None,
                "deadline_sha256": self.sha256(deadline_path),
                "attempt_started_at_epoch": deadline["started_at_epoch"],
                "execution_deadline_at_epoch": deadline[
                    "execution_deadline_at_epoch"
                ],
                "active_deadline_at_epoch": active_deadline,
            },
        )
        return supervisor_path

    def validation_args(
        self,
        attempt: Path,
        **overrides: object,
    ) -> argparse.Namespace:
        values: dict[str, object] = {
            "attempt_path": str(attempt / "ATTEMPT.json"),
            "task_dir": str(self.task_dir),
            "status_path": str(self.task_dir / "STATUS.json"),
            "attempt_id": attempt.name,
            "exit_code_raw": "0",
            "startup_path": "",
            "supervisor_result": str(attempt / "supervisor-result.json"),
            "worktree": str(self.root),
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_readiness_failure_is_read_only(self) -> None:
        (self.task_dir / "CONTEXT.md").write_text(
            CONTEXT.replace("Keep the existing", "RDO_TEMPLATE_INCOMPLETE: Keep the existing"),
            encoding="utf-8",
        )
        result = self.cli(
            "check-task-readiness",
            "--task-dir",
            str(self.task_dir),
            "--run-dir",
            str(self.run_dir),
            "--task-id",
            "T001",
            "--profile",
            "direct",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RDO_TEMPLATE_INCOMPLETE", result.stderr)

        dispatch = subprocess.run(
            [
                str(SCRIPTS / "dispatch_agent.sh"),
                "R001",
                "T001",
                "--worker",
                "claude-code",
                "--runtime",
                "plain",
                "--io",
                "machine",
                "--command",
                "true",
            ],
            cwd=self.root,
            env={
                **os.environ,
                "DISPATCH_DRY_RUN": "1",
                "RDO_TEST_ALLOW_UNGOVERNED_COMMAND_OVERRIDE": "1",
            },
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(dispatch.returncode, 0)
        self.assertIn("RDO_TEMPLATE_INCOMPLETE", dispatch.stderr)
        self.assertEqual(list((self.task_dir / "attempts").iterdir()), [])
        self.assertFalse((self.task_dir / ".dispatch-lock").exists())
        self.assertEqual(json.loads((self.task_dir / "STATUS.json").read_text())["state"], "pending")
        branch = run(
            "git",
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/agent/T001",
            cwd=self.root,
            check=False,
        )
        self.assertNotEqual(branch.returncode, 0)

    def test_current_run_requires_explicit_task_protocol_discriminator(self) -> None:
        status_path = self.task_dir / "STATUS.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status.pop("artifact_protocol_version")
        self.write_json(status_path, status)

        result = self.cli(
            "task-protocol-version",
            "--task-dir",
            str(self.task_dir),
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unknown artifact protocol version", result.stderr)

    def test_readiness_requires_the_explicit_status_profile_before_mutation(self) -> None:
        mismatch = self.cli(
            "check-task-readiness",
            "--task-dir",
            str(self.task_dir),
            "--run-dir",
            str(self.run_dir),
            "--task-id",
            "T001",
            "--profile",
            "delegated",
            check=False,
        )
        self.assertNotEqual(0, mismatch.returncode)
        self.assertIn("does not match explicit STATUS.profile 'direct'", mismatch.stderr)
        self.assertEqual([], list((self.task_dir / "attempts").iterdir()))

        status = json.loads((self.task_dir / "STATUS.json").read_text(encoding="utf-8"))
        status["profile"] = "full"
        self.write_json(self.task_dir / "STATUS.json", status)
        policy = dict(POLICY)
        policy["strategy_required"] = True
        self.write_json(self.task_dir / "EXECUTION_POLICY.json", policy)
        dispatch = subprocess.run(
            [
                str(SCRIPTS / "dispatch_agent.sh"),
                "R001",
                "T001",
                "--worker",
                "claude-code",
                "--runtime",
                "plain",
                "--io",
                "machine",
                "--command",
                "true",
            ],
            cwd=self.root,
            env={
                **os.environ,
                "DISPATCH_DRY_RUN": "1",
                "RDO_TEST_ALLOW_UNGOVERNED_COMMAND_OVERRIDE": "1",
            },
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(0, dispatch.returncode)
        self.assertIn(
            "STATUS.profile 'full' does not match task_created profile 'direct'",
            dispatch.stderr,
        )
        self.assertEqual([], list((self.task_dir / "attempts").iterdir()))
        self.assertFalse((self.task_dir / ".dispatch-lock").exists())

    def test_freeze_and_attempt_bind_exact_task_inputs(self) -> None:
        result = self.freeze("A001")
        frozen = json.loads(result.stdout)
        inputs_path = self.task_dir / "attempts" / "A001" / "TASK_INPUTS.json"
        self.assertEqual(frozen["task_base_commit"], self.base)
        self.assertEqual(frozen["sha256"], hashlib.sha256(inputs_path.read_bytes()).hexdigest())
        attempt_dir = self.create_attempt("A001", frozen["sha256"])
        attempt = json.loads((attempt_dir / "ATTEMPT.json").read_text())
        self.assertEqual(attempt["artifact_protocol_version"], 2)
        self.assertEqual(attempt["task_inputs_ref"], "TASK_INPUTS.json")
        self.assertEqual(attempt["task_inputs_sha256"], frozen["sha256"])
        self.assertNotIn("contract_sha256", attempt)
        self.assertNotIn("inputs", attempt)

    def test_dispatch_passes_v2_publication_identity_and_uses_runtime_transcript(self) -> None:
        dispatch = subprocess.run(
            [
                str(SCRIPTS / "dispatch_agent.sh"),
                "R001",
                "T001",
                "--worker",
                "claude-code",
                "--runtime",
                "plain",
                "--io",
                "machine",
                "--command",
                "true",
            ],
            cwd=self.root,
            env={
                **os.environ,
                "DISPATCH_DRY_RUN": "1",
                "RDO_TEST_ALLOW_UNGOVERNED_COMMAND_OVERRIDE": "1",
            },
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Dry-run workers intentionally do not publish a handoff, so terminal
        # validation blocks them after creating auditable dispatch artifacts.
        self.assertEqual(dispatch.returncode, 4, dispatch.stderr)
        attempts = list((self.task_dir / "attempts").iterdir())
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        metadata = json.loads((attempt / "ATTEMPT.json").read_text())
        supervisor = metadata["runtime"]["supervisor_command"]
        self.assertIn("--artifact-protocol-version 2", supervisor)
        self.assertIn("--publication-path", supervisor)
        self.assertIn(str(attempt / "runtime" / "HANDOFF_READY.json"), supervisor)
        self.assertIn(f"--task-dir {self.task_dir.resolve()}", supervisor)
        self.assertIn(f"--attempt-id {attempt.name}", supervisor)
        self.assertTrue((attempt / "runtime" / "transcript.log").exists())
        self.assertFalse((attempt / "transcript.log").exists())
        self.assertFalse((attempt / "COMPLETION.json").exists())
        self.assertFalse((self.task_dir / "HANDOFF.json").exists())
        self.assertFalse((self.task_dir / "EVIDENCE.md").exists())

    def test_full_dispatch_rejects_worker_replacement_of_frozen_approved_strategy(self) -> None:
        from tests.unit.test_strategy import strategy_payload

        self.set_task_profile("full")
        status = json.loads((self.task_dir / "STATUS.json").read_text(encoding="utf-8"))
        status.update(
            branch="agent/T001",
            state="pending",
            previous_state=None,
            current_attempt_id=None,
            state_history=[],
        )
        self.write_json(self.task_dir / "STATUS.json", status)
        strategy = strategy_payload("T001")
        strategy["workflows"][0]["executor"]["allowed_paths"] = ["file.txt"]
        submit_strategy(self.task_dir, strategy)
        review_strategy(
            self.task_dir,
            1,
            decision="approved",
            reviewer="coordinator",
        )
        status = json.loads((self.task_dir / "STATUS.json").read_text(encoding="utf-8"))
        status.update(
            state="strategy_review",
            previous_state="planning",
            owner="dispatch",
            updated_at="2026-07-15T00:01:00Z",
            state_history=[
                {
                    "from": "pending",
                    "to": "planning",
                    "actor": "dispatch",
                    "at": "2026-07-15T00:00:00Z",
                },
                {
                    "from": "planning",
                    "to": "strategy_review",
                    "actor": "dispatch",
                    "at": "2026-07-15T00:01:00Z",
                },
            ],
        )
        self.write_json(self.task_dir / "STATUS.json", status)

        worker = self.task_dir / "replace-approved-strategy.py"
        worker.write_text(
            f"""#!/usr/bin/env python3
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

prompt = sys.stdin.read()
task = Path(re.search(r\"^- TASK_DIR: (.+)$\", prompt, re.M).group(1))
attempt = Path(re.search(r\"^- ATTEMPT_DIR: (.+)$\", prompt, re.M).group(1))
strategy_path = task / \"strategy\" / \"STRATEGY-v002.json\"
review_path = task / \"strategy\" / \"REVIEW-v002.json\"
current_path = task / \"strategy\" / \"CURRENT.json\"
attempt_path = attempt / \"ATTEMPT.json\"

strategy = json.loads(
    (task / \"strategy\" / \"STRATEGY-v001.json\").read_text(encoding=\"utf-8\")
)
strategy[\"strategy_id\"] = \"T001-S002-worker-replacement\"
strategy[\"revision\"] = 2
strategy[\"supersedes\"] = \"T001-S001\"
strategy[\"objective\"] = \"Worker-controlled replacement of the approved strategy.\"
strategy_path.write_text(json.dumps(strategy, indent=2) + \"\\n\", encoding=\"utf-8\")
canonical = json.dumps(
    strategy, sort_keys=True, separators=(\",\", \":\"), ensure_ascii=True
).encode(\"utf-8\")
replacement_sha = hashlib.sha256(canonical).hexdigest()

review = json.loads(
    (task / \"strategy\" / \"REVIEW-v001.json\").read_text(encoding=\"utf-8\")
)
review.update(
    strategy_id=strategy[\"strategy_id\"],
    strategy_sha256=replacement_sha,
    reviewer=\"worker-forged-coordinator\",
)
review_path.write_text(json.dumps(review, indent=2) + \"\\n\", encoding=\"utf-8\")
current = json.loads(current_path.read_text(encoding=\"utf-8\"))
current.update(
    revision=2,
    strategy=\"STRATEGY-v002.json\",
    review=\"REVIEW-v002.json\",
    strategy_id=strategy[\"strategy_id\"],
    strategy_sha256=replacement_sha,
)
current_path.write_text(json.dumps(current, indent=2) + \"\\n\", encoding=\"utf-8\")
metadata = json.loads(attempt_path.read_text(encoding=\"utf-8\"))
metadata.update(
    strategy_id=strategy[\"strategy_id\"],
    strategy_revision=2,
    strategy_sha256=replacement_sha,
)
attempt_path.write_text(json.dumps(metadata, indent=2) + \"\\n\", encoding=\"utf-8\")

rdo = [sys.executable, {str(SCRIPTS / 'rdo.py')!r}]
commands = [
    rdo + [\"workflow\", \"start\", \"--attempt-dir\", str(attempt),
           \"--workflow-id\", \"WF-implementation\", \"--instance-id\", \"I001\"],
    rdo + [\"check\", \"--attempt-dir\", str(attempt), \"--check-id\", \"unit\",
           \"--workflow-id\", \"WF-implementation\", \"--instance-id\", \"I001\"],
    rdo + [\"workflow\", \"complete\", \"--attempt-dir\", str(attempt),
           \"--workflow-id\", \"WF-implementation\", \"--instance-id\", \"I001\"],
    rdo + [\"finalize\", \"--attempt-dir\", str(attempt), \"--state\", \"review\",
           \"--summary\", \"Worker replaced the coordinator-approved strategy.\"],
]
results = [subprocess.run(command, capture_output=True, text=True) for command in commands]
(attempt / \"runtime\" / \"strategy-replacement-results.json\").write_text(
    json.dumps([{{\"returncode\": item.returncode, \"stderr\": item.stderr}} for item in results], indent=2) + \"\\n\",
    encoding=\"utf-8\",
)
""",
            encoding="utf-8",
        )
        worker.chmod(0o755)

        dispatch = subprocess.run(
            [
                str(SCRIPTS / "dispatch_agent.sh"),
                "R001",
                "T001",
                "--worker",
                "claude-code",
                "--runtime",
                "plain",
                "--io",
                "machine",
                "--command",
                str(worker),
            ],
            cwd=self.root,
            env={
                **os.environ,
                "RDO_TEST_ALLOW_UNGOVERNED_COMMAND_OVERRIDE": "1",
            },
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(4, dispatch.returncode, dispatch.stderr)
        status = json.loads((self.task_dir / "STATUS.json").read_text(encoding="utf-8"))
        attempt = self.task_dir / "attempts" / str(status["current_attempt_id"])
        metadata = json.loads((attempt / "ATTEMPT.json").read_text(encoding="utf-8"))
        self.assertEqual("invalid_handoff", metadata["state"])
        self.assertIn("strategy", status["blocking_reason"].lower())

    def test_contract_drift_blocks_later_attempt_before_publication(self) -> None:
        first = json.loads(self.freeze("A001").stdout)
        self.create_attempt("A001", first["sha256"])
        (self.task_dir / "TASK.md").write_text(task_text("Implement a revised feature."), encoding="utf-8")
        result = self.freeze("A002", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("revision task", result.stderr)
        self.assertFalse((self.task_dir / "attempts" / "A002" / "TASK_INPUTS.json").exists())

    def test_dependency_resolution_freezes_exact_merged_commit(self) -> None:
        dependency = self.run_dir / "tasks" / "T000"
        dependency.mkdir()
        self.write_json(
            dependency / "STATUS.json",
            {"task_id": "T000", "artifact_protocol_version": 2, "state": "merged"},
        )
        self.append_event(
            {
                "event": "task_merged",
                "task_id": "T000",
                "commit": self.base,
                "verification": {"passed": True},
            }
        )
        dependencies = json.dumps(
            {
                "schema_version": 2,
                "dependencies": [{"task_id": "T000", "required_state": "merged"}],
            },
            indent=2,
        )
        updated = task_text().replace(
            '{\n  "schema_version": 2,\n  "dependencies": []\n}',
            dependencies,
        )
        (self.task_dir / "TASK.md").write_text(updated, encoding="utf-8")
        frozen = json.loads(self.freeze("A001").stdout)
        payload = json.loads(Path(frozen["path"]).read_text())
        self.assertEqual(
            payload["resolved_dependencies"],
            [{"task_id": "T000", "required_state": "merged", "commit": self.base}],
        )

    def test_dependency_with_failed_v2_merge_verification_is_not_ready(self) -> None:
        dependency = self.run_dir / "tasks" / "T000"
        dependency.mkdir()
        self.write_json(
            dependency / "STATUS.json",
            {"task_id": "T000", "artifact_protocol_version": 2, "state": "merged"},
        )
        self.append_event(
            {
                "event": "task_merged",
                "task_id": "T000",
                "commit": self.base,
                "verification": {"passed": False},
            }
        )
        dependencies = json.dumps(
            {
                "schema_version": 2,
                "dependencies": [{"task_id": "T000", "required_state": "merged"}],
            },
            indent=2,
        )
        (self.task_dir / "TASK.md").write_text(
            task_text().replace(
                '{\n  "schema_version": 2,\n  "dependencies": []\n}',
                dependencies,
            ),
            encoding="utf-8",
        )
        result = self.freeze("A001", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("merged_unverified", result.stderr)

    def _publish_verified(
        self,
        attempt_id: str,
        source_commit: str,
        *,
        include_record: bool = True,
        record_overrides: dict[str, object] | None = None,
    ) -> Path:
        frozen = json.loads(self.freeze(attempt_id).stdout)
        attempt = self.create_attempt(attempt_id, frozen["sha256"])
        metadata = json.loads((attempt / "ATTEMPT.json").read_text())
        metadata["state"] = "running"
        self.write_json(attempt / "ATTEMPT.json", metadata)
        now = "2026-07-15T00:01:00Z"
        self.status.update(
            state="running",
            previous_state="pending",
            owner="worker",
            current_attempt_id=attempt_id,
            state_history=[
                {"from": "pending", "to": "running", "actor": "dispatch", "at": now}
            ],
        )
        self.write_json(self.task_dir / "STATUS.json", self.status)
        runtime = attempt / "runtime"
        self.write_json(runtime / "worktree-before.json", fingerprint(self.root))
        self.write_json(runtime / "worktree-after.json", fingerprint(self.root))
        (runtime / "transcript.log").write_text("worker transcript\n", encoding="utf-8")
        frozen_entries_sha256, finalization_started_at_epoch = (
            self.prepare_finalization(
                attempt,
                task_inputs_sha256=frozen["sha256"],
            )
        )
        command_record_ids: list[str] = []
        if include_record:
            inputs = json.loads((attempt / "TASK_INPUTS.json").read_text())
            command_dir = attempt / "runtime" / "commands"
            command_dir.mkdir(parents=True)
            stdout_path = command_dir / "C001.stdout.log"
            stderr_path = command_dir / "C001.stderr.log"
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            record = {
                "artifact_protocol_version": 2,
                "schema_version": 2,
                "record_id": "C001",
                "task_id": "T001",
                "attempt_id": attempt_id,
                "task_inputs_sha256": frozen["sha256"],
                "acceptance_contract_sha256": inputs["inputs"]["acceptance"]["sha256"],
                "category": "required_commands",
                "check_id": "unit",
                "argv": ["true"],
                "cwd": ".",
                "timeout_seconds": 10,
                "exit_code": 0,
                "timed_out": False,
                "elapsed_seconds": 0.01,
                "surviving_processes": [],
                "started_at": "2026-07-15T00:00:00Z",
                "finished_at": "2026-07-15T00:00:01Z",
                "stdout_ref": stdout_path.relative_to(attempt).as_posix(),
                "stdout_sha256": hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
                "stderr_ref": stderr_path.relative_to(attempt).as_posix(),
                "stderr_sha256": hashlib.sha256(stderr_path.read_bytes()).hexdigest(),
                "source_before_entries_sha256": frozen_entries_sha256,
                "source_after_entries_sha256": frozen_entries_sha256,
                "source_unchanged": True,
                "finalization_started_at_epoch": finalization_started_at_epoch,
                "source_snapshot_entries_sha256": frozen_entries_sha256,
            }
            record.update(record_overrides or {})
            record["record_sha256"] = command_record_sha256(record)
            (attempt / "runtime" / "COMMANDS.ndjson").write_text(
                json.dumps(record, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            command_record_ids = ["C001"]
        publish_bundle(
            attempt,
            requested_state="verified",
            summary="Implemented and self-reviewed the change.",
            known_limitations=[],
            direct_self_review={
                "performed": True,
                "passed": True,
                "summary": "No unresolved findings.",
                "findings": [],
            },
            source_commit=source_commit,
            command_record_ids=command_record_ids,
            changed_paths=[],
            worktree={
                "before": "runtime/worktree-before.json",
                "after": "runtime/worktree-after.json",
            },
            artifact_refs=(
                "runtime/finalization-worktree.json",
                "runtime/FINALIZATION.json",
                "runtime/DEADLINE.json",
            ),
            log_refs=(
                [
                    "runtime/transcript.log",
                    "runtime/commands/C001.stdout.log",
                    "runtime/commands/C001.stderr.log",
                ]
                if include_record
                else ["runtime/transcript.log"]
            ),
            required_outputs=build_required_output_bindings(
                self.root,
                source_commit,
                ["file.txt"],
            ),
            expected_task_id="T001",
            expected_attempt_id=attempt_id,
        )
        self.write_supervisor_receipt(
            attempt,
            finalization_started=True,
        )
        return attempt

    def test_direct_bundle_without_required_command_record_is_rejected(self) -> None:
        attempt = self._publish_verified("A001", self.base, include_record=False)
        result = cmd_validate_handoff(self.validation_args(attempt))
        self.assertEqual(result, 4)
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        self.assertIn("required check", status["blocking_reason"])

    def test_v2_handoff_requires_supervisor_publication_acceptance(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        supervisor = attempt / "supervisor-result.json"
        self.write_json(
            supervisor,
            {
                "exit_code": 0,
                "timed_out": False,
                "publication_requested": False,
                "publication_unaccepted": True,
                "surviving_pids": [],
                "cleanup_verified": True,
            },
        )
        result = cmd_validate_handoff(
            self.validation_args(attempt, supervisor_result=str(supervisor))
        )
        self.assertEqual(4, result)
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        self.assertIn(
            "publication_requested must be True",
            status["blocking_reason"],
        )

    def test_completed_replay_requires_existing_supervisor_receipt(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        args = self.validation_args(attempt)
        self.assertEqual(0, cmd_validate_handoff(args))

        (attempt / "supervisor-result.json").unlink()

        self.assertEqual(4, cmd_validate_handoff(args))
        self.assertEqual(
            "verified",
            json.loads((self.task_dir / "STATUS.json").read_text())["state"],
        )

    def test_completed_replay_rejects_corrupt_supervisor_receipt(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        args = self.validation_args(attempt)
        self.assertEqual(0, cmd_validate_handoff(args))

        (attempt / "supervisor-result.json").write_text("{", encoding="utf-8")

        self.assertEqual(4, cmd_validate_handoff(args))
        self.assertEqual(
            "verified",
            json.loads((self.task_dir / "STATUS.json").read_text())["state"],
        )

    def test_v2_supervisor_receipt_boolean_fields_are_strictly_typed(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        supervisor_path = attempt / "supervisor-result.json"
        supervisor = json.loads(supervisor_path.read_text(encoding="utf-8"))
        supervisor["publication_requested"] = 1
        supervisor["timed_out"] = 0
        self.write_json(supervisor_path, supervisor)

        self.assertEqual(4, cmd_validate_handoff(self.validation_args(attempt)))
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        self.assertIn(
            "publication_requested must be True",
            status["blocking_reason"],
        )
        self.assertIn("timed_out must be False", status["blocking_reason"])

    def test_bundle_with_nonmatching_structured_record_is_rejected(self) -> None:
        attempt = self._publish_verified(
            "A001",
            self.base,
            record_overrides={"argv": ["false"]},
        )
        result = cmd_validate_handoff(self.validation_args(attempt))
        self.assertEqual(result, 4)
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        self.assertIn("no exact successful record", status["blocking_reason"])

    def test_task_root_input_drift_invalidates_published_bundle(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        (self.task_dir / "ACCEPTANCE.md").write_text(
            ACCEPTANCE.replace("The file remains readable", "The file remains unchanged"),
            encoding="utf-8",
        )
        result = cmd_validate_handoff(self.validation_args(attempt))
        self.assertEqual(result, 4)
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        self.assertIn("contract drifted after dispatch", status["blocking_reason"])

    def test_missing_required_output_invalidates_published_bundle(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        (self.root / "file.txt").unlink()
        result = cmd_validate_handoff(self.validation_args(attempt))
        self.assertEqual(result, 4)
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        self.assertIn("required outputs are missing", status["blocking_reason"])

    def test_post_publication_commit_is_rejected_not_recaptured(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        (self.root / "file.txt").chmod(0o755)
        run("git", "add", "file.txt", cwd=self.root)
        run("git", "commit", "-m", "late mode change", cwd=self.root)
        result = cmd_validate_handoff(self.validation_args(attempt))
        self.assertEqual(result, 4)
        metadata = json.loads((attempt / "ATTEMPT.json").read_text())
        self.assertEqual(metadata["state"], "invalid_handoff")
        self.assertNotIn("verified_commit", metadata)
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        self.assertIn("HEAD changed after handoff finalization", status["blocking_reason"])

    def test_completed_attempt_recovers_status_transition_after_interruption(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        metadata = json.loads((attempt / "ATTEMPT.json").read_text())
        metadata.update(
            state="completed",
            handoff_valid=True,
            handoff_state="verified",
            source_commit=self.base,
            verified_commit=self.base,
        )
        self.write_json(attempt / "ATTEMPT.json", metadata)

        result = cmd_validate_handoff(self.validation_args(attempt))
        self.assertEqual(0, result)
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        self.assertEqual("verified", status["state"])

    def test_completed_fast_path_revalidates_bundle_before_returning_success(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        args = self.validation_args(attempt)
        self.assertEqual(0, cmd_validate_handoff(args))
        evidence_path = attempt / "EVIDENCE.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["summary_tamper"] = True
        self.write_json(evidence_path, evidence)

        self.assertEqual(4, cmd_validate_handoff(args))
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        self.assertEqual("verified", status["state"])

    def test_completed_replay_requires_frozen_commit_fields_and_valid_lifecycle(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        args = self.validation_args(attempt)
        self.assertEqual(0, cmd_validate_handoff(args))
        original = json.loads((attempt / "ATTEMPT.json").read_text())

        for field in ("source_commit", "verified_commit"):
            with self.subTest(field=field):
                corrupted = dict(original)
                corrupted.pop(field)
                self.write_json(attempt / "ATTEMPT.json", corrupted)
                self.assertEqual(4, cmd_validate_handoff(args))
        corrupted = dict(original)
        corrupted["exit_code"] = None
        self.write_json(attempt / "ATTEMPT.json", corrupted)
        self.assertEqual(4, cmd_validate_handoff(args))

    def test_completed_replay_does_not_downgrade_downstream_coordinator_state(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        args = self.validation_args(attempt)
        self.assertEqual(0, cmd_validate_handoff(args))
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        status["previous_state"] = "verified"
        status["state"] = "merged"
        status["owner"] = "coordinator"
        status["state_history"].append(
            {
                "from": "verified",
                "to": "merged",
                "actor": "coordinator",
                "at": "2026-07-15T00:03:00Z",
            }
        )
        self.write_json(self.task_dir / "STATUS.json", status)
        (self.run_dir / "EVENTS.ndjson").write_text(
            json.dumps(
                {
                    "at": "2026-07-15T00:03:00Z",
                    "actor": "coordinator",
                    "event": "task_merged",
                    "run_id": "R001",
                    "task_id": "T001",
                    "attempt_id": "A001",
                    "commit": self.base,
                    "source_branch": "main",
                    "target_branch": "main",
                    "coordinator_id": "test-coordinator",
                    "artifact_binding": artifact_binding(attempt),
                    "verification": {"passed": True},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertEqual(0, cmd_validate_handoff(args))
        self.assertEqual(
            "merged",
            json.loads((self.task_dir / "STATUS.json").read_text())["state"],
        )

    def test_completed_replay_rejects_worker_forged_merged_state_on_first_validation(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        metadata = json.loads((attempt / "ATTEMPT.json").read_text())
        metadata.update(
            state="completed",
            handoff_valid=True,
            handoff_state="verified",
            source_commit=self.base,
            verified_commit=self.base,
            ended_at="2026-07-15T00:02:00Z",
            exit_code=0,
        )
        self.write_json(attempt / "ATTEMPT.json", metadata)
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        status.update(
            state="merged",
            previous_state="running",
            owner="worker",
        )
        status["state_history"].append(
            {
                "from": "running",
                "to": "merged",
                "actor": "worker",
                "at": "2026-07-15T00:02:00Z",
            }
        )
        self.write_json(self.task_dir / "STATUS.json", status)

        result = cmd_validate_handoff(self.validation_args(attempt))

        self.assertEqual(4, result)
        self.assertEqual(
            "merged",
            json.loads((self.task_dir / "STATUS.json").read_text())["state"],
        )
        self.assertEqual(
            "completed",
            json.loads((attempt / "ATTEMPT.json").read_text())["state"],
        )

    def test_strategy_changes_requested_replay_binds_submission_and_review(self) -> None:
        from tests.unit.test_strategy import strategy_payload

        self.set_task_profile("full")
        frozen = self.cli(
            "freeze-task-inputs",
            "--task-dir",
            str(self.task_dir),
            "--run-dir",
            str(self.run_dir),
            "--repo-root",
            str(self.root),
            "--task-id",
            "T001",
            "--attempt-id",
            "A001",
            "--attempt-dir",
            str(self.task_dir / "attempts" / "A001"),
            "--profile",
            "full",
            "--execution-mode",
            "start",
        )
        inputs_sha256 = json.loads(frozen.stdout)["sha256"]
        attempt = self.task_dir / "attempts" / "A001"
        self.cli(
            "create-attempt",
            "--path",
            str(attempt / "ATTEMPT.json"),
            "--artifact-protocol-version",
            "2",
            "--task-inputs-ref",
            "TASK_INPUTS.json",
            "--task-inputs-sha256",
            inputs_sha256,
            "--attempt-id",
            "A001",
            "--task-id",
            "T001",
            "--agent-name",
            "worker",
            "--worker-id",
            "W001",
            "--phase",
            "planning",
            "--command",
            "true",
            "--cwd",
            str(self.root),
            "--backend",
            "plain",
        )
        metadata = json.loads((attempt / "ATTEMPT.json").read_text())
        metadata.update(
            state="completed",
            handoff_valid=True,
            handoff_state="strategy_review",
            ended_at="2026-07-15T00:02:00Z",
            exit_code=0,
        )
        self.write_json(attempt / "ATTEMPT.json", metadata)
        self.write_json(attempt / "runtime" / "worktree-before.json", fingerprint(self.root))
        self.write_json(attempt / "runtime" / "worktree-after.json", fingerprint(self.root))
        load_or_create_attempt_deadline(
            attempt / "runtime" / "DEADLINE.json",
            attempt_timeout_seconds=120,
            finalization_grace_seconds=90,
            reminder_seconds=30,
        )

        strategy = strategy_payload("T001")
        strategy["workflows"][0]["executor"]["allowed_paths"] = ["file.txt"]
        strategy_path, strategy_sha256 = submit_strategy(self.task_dir, strategy)
        self.write_json(
            attempt / "runtime" / "STRATEGY_SUBMISSION.json",
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_id": "T001",
                "attempt_id": "A001",
                "strategy_revision": 1,
                "strategy_id": strategy["strategy_id"],
                "strategy_ref": f"../../strategy/{strategy_path.name}",
                "strategy_sha256": strategy_sha256,
            },
        )
        publish_bundle(
            attempt,
            requested_state="strategy_review",
            summary="Strategy is ready for coordinator review.",
            direct_self_review={
                "performed": False,
                "passed": False,
                "summary": "",
                "findings": [],
            },
            source_commit=self.base,
            changed_paths=[],
            worktree={
                "before": "runtime/worktree-before.json",
                "after": "runtime/worktree-after.json",
            },
            artifact_refs=("runtime/STRATEGY_SUBMISSION.json",),
            expected_task_id="T001",
            expected_attempt_id="A001",
        )
        supervisor_path = self.write_supervisor_receipt(
            attempt,
            finalization_started=False,
        )
        supervisor = json.loads(supervisor_path.read_text(encoding="utf-8"))
        self.assertFalse(supervisor["finalization_started"])
        self.assertEqual(
            supervisor["execution_deadline_at_epoch"],
            supervisor["active_deadline_at_epoch"],
        )
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        status.update(
            profile="full",
            state="strategy_review",
            previous_state="planning",
            owner="worker",
            current_attempt_id="A001",
            state_history=[
                {
                    "from": "pending",
                    "to": "planning",
                    "actor": "dispatch",
                    "at": "2026-07-15T00:00:00Z",
                },
                {
                    "from": "planning",
                    "to": "strategy_review",
                    "actor": "dispatch",
                    "at": "2026-07-15T00:02:00Z",
                },
            ],
        )
        self.write_json(self.task_dir / "STATUS.json", status)
        submitted_event = {
            "at": "2026-07-15T00:02:00Z",
            "actor": "worker",
            "event": "strategy_submitted",
            "run_id": "R001",
            "task_id": "T001",
            "strategy_id": strategy["strategy_id"],
            "revision": 1,
            "strategy_sha256": strategy_sha256,
        }
        (self.run_dir / "EVENTS.ndjson").write_text(
            json.dumps(submitted_event, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        args = self.validation_args(attempt)
        self.assertEqual(0, cmd_validate_handoff(args))

        drifted_strategy = json.loads(strategy_path.read_text())
        drifted_strategy["objective"] = "Post-handoff strategy drift."
        self.write_json(strategy_path, drifted_strategy)
        self.assertEqual(4, cmd_validate_handoff(args))
        self.write_json(strategy_path, strategy)

        review = review_strategy(
            self.task_dir,
            1,
            decision="changes_requested",
            reviewer="coordinator",
            notes=["Tighten the workflow."],
        )
        status.update(
            state="changes_requested",
            previous_state="strategy_review",
            owner="coordinator",
        )
        status["state_history"].append(
            {
                "from": "strategy_review",
                "to": "changes_requested",
                "actor": "coordinator",
                "at": "2026-07-15T00:03:00Z",
            }
        )
        self.write_json(self.task_dir / "STATUS.json", status)
        reviewed_event = {
            "at": "2026-07-15T00:03:00Z",
            "actor": "coordinator",
            "event": "strategy_reviewed",
            "run_id": "R001",
            "task_id": "T001",
            "decision": "changes_requested",
            "revision": 1,
            "strategy_sha256": strategy_sha256,
        }
        (self.run_dir / "EVENTS.ndjson").write_text(
            "\n".join(
                json.dumps(event, sort_keys=True)
                for event in (submitted_event, reviewed_event)
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(0, cmd_validate_handoff(args))

        review["strategy_sha256"] = "0" * 64
        self.write_json(self.task_dir / "strategy" / "REVIEW-v001.json", review)
        self.assertEqual(4, cmd_validate_handoff(args))

    def test_completed_replay_rejects_dispatch_identity_drift(self) -> None:
        attempt = self._publish_verified("A001", self.base)
        metadata = json.loads((attempt / "ATTEMPT.json").read_text())
        args = self.validation_args(
            attempt,
            expected_profile="direct",
            expected_task_id="T001",
            expected_artifact_protocol_version=2,
            expected_phase="execution",
            expected_branch="main",
            expected_worktree=str(self.root),
            expected_worker_backend="claude-code",
            expected_backend_profile_sha256="",
            expected_backend_settings_sha256="",
            expected_read_policy_sha256="",
            expected_strategy_id="",
            expected_strategy_revision="",
            expected_strategy_sha256="",
            expected_task_inputs_sha256=metadata["task_inputs_sha256"],
            expected_task_base_commit=self.base,
            expected_worktree_before_sha256=hashlib.sha256(
                (attempt / "runtime" / "worktree-before.json").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(0, cmd_validate_handoff(args))
        status = json.loads((self.task_dir / "STATUS.json").read_text())
        status["previous_state"] = "verified"
        status["state"] = "merged"
        status["profile"] = "delegated"
        status["state_history"].append(
            {"from": "verified", "to": "merged", "actor": "coordinator", "at": "later"}
        )
        self.write_json(self.task_dir / "STATUS.json", status)

        self.assertEqual(4, cmd_validate_handoff(args))
        self.assertEqual(
            "merged",
            json.loads((self.task_dir / "STATUS.json").read_text())["state"],
        )

    def test_legacy_completed_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "tasks" / "Tlegacy"
            attempt = task / "attempts" / "A001"
            attempt.mkdir(parents=True)
            self.write_json(
                task / "STATUS.json",
                {
                    "task_id": "Tlegacy",
                    "state": "review",
                    "current_attempt_id": "A001",
                },
            )
            self.write_json(
                attempt / "ATTEMPT.json",
                {
                    "attempt_id": "A001",
                    "state": "completed",
                    "phase": "execution",
                    "handoff_valid": True,
                    "handoff_state": "review",
                },
            )
            self.write_json(task / "HANDOFF.json", {"requested_state": "review"})
            result = cmd_validate_handoff(
                argparse.Namespace(
                    attempt_path=str(attempt / "ATTEMPT.json"),
                    task_dir=str(task),
                    status_path=str(task / "STATUS.json"),
                    attempt_id="A001",
                    exit_code_raw="0",
                    startup_path="",
                    worktree="",
                    expected_artifact_protocol_version=1,
                    expected_phase="execution",
                )
            )
            self.assertEqual(0, result)
            self.assertEqual(
                "review",
                json.loads((task / "STATUS.json").read_text())["state"],
            )


if __name__ == "__main__":
    unittest.main()
