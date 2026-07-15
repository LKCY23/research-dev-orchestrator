import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from artifact_bundle import command_record_sha256, publish_bundle
from artifact_resolver import (
    ArtifactResolutionError,
    UnsupportedArtifactProtocolError,
    artifact_binding_for_task,
    protocol_route,
    resolve_task_artifacts,
    validate_artifact_binding_for_task,
)
from task_contract import TASK_INPUT_FILENAMES, build_task_inputs_payload


class ArtifactResolverTests(unittest.TestCase):
    def git(self, root: Path, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    def write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def make_repo(self, root: Path) -> str:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        self.git(root, "config", "user.email", "test@example.com")
        self.git(root, "config", "user.name", "Resolver Test")
        (root / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.git(root, "add", "tracked.txt")
        self.git(root, "commit", "-qm", "initial")
        return self.git(root, "rev-parse", "HEAD")

    def make_v2_task(self, root: Path, *, publish: bool = True) -> tuple[Path, dict, str]:
        commit = self.make_repo(root)
        task = root / ".agent-collab" / "runs" / "run" / "tasks" / "T001"
        attempt = task / "attempts" / "A001"
        runtime = attempt / "runtime"
        runtime.mkdir(parents=True)
        inputs = build_task_inputs_payload(
            task_id="T001",
            attempt_id="A001",
            source_bytes={name: f"{name}\n".encode() for name in TASK_INPUT_FILENAMES},
            task_base_commit=commit,
            resolved_dependencies=[],
        )
        self.write_json(attempt / "TASK_INPUTS.json", inputs)
        digest = hashlib.sha256((attempt / "TASK_INPUTS.json").read_bytes()).hexdigest()
        self.write_json(
            attempt / "ATTEMPT.json",
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_id": "T001",
                "attempt_id": "A001",
                "task_inputs_ref": "TASK_INPUTS.json",
                "task_inputs_sha256": digest,
                "state": "completed",
                "handoff_valid": True,
                "handoff_state": "verified",
            },
        )
        command_dir = runtime / "commands"
        command_dir.mkdir()
        stdout_path = command_dir / "C001.stdout.log"
        stderr_path = command_dir / "C001.stderr.log"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        command = {
            "artifact_protocol_version": 2,
            "schema_version": 2,
            "record_id": "C001",
            "task_id": "T001",
            "attempt_id": "A001",
            "task_inputs_sha256": digest,
            "check_id": "unit",
            "acceptance_contract_sha256": inputs["inputs"]["acceptance"]["sha256"],
            "category": "required_commands",
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
        }
        command["record_sha256"] = command_record_sha256(command)
        (runtime / "COMMANDS.ndjson").write_text(json.dumps(command) + "\n", encoding="utf-8")
        self.write_json(runtime / "worktree-before.json", {"head": commit})
        self.write_json(runtime / "worktree-after.json", {"head": commit})
        status = {
            "task_id": "T001",
            "artifact_protocol_version": 2,
            "profile": "direct",
            "state": "verified",
            "current_attempt_id": "A001",
            "worktree": str(root),
        }
        self.write_json(task / "STATUS.json", status)
        if publish:
            publish_bundle(
                attempt,
                requested_state="verified",
                summary="Verified resolver fixture.",
                direct_self_review={
                    "performed": True,
                    "passed": True,
                    "summary": "Reviewed diff and checks.",
                    "findings": [],
                },
                source_commit=commit,
                command_record_ids=["C001"],
                changed_paths=["tracked.txt"],
                worktree={
                    "before": "runtime/worktree-before.json",
                    "after": "runtime/worktree-after.json",
                },
                log_refs=[
                    stdout_path.relative_to(attempt).as_posix(),
                    stderr_path.relative_to(attempt).as_posix(),
                ],
                expected_task_id="T001",
                expected_attempt_id="A001",
            )
        return task, status, commit

    def test_protocol_route_is_explicit_and_unknown_versions_fail(self):
        self.assertEqual(("v2", 2), protocol_route({"artifact_protocol_version": 2}))
        self.assertEqual(("legacy-v1", 1), protocol_route({"artifact_protocol_version": 1}))
        self.assertEqual(("legacy-v0.5", 1), protocol_route({}))
        with self.assertRaises(UnsupportedArtifactProtocolError):
            protocol_route({"artifact_protocol_version": 3})
        with self.assertRaises(UnsupportedArtifactProtocolError):
            protocol_route({"artifact_protocol_version": []})

    def test_v2_resolves_only_current_attempt_and_recomputes_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, status, commit = self.make_v2_task(Path(temporary))
            resolved = resolve_task_artifacts(task, status)
            self.assertEqual("v2", resolved.protocol)
            self.assertEqual("published", resolved.publication_state)
            self.assertEqual(commit, resolved.commit_check.resolved)
            self.assertTrue(resolved.commit_check.valid)
            self.assertIn("attempts/A001/EVIDENCE.json", resolved.artifact_refs["evidence"])
            self.assertEqual("verified", resolved.handoff_index["requested_state"])

            binding = artifact_binding_for_task(
                task,
                status,
                expected_requested_state="verified",
                expected_source_commit=commit,
            )
            self.assertEqual(commit, binding["source_commit"])
            self.assertEqual("A001", binding["attempt_id"])
            validated = validate_artifact_binding_for_task(
                task,
                status,
                binding,
                expected_requested_state="verified",
                expected_source_commit=commit,
            )
            self.assertEqual("A001", validated.task_inputs_binding.attempt_id)
            changed = dict(binding)
            changed["evidence_sha256"] = "0" * 64
            with self.assertRaisesRegex(ArtifactResolutionError, "stored artifact binding"):
                validate_artifact_binding_for_task(task, status, changed)

    def test_v2_missing_ready_never_falls_back_to_task_root_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, status, _ = self.make_v2_task(Path(temporary), publish=False)
            self.write_json(task / "HANDOFF.json", {"requested_state": "verified"})
            (task / "EVIDENCE.md").write_text("legacy\n", encoding="utf-8")
            with self.assertRaisesRegex(ArtifactResolutionError, "no HANDOFF_READY"):
                resolve_task_artifacts(task, status)

            status["current_attempt_id"] = "../A001"
            with self.assertRaisesRegex(ArtifactResolutionError, "safe path component"):
                resolve_task_artifacts(task, status)

    def test_v2_invalid_handoff_blocker_is_auditable_without_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, status, _ = self.make_v2_task(Path(temporary), publish=False)
            attempt_path = task / "attempts" / "A001" / "ATTEMPT.json"
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt["state"] = "invalid_handoff"
            self.write_json(attempt_path, attempt)
            status.update(state="blocked", blocker_type="needs_coordinator")

            resolved = resolve_task_artifacts(task, status)
            self.assertEqual("rejected", resolved.publication_state)
            self.assertIsNone(resolved.bundle)
            with self.assertRaisesRegex(ArtifactResolutionError, "no HANDOFF_READY"):
                resolve_task_artifacts(task, status, require_publication=True)

    def test_v2_rejected_handoff_with_ready_is_not_exposed_as_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, status, _ = self.make_v2_task(Path(temporary))
            attempt_path = task / "attempts" / "A001" / "ATTEMPT.json"
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt["state"] = "invalid_handoff"
            attempt["handoff_valid"] = False
            self.write_json(attempt_path, attempt)
            status.update(state="blocked", blocker_type="needs_coordinator")

            resolved = resolve_task_artifacts(task, status)
            self.assertEqual("rejected", resolved.publication_state)
            self.assertIsNone(resolved.bundle)
            self.assertIn("handoff_ready", resolved.artifact_refs)
            with self.assertRaises(ArtifactResolutionError):
                resolve_task_artifacts(task, status, require_publication=True)

    def test_full_planning_changes_requested_resolves_strategy_review_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, status, commit = self.make_v2_task(Path(temporary), publish=False)
            attempt_dir = task / "attempts" / "A001"
            attempt = json.loads((attempt_dir / "ATTEMPT.json").read_text())
            attempt.update(phase="planning", handoff_state="strategy_review")
            self.write_json(attempt_dir / "ATTEMPT.json", attempt)
            publish_bundle(
                attempt_dir,
                requested_state="strategy_review",
                summary="Submitted a frozen strategy.",
                direct_self_review={
                    "performed": False,
                    "passed": False,
                    "summary": "",
                    "findings": [],
                },
                source_commit=commit,
                worktree={
                    "before": "runtime/worktree-before.json",
                    "after": "runtime/worktree-after.json",
                },
                expected_task_id="T001",
                expected_attempt_id="A001",
            )
            status.update(state="changes_requested", profile="full")
            resolved = resolve_task_artifacts(task, status)
            self.assertEqual("published", resolved.publication_state)
            self.assertEqual("strategy_review", resolved.handoff_index["requested_state"])

    def test_v2_source_worktree_drift_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, status, _ = self.make_v2_task(root)
            (root / "tracked.txt").write_text("later\n", encoding="utf-8")
            self.git(root, "add", "tracked.txt")
            self.git(root, "commit", "-qm", "later")
            with self.assertRaisesRegex(ArtifactResolutionError, "worktree HEAD differs"):
                resolve_task_artifacts(task, status)

    def test_strict_consumer_rejects_ready_before_dispatch_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, status, commit = self.make_v2_task(Path(temporary))
            attempt_path = task / "attempts" / "A001" / "ATTEMPT.json"
            attempt = json.loads(attempt_path.read_text())
            attempt["state"] = "running"
            attempt.pop("handoff_valid", None)
            attempt.pop("handoff_state", None)
            self.write_json(attempt_path, attempt)
            with self.assertRaisesRegex(ArtifactResolutionError, "completed, valid"):
                artifact_binding_for_task(
                    task,
                    status,
                    expected_requested_state="verified",
                    expected_source_commit=commit,
                )

    def test_legacy_v05_remains_task_root_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "task"
            task.mkdir()
            status = {"task_id": "T-old", "state": "review", "current_attempt_id": "A-old"}
            self.write_json(
                task / "HANDOFF.json",
                {
                    "_template": False,
                    "requested_state": "review",
                    "summary": "legacy summary",
                    "commands_run": ["pytest"],
                    "files_changed": ["old.py"],
                    "known_limitations": [],
                    "needs_coordinator": False,
                },
            )
            (task / "EVIDENCE.md").write_text("legacy evidence\n", encoding="utf-8")
            resolved = resolve_task_artifacts(task, status, verify_commit=False)
            self.assertEqual("legacy-v0.5", resolved.protocol)
            self.assertEqual("legacy summary", resolved.handoff_index["summary"])
            self.assertEqual(
                str(task.resolve() / "HANDOFF.json"),
                resolved.artifact_refs["handoff_json"],
            )


if __name__ == "__main__":
    unittest.main()
