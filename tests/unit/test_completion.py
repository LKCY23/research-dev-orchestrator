import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from artifact_bundle import publish_bundle
import completion
from completion import (
    handoff_ready_path,
    inspect_publication_candidate,
    publication_path,
    validate_completion,
    validate_publication,
    write_completion,
)
from task_contract import TASK_INPUT_FILENAMES, build_task_inputs_payload


class CompletionTests(unittest.TestCase):
    def write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    def make_task(self, root: Path) -> tuple[Path, Path]:
        task = root / "tasks" / "T001"
        attempt = task / "attempts" / "A001"
        attempt.mkdir(parents=True)
        (task / "STATUS.json").write_text(
            json.dumps({"task_id": "T001", "state": "planning", "current_attempt_id": "A001"}),
            encoding="utf-8",
        )
        (attempt / "ATTEMPT.json").write_text(
            json.dumps({"attempt_id": "A001", "state": "running", "phase": "planning"}),
            encoding="utf-8",
        )
        (task / "HANDOFF.json").write_text(
            json.dumps({"requested_state": "strategy_review", "strategy_sha256": "abc"}),
            encoding="utf-8",
        )
        return task, attempt

    def make_v2_task(self, root: Path, attempt_id: str = "A001") -> tuple[Path, Path]:
        task = root / "tasks" / "T002"
        attempt = task / "attempts" / attempt_id
        (attempt / "runtime").mkdir(parents=True)
        self.write_json(
            task / "STATUS.json",
            {
                "artifact_protocol_version": 2,
                "task_id": "T002",
                "state": "running",
                "current_attempt_id": attempt_id,
            },
        )
        task_inputs = build_task_inputs_payload(
            task_id="T002",
            attempt_id=attempt_id,
            source_bytes={name: f"{name}\n".encode() for name in TASK_INPUT_FILENAMES},
            task_base_commit="a" * 40,
            resolved_dependencies=[],
        )
        self.write_json(attempt / "TASK_INPUTS.json", task_inputs)
        task_inputs_sha256 = hashlib.sha256(
            (attempt / "TASK_INPUTS.json").read_bytes()
        ).hexdigest()
        self.write_json(
            attempt / "ATTEMPT.json",
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_id": "T002",
                "attempt_id": attempt_id,
                "state": "running",
                "task_inputs_ref": "TASK_INPUTS.json",
                "task_inputs_sha256": task_inputs_sha256,
            },
        )
        publish_bundle(
            attempt,
            requested_state="blocked",
            summary="Waiting for an external prerequisite.",
            conditional_blocker={
                "blocker_type": "external_dependency",
                "reason": "The required service is unavailable.",
            },
            direct_self_review={
                "performed": False,
                "passed": False,
                "summary": "",
                "findings": [],
            },
            expected_task_id="T002",
            expected_attempt_id=attempt_id,
        )
        return task, attempt

    def test_valid_completion_binds_attempt_and_handoff_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, attempt = self.make_task(Path(temporary))
            path = write_completion(
                task,
                attempt_id="A001",
                phase="planning",
                requested_state="strategy_review",
                strategy_sha256="abc",
            )
            result = validate_completion(path, task_dir=task, attempt_id="A001")
            self.assertTrue(result.valid, result.reasons)
            self.assertEqual(attempt / "COMPLETION.json", path)

    def test_changed_handoff_invalidates_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, _attempt = self.make_task(Path(temporary))
            path = write_completion(
                task,
                attempt_id="A001",
                phase="planning",
                requested_state="strategy_review",
                strategy_sha256="abc",
            )
            (task / "HANDOFF.json").write_text(
                json.dumps({"requested_state": "blocked"}), encoding="utf-8"
            )
            result = validate_completion(path, task_dir=task, attempt_id="A001")
            self.assertFalse(result.valid)
            self.assertTrue(any("handoff_sha256" in reason for reason in result.reasons))

    def test_completion_for_stale_attempt_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, _attempt = self.make_task(Path(temporary))
            path = write_completion(
                task,
                attempt_id="A001",
                phase="planning",
                requested_state="strategy_review",
                strategy_sha256="abc",
            )
            status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
            status["current_attempt_id"] = "A002"
            (task / "STATUS.json").write_text(json.dumps(status), encoding="utf-8")
            result = validate_completion(path, task_dir=task, attempt_id="A001")
            self.assertFalse(result.valid)
            self.assertTrue(any("current task attempt" in reason for reason in result.reasons))

    def test_execution_attempt_may_request_strategy_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, attempt = self.make_task(Path(temporary))
            status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
            status["state"] = "running"
            (task / "STATUS.json").write_text(json.dumps(status), encoding="utf-8")
            metadata = json.loads((attempt / "ATTEMPT.json").read_text(encoding="utf-8"))
            metadata["phase"] = "execution"
            (attempt / "ATTEMPT.json").write_text(json.dumps(metadata), encoding="utf-8")
            path = write_completion(
                task,
                attempt_id="A001",
                phase="execution",
                requested_state="strategy_review",
                strategy_sha256="abc",
            )
            result = validate_completion(path, task_dir=task, attempt_id="A001")
            self.assertTrue(result.valid, result.reasons)

    def test_verified_completion_binds_finalize_time_source_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, attempt = self.make_task(Path(temporary))
            status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
            status.update(state="running", profile="direct")
            (task / "STATUS.json").write_text(json.dumps(status), encoding="utf-8")
            metadata = json.loads((attempt / "ATTEMPT.json").read_text(encoding="utf-8"))
            metadata["phase"] = "execution"
            (attempt / "ATTEMPT.json").write_text(json.dumps(metadata), encoding="utf-8")
            (task / "HANDOFF.json").write_text(
                json.dumps({"requested_state": "verified", "source_commit": "commit-a"}),
                encoding="utf-8",
            )
            path = write_completion(
                task,
                attempt_id="A001",
                phase="execution",
                requested_state="verified",
                source_commit="commit-a",
            )
            result = validate_completion(path, task_dir=task, attempt_id="A001")
            self.assertTrue(result.valid, result.reasons)
            self.assertEqual(result.payload["source_commit"], "commit-a")

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["source_commit"] = "commit-b"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = validate_completion(path, task_dir=task, attempt_id="A001")
            self.assertFalse(result.valid)
            self.assertTrue(any("source_commit" in reason for reason in result.reasons))

    def test_v2_publication_validates_the_complete_attempt_local_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, attempt = self.make_v2_task(Path(temporary))
            marker = handoff_ready_path(task, "A001")
            self.assertEqual(attempt / "runtime" / "HANDOFF_READY.json", marker)
            self.assertEqual(marker, publication_path(task, "A001", 2))

            result = validate_publication(
                marker,
                artifact_protocol_version=2,
                task_dir=task,
                attempt_id="A001",
            )
            self.assertTrue(result.valid, result.reasons)
            self.assertEqual("handoff_ready", result.payload["publication"])

            payload = json.loads(marker.read_text(encoding="utf-8"))
            payload["evidence_sha256"] = "0" * 64
            self.write_json(marker, payload)
            result = validate_publication(
                marker,
                artifact_protocol_version=2,
                task_dir=task,
                attempt_id="A001",
            )
            self.assertFalse(result.valid)
            self.assertTrue(any("evidence_sha256" in reason for reason in result.reasons))

    def test_candidate_reads_one_open_marker_when_path_is_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, attempt = self.make_v2_task(Path(temporary))
            marker = attempt / "runtime" / "HANDOFF_READY.json"
            original_payload = json.loads(marker.read_text(encoding="utf-8"))
            replacement_payload = dict(original_payload, task_id="replacement")
            replacement = marker.with_name("replacement.json")
            self.write_json(replacement, replacement_payload)
            real_open = os.open
            replaced = False

            def open_and_replace(path, flags, *args, **kwargs):
                nonlocal replaced
                descriptor = real_open(path, flags, *args, **kwargs)
                if Path(path) == marker and not replaced:
                    replaced = True
                    os.replace(replacement, marker)
                return descriptor

            with mock.patch.object(completion.os, "open", side_effect=open_and_replace):
                result = inspect_publication_candidate(
                    marker,
                    artifact_protocol_version=2,
                    task_dir=task,
                    attempt_id="A001",
                )
            self.assertTrue(result.valid, result.reasons)
            self.assertEqual("T002", result.payload["task_id"])
            self.assertNotEqual(
                result.receipt["inode"],
                marker.stat(follow_symlinks=False).st_ino,
            )

    def test_candidate_rejects_same_inode_rewrite_during_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, attempt = self.make_v2_task(Path(temporary))
            marker = attempt / "runtime" / "HANDOFF_READY.json"
            original_read = os.read
            rewritten = False

            def read_and_rewrite(descriptor, size):
                nonlocal rewritten
                chunk = original_read(descriptor, size)
                if chunk and not rewritten:
                    rewritten = True
                    marker.write_bytes(marker.read_bytes())
                return chunk

            with mock.patch.object(completion.os, "read", side_effect=read_and_rewrite):
                result = inspect_publication_candidate(
                    marker,
                    artifact_protocol_version=2,
                    task_dir=task,
                    attempt_id="A001",
                )
            self.assertFalse(result.valid)
            self.assertIn("changed while", result.reasons[0])

    def test_v2_publication_for_a_stale_attempt_is_not_a_stop_signal(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, _attempt = self.make_v2_task(Path(temporary))
            status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
            status["current_attempt_id"] = "A002"
            self.write_json(task / "STATUS.json", status)
            result = validate_publication(
                handoff_ready_path(task, "A001"),
                artifact_protocol_version=2,
                task_dir=task,
                attempt_id="A001",
            )
            self.assertFalse(result.valid)
            self.assertTrue(any("current task attempt" in reason for reason in result.reasons))

    def test_protocol_explicit_validator_keeps_legacy_completion_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, _attempt = self.make_task(Path(temporary))
            completion = write_completion(
                task,
                attempt_id="A001",
                phase="planning",
                requested_state="strategy_review",
                strategy_sha256="abc",
            )
            legacy = validate_publication(
                completion,
                artifact_protocol_version=1,
                task_dir=task,
                attempt_id="A001",
            )
            self.assertTrue(legacy.valid, legacy.reasons)

            wrong_protocol = validate_publication(
                completion,
                artifact_protocol_version=2,
                task_dir=task,
                attempt_id="A001",
            )
            self.assertFalse(wrong_protocol.valid)
            self.assertTrue(any("publication path" in reason for reason in wrong_protocol.reasons))


if __name__ == "__main__":
    unittest.main()
