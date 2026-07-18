import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import artifact_bundle
from artifact_bundle import (
    IntegrityError,
    MissingArtifactError,
    PublicationConflictError,
    UnsafeArtifactReferenceError,
    artifact_binding,
    command_record_sha256,
    load_bundle,
    load_command_records,
    publish_bundle,
    safe_ref,
    validate_artifact_binding,
    validate_task_inputs_binding,
)
from task_contract import TASK_INPUT_FILENAMES, build_task_inputs_payload


class ArtifactBundleTests(unittest.TestCase):
    def write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def make_attempt(self, root: Path, attempt_id: str = "A001") -> Path:
        attempt = root / "tasks" / "T001" / "attempts" / attempt_id
        runtime = attempt / "runtime"
        runtime.mkdir(parents=True)
        task_inputs = build_task_inputs_payload(
            task_id="T001",
            attempt_id=attempt_id,
            source_bytes={name: f"{name}\n".encode() for name in TASK_INPUT_FILENAMES},
            task_base_commit="a" * 40,
            resolved_dependencies=[],
        )
        inputs_path = attempt / "TASK_INPUTS.json"
        self.write_json(inputs_path, task_inputs)
        inputs_digest = hashlib.sha256(inputs_path.read_bytes()).hexdigest()
        self.write_json(
            attempt / "ATTEMPT.json",
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_id": "T001",
                "attempt_id": attempt_id,
                "task_inputs_ref": "TASK_INPUTS.json",
                "task_inputs_sha256": inputs_digest,
                "state": "running",
            },
        )
        command_dir = runtime / "commands"
        command_dir.mkdir()
        stdout_path = command_dir / "C001.stdout.log"
        stderr_path = command_dir / "C001.stderr.log"
        stdout_path.write_text("ok\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        command = {
            "artifact_protocol_version": 2,
            "schema_version": 2,
            "record_id": "C001",
            "task_id": "T001",
            "attempt_id": attempt_id,
            "task_inputs_sha256": inputs_digest,
            "check_id": "unit",
            "acceptance_contract_sha256": task_inputs["inputs"]["acceptance"]["sha256"],
            "category": "required_commands",
            "argv": ["python3", "-m", "unittest"],
            "cwd": "/tmp/worktree",
            "timeout_seconds": 60,
            "exit_code": 0,
            "timed_out": False,
            "elapsed_seconds": 1.25,
            "surviving_processes": [],
            "started_at": "2026-07-15T00:00:00Z",
            "finished_at": "2026-07-15T00:00:01Z",
            "stdout_ref": stdout_path.relative_to(attempt).as_posix(),
            "stdout_sha256": hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
            "stderr_ref": stderr_path.relative_to(attempt).as_posix(),
            "stderr_sha256": hashlib.sha256(stderr_path.read_bytes()).hexdigest(),
        }
        command["record_sha256"] = command_record_sha256(command)
        (runtime / "COMMANDS.ndjson").write_text(
            json.dumps(command, sort_keys=True) + "\n", encoding="utf-8"
        )
        (runtime / "transcript.log").write_text("worker output\n", encoding="utf-8")
        self.write_json(runtime / "worktree-before.json", {"sha256": "before"})
        self.write_json(runtime / "worktree-after.json", {"sha256": "after"})
        return attempt

    def publish(self, attempt: Path, **overrides):
        values = {
            "requested_state": "verified",
            "summary": "Implemented and verified the task.",
            "known_limitations": [],
            "direct_self_review": {
                "performed": True,
                "passed": True,
                "summary": "Reviewed the final diff and acceptance results.",
                "findings": [],
            },
            "source_commit": "b" * 40,
            "command_record_ids": ["C001"],
            "changed_paths": ["src/example.py"],
            "worktree": {
                "before": "runtime/worktree-before.json",
                "after": "runtime/worktree-after.json",
            },
            "log_refs": [
                "runtime/transcript.log",
                "runtime/commands/C001.stdout.log",
                "runtime/commands/C001.stderr.log",
            ],
            "expected_task_id": "T001",
            "expected_attempt_id": attempt.name,
        }
        values.update(overrides)
        return publish_bundle(attempt, **values)

    def test_valid_bundle_has_complete_attempt_local_digest_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            bundle = self.publish(attempt)

            self.assertTrue((attempt / "HANDOFF.json").is_file())
            self.assertTrue((attempt / "EVIDENCE.json").is_file())
            self.assertTrue((attempt / "runtime" / "HANDOFF_READY.json").is_file())
            self.assertFalse((attempt.parent.parent / "HANDOFF.json").exists())
            self.assertNotIn("commands_run", bundle.handoff)
            self.assertNotIn("files_changed", bundle.handoff)
            self.assertEqual("C001", bundle.evidence["command_records"][0]["record_id"])
            self.assertEqual(
                bundle.task_inputs_binding.task_inputs["inputs"]["acceptance"]["sha256"],
                bundle.evidence["command_records"][0]["acceptance_contract_sha256"],
            )
            self.assertEqual(bundle.evidence_sha256, bundle.handoff["evidence_sha256"])
            self.assertEqual(bundle.handoff_sha256, bundle.ready["handoff_sha256"])
            self.assertEqual(
                bundle.task_inputs_binding.task_inputs_sha256,
                bundle.ready["task_inputs_sha256"],
            )
            self.assertEqual(
                bundle.task_inputs_binding.attempt_binding_sha256,
                bundle.ready["attempt_binding_sha256"],
            )
            self.assertIsInstance(bundle.ready["published_at_epoch"], float)
            self.assertTrue(bundle.ready["published_at"].endswith("Z"))

            approval = artifact_binding(bundle)
            loaded = validate_artifact_binding(
                attempt,
                approval,
                expected_task_id="T001",
                expected_attempt_id="A001",
                expected_source_commit="b" * 40,
            )
            self.assertEqual(bundle.ready_sha256, loaded.ready_sha256)

    def test_reviewer_receipt_binds_workflow_artifact_and_backend_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            artifact = attempt / "runtime" / "reviews" / "reviewer-1.md"
            artifact.parent.mkdir()
            artifact.write_text("No findings.\n", encoding="utf-8")
            start = {
                "at": "2026-07-18T00:00:00Z",
                "event": "subagent_started",
                "session_id": "reviewer-1",
            }
            stop = {
                "at": "2026-07-18T00:00:01Z",
                "event": "subagent_stopped",
                "session_id": "reviewer-1",
            }
            events = attempt / "runtime" / "BACKEND_EVENTS.ndjson"
            events.write_text(
                json.dumps(start, sort_keys=True) + "\n" +
                json.dumps(stop, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            artifact_ref = artifact.relative_to(attempt).as_posix()
            artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
            event_digest = lambda value: hashlib.sha256(
                json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            receipt_ref = "runtime/reviewer-receipts/reviewer-1.json"
            receipt_sha = artifact_bundle.publish_json_once(
                attempt / receipt_ref,
                {
                    "schema_version": 1,
                    "artifact_protocol_version": 2,
                    "receipt_type": "independent_review",
                    "task_id": "T001",
                    "attempt_id": "A001",
                    "workflow_id": "WF-review",
                    "instance_id": "WF-review-I001",
                    "reviewer_id": "reviewer-1",
                    "reviewer_started_at": start["at"],
                    "reviewer_start_event_sha256": event_digest(start),
                    "reviewer_stopped_at": stop["at"],
                    "reviewer_stop_event_sha256": event_digest(stop),
                    "artifact_ref": artifact_ref,
                    "artifact_sha256": artifact_sha,
                    "artifact_mtime_ns": artifact.stat().st_mtime_ns,
                },
            )
            self.publish(
                attempt,
                reviewer_evidence=[
                    {
                        "reviewer_id": "reviewer-1",
                        "workflow_id": "WF-review",
                        "instance_id": "WF-review-I001",
                        "ref": artifact_ref,
                        "receipt_ref": receipt_ref,
                        "receipt_sha256": receipt_sha,
                    }
                ],
            )
            self.assertEqual("reviewer-1", load_bundle(attempt).evidence["reviewer_evidence"][0]["reviewer_id"])

            events.write_text(json.dumps(start, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "backend lifecycle event"):
                load_bundle(attempt)

    def test_attempt_binds_exact_task_inputs_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            payload = json.loads((attempt / "TASK_INPUTS.json").read_text())
            payload["task_base_commit"] = "changed"
            self.write_json(attempt / "TASK_INPUTS.json", payload)
            with self.assertRaisesRegex(IntegrityError, "task_inputs_sha256"):
                validate_task_inputs_binding(attempt)

    def test_command_reader_rejects_declared_record_digest_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            path = attempt / "runtime" / "COMMANDS.ndjson"
            record = json.loads(path.read_text())
            record["exit_code"] = 1
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "record_sha256"):
                load_command_records(attempt)

    def test_publish_is_create_once_and_equal_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            first = self.publish(attempt)
            second = self.publish(attempt)
            self.assertEqual(first.ready_sha256, second.ready_sha256)

            with self.assertRaisesRegex(PublicationConflictError, "immutable"):
                self.publish(attempt, summary="A different transition request")
            self.assertEqual(first.ready_sha256, load_bundle(attempt).ready_sha256)

    def test_interrupted_publication_recovers_only_matching_partial_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            real_publish = artifact_bundle.publish_json_once
            calls = 0

            def interrupt_after_evidence(path, payload):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated crash")
                return real_publish(path, payload)

            with patch("artifact_bundle.publish_json_once", side_effect=interrupt_after_evidence):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    self.publish(attempt)
            self.assertTrue((attempt / "EVIDENCE.json").exists())
            self.assertFalse((attempt / "HANDOFF.json").exists())
            self.assertFalse((attempt / "runtime" / "HANDOFF_READY.json").exists())

            recovered = self.publish(attempt)
            self.assertEqual("handoff_ready", recovered.ready["publication"])

    def test_different_partial_publication_cannot_be_recovered_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            real_publish = artifact_bundle.publish_json_once
            calls = 0

            def interrupt_after_evidence(path, payload):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated crash")
                return real_publish(path, payload)

            with patch("artifact_bundle.publish_json_once", side_effect=interrupt_after_evidence):
                with self.assertRaises(RuntimeError):
                    self.publish(attempt)
            with self.assertRaisesRegex(PublicationConflictError, "immutable"):
                self.publish(attempt, changed_paths=["src/different.py"])

    def test_marker_for_stale_or_other_attempt_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_attempt(root, "A001")
            second = self.make_attempt(root, "A002")
            self.publish(first)
            self.publish(second)

            with self.assertRaisesRegex(IntegrityError, "supervised/current attempt"):
                load_bundle(first, expected_attempt_id="A002")

            shutil.copyfile(
                first / "runtime" / "HANDOFF_READY.json",
                second / "runtime" / "HANDOFF_READY.json",
            )
            with self.assertRaisesRegex(IntegrityError, "identity"):
                load_bundle(second, expected_attempt_id="A002")

    def test_invalid_marker_and_digest_tamper_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            self.publish(attempt)
            marker_path = attempt / "runtime" / "HANDOFF_READY.json"
            marker = json.loads(marker_path.read_text())
            del marker["evidence_sha256"]
            self.write_json(marker_path, marker)
            with self.assertRaisesRegex(IntegrityError, "evidence_sha256"):
                load_bundle(attempt)

        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            self.publish(attempt)
            marker_path = attempt / "runtime" / "HANDOFF_READY.json"
            marker = json.loads(marker_path.read_text())
            marker["published_at_epoch"] += 10
            self.write_json(marker_path, marker)
            with self.assertRaisesRegex(IntegrityError, "published_at"):
                load_bundle(attempt)

        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            self.publish(attempt)
            command_path = attempt / "runtime" / "COMMANDS.ndjson"
            command = json.loads(command_path.read_text())
            command["elapsed_seconds"] = 99
            command["record_sha256"] = command_record_sha256(command)
            command_path.write_text(json.dumps(command) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "record digest mismatch"):
                load_bundle(attempt)

        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            self.publish(attempt)
            transcript = attempt / "runtime" / "transcript.log"
            transcript.write_text("worker output\nlate output\n", encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "logs\[0\].sha256"):
                load_bundle(attempt)

    def test_supervisor_runtime_attempt_updates_do_not_break_immutable_identity_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            original = self.publish(attempt)
            metadata_path = attempt / "ATTEMPT.json"
            metadata = json.loads(metadata_path.read_text())
            metadata.update(state="completed", ended_at="2026-07-15T01:03:00Z", exit_code=0)
            self.write_json(metadata_path, metadata)

            loaded = load_bundle(attempt)
            self.assertEqual(original.ready_sha256, loaded.ready_sha256)

            metadata["task_inputs_sha256"] = "0" * 64
            self.write_json(metadata_path, metadata)
            with self.assertRaisesRegex(IntegrityError, "task_inputs_sha256"):
                load_bundle(attempt)

        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.make_attempt(Path(temporary))
            self.publish(attempt)
            evidence_path = attempt / "EVIDENCE.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["changed_paths"].append("src/tampered.py")
            self.write_json(evidence_path, evidence)
            with self.assertRaisesRegex(IntegrityError, "evidence_sha256"):
                load_bundle(attempt)

    def test_v2_missing_never_falls_back_to_task_root_legacy_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt = self.make_attempt(root)
            task = attempt.parent.parent
            self.write_json(task / "HANDOFF.json", {"requested_state": "verified"})
            (task / "EVIDENCE.md").write_text("legacy evidence\n", encoding="utf-8")
            with self.assertRaisesRegex(MissingArtifactError, "EVIDENCE.json"):
                load_bundle(attempt)

    def test_refs_cannot_escape_attempt_or_traverse_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt = self.make_attempt(root)
            outside = root / "outside.json"
            outside.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(UnsafeArtifactReferenceError):
                safe_ref(attempt, "../outside.json")
            (attempt / "runtime" / "outside-link").symlink_to(outside)
            with self.assertRaises(UnsafeArtifactReferenceError):
                safe_ref(attempt, "runtime/outside-link")


if __name__ == "__main__":
    unittest.main()
