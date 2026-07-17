import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from artifact_bundle import artifact_binding, file_sha256, publish_bundle
from collect_status import (
    collect,
    load_events,
    render_human,
    render_summary,
    validate_approved_task,
    validate_attempt,
    validate_merged_task,
)
from protocol import PACKAGE_VERSION, PROTOCOL_VERSION, utc_now
from render_dashboard import render_task_card
from status_projection import resolve_status_projection
from task_contract import TASK_INPUT_FILENAMES, build_task_inputs_payload


class CollectStatusV2Tests(unittest.TestCase):
    def write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def make_run(self, root: Path, artifact_version: int) -> Path:
        run = root / ".agent-collab" / "runs" / "run"
        task = run / "tasks" / "T001"
        task.mkdir(parents=True)
        self.write_json(
            run / "RUN.json",
            {
                "run_id": "run",
                "package_version": PACKAGE_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "target_branch": "main",
            },
        )
        (run / "EVENTS.ndjson").write_text(
            json.dumps(
                {
                    "at": "2026-07-15T00:00:00Z",
                    "actor": "coordinator",
                    "event": "task_created",
                    "run_id": "run",
                    "task_id": "T001",
                    "profile": "direct",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (run / "JOURNAL.md").write_text("# Journal\n", encoding="utf-8")
        self.write_json(
            task / "STATUS.json",
            {
                "task_id": "T001",
                "artifact_protocol_version": artifact_version,
                "profile": "direct",
                "state": "pending",
                "previous_state": None,
                "owner": "",
                "branch": "agent/T001",
                "worktree": ".agent-worktrees/T001",
                "updated_at": "2026-07-15T00:00:00Z",
                "needs_coordinator": False,
                "summary": "",
                "blocking_reason": "",
                "blocker_type": "",
                "current_attempt_id": None,
                "assigned_worker": None,
                "evidence": {"commands_run": [], "logs": [], "passed": None},
                "state_history": [],
            },
        )
        return run

    def test_load_events_returns_each_record_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            (run / "EVENTS.ndjson").write_text(
                json.dumps(
                    {
                        "at": "2026-07-15T00:00:00Z",
                        "actor": "coordinator",
                        "event": "run_created",
                        "run_id": "run",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            events, violations, warnings = load_events(run, "run")
            self.assertEqual(1, len(events))
            self.assertEqual([], violations)
            self.assertEqual([], warnings)

    def test_collect_audit_warns_during_dispatch_interwrite_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "tasks" / "T001"
            attempt = task / "attempts" / "A001"
            (attempt / "runtime").mkdir(parents=True)
            status = {
                "task_id": "T001",
                "artifact_protocol_version": 2,
                "profile": "delegated",
                "state": "running",
                "current_attempt_id": "A001",
            }
            self.write_json(task / "STATUS.json", status)
            self.write_json(
                attempt / "ATTEMPT.json",
                {
                    "attempt_id": "A001",
                    "task_id": "T001",
                    "agent": "claude-code",
                    "agent_name": "worker",
                    "session_id": "",
                    "state": "completed",
                    "handoff_valid": True,
                    "handoff_state": "review",
                    "started_at": utc_now(),
                    "ended_at": utc_now(),
                    "exit_code": 0,
                    "phase": "execution",
                    "runtime": {
                        "backend": "plain",
                        "cli": "claude",
                        "command": "claude",
                        "cwd": str(Path(temporary)),
                    },
                },
            )
            (task / "LOCK").write_text("attempt_id: A001\n", encoding="utf-8")
            dispatch_lock = task / ".dispatch-lock"
            dispatch_lock.mkdir()
            (dispatch_lock / "attempt_id").write_text("A001\n", encoding="utf-8")
            (dispatch_lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            resolved = SimpleNamespace(
                bundle=SimpleNamespace(handoff={"requested_state": "review"})
            )
            with patch("collect_status.resolve_task_artifacts", return_value=resolved):
                violations, warnings, _attempt = validate_attempt(task, status, 10, 60)
            self.assertEqual([], violations)
            self.assertTrue(any("inter-write" in item for item in warnings))

            metadata = json.loads((attempt / "ATTEMPT.json").read_text())
            metadata["handoff_state"] = "strategy_review"
            self.write_json(attempt / "ATTEMPT.json", metadata)
            resolved.bundle.handoff["requested_state"] = "strategy_review"
            with patch("collect_status.resolve_task_artifacts", return_value=resolved):
                violations, _warnings, _attempt = validate_attempt(task, status, 10, 60)
            self.assertTrue(any("not a recoverable" in item for item in violations))

            metadata["handoff_state"] = "review"
            metadata["ended_at"] = "2099-01-01T00:00:00Z"
            self.write_json(attempt / "ATTEMPT.json", metadata)
            resolved.bundle.handoff["requested_state"] = "review"
            with patch("collect_status.resolve_task_artifacts", return_value=resolved):
                violations, _warnings, _attempt = validate_attempt(task, status, 10, 60)
            self.assertTrue(any("ended_at is in the future" in item for item in violations))

            status["state"] = "planning"
            metadata["phase"] = "planning"
            metadata["handoff_state"] = "strategy_review"
            metadata["ended_at"] = utc_now()
            self.write_json(attempt / "ATTEMPT.json", metadata)
            resolved.bundle.handoff["requested_state"] = "strategy_review"
            with patch("collect_status.resolve_task_artifacts", return_value=resolved):
                violations, _warnings, _attempt = validate_attempt(task, status, 10, 60)
            self.assertTrue(
                any("STATUS planning requires profile='full'" in item for item in violations)
            )

    def test_cleanup_failure_allows_only_a_matching_retained_dispatch_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "tasks" / "T001"
            attempt = task / "attempts" / "A001"
            attempt.mkdir(parents=True)
            status = {
                "task_id": "T001",
                "artifact_protocol_version": 2,
                "profile": "delegated",
                "state": "blocked",
                "current_attempt_id": "A001",
                "blocker_type": "irrecoverable",
                "blocking_reason": "worker descendant survived cleanup",
            }
            self.write_json(task / "STATUS.json", status)
            self.write_json(
                attempt / "ATTEMPT.json",
                {
                    "attempt_id": "A001",
                    "task_id": "T001",
                    "agent": "claude-code",
                    "agent_name": "worker",
                    "session_id": "",
                    "state": "invalid_handoff",
                    "outcome": "execution_failed",
                    "handoff_valid": False,
                    "handoff_state": None,
                    "started_at": utc_now(),
                    "ended_at": utc_now(),
                    "exit_code": 6,
                    "phase": "execution",
                    "cleanup_failure": {
                        "terminated": True,
                        "surviving_pids": [43210],
                    },
                    "runtime": {
                        "backend": "tmux",
                        "cli": "claude",
                        "command": "claude",
                        "cwd": str(Path(temporary)),
                        "tmux_session": "rdo-cleanup",
                        "attach_command": "tmux attach -t rdo-cleanup",
                    },
                },
            )
            dispatch_lock = task / ".dispatch-lock"
            dispatch_lock.mkdir()
            (dispatch_lock / "attempt_id").write_text("A001\n", encoding="utf-8")
            (dispatch_lock / "pid").write_text("43211\n", encoding="utf-8")
            (dispatch_lock / "tmux_session").write_text(
                "rdo-cleanup\n", encoding="utf-8"
            )

            violations, warnings, _attempt = validate_attempt(task, status, 10, 60)
            self.assertEqual([], violations)
            self.assertTrue(any("intentionally retained" in item for item in warnings))

            status["blocker_type"] = "environment"
            violations, _warnings, _attempt = validate_attempt(task, status, 10, 60)
            self.assertTrue(
                any("STATUS.blocker_type must be irrecoverable" in item for item in violations)
            )

            status["blocker_type"] = "irrecoverable"
            (dispatch_lock / "attempt_id").write_text("A999\n", encoding="utf-8")
            violations, _warnings, _attempt = validate_attempt(task, status, 10, 60)
            self.assertTrue(
                any("attempt_id does not match" in item for item in violations)
            )

            for path in dispatch_lock.iterdir():
                path.unlink()
            dispatch_lock.rmdir()
            violations, _warnings, _attempt = validate_attempt(task, status, 10, 60)
            self.assertTrue(
                any("requires retained .dispatch-lock" in item for item in violations)
            )

    def make_published_task(
        self,
        root: Path,
        *,
        profile: str = "delegated",
        state: str = "approved",
    ) -> tuple[Path, dict, str, dict]:
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Collect Test"], check=True)
        (root / "tracked.txt").write_text("result\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "result"], check=True)
        commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()

        run = self.make_run(root, 2)
        task = run / "tasks" / "T001"
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
        inputs_digest = hashlib.sha256((attempt / "TASK_INPUTS.json").read_bytes()).hexdigest()
        self.write_json(
            attempt / "ATTEMPT.json",
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_id": "T001",
                "attempt_id": "A001",
                "task_inputs_ref": "TASK_INPUTS.json",
                "task_inputs_sha256": inputs_digest,
                "state": "completed",
                "handoff_valid": True,
                "handoff_state": "verified" if profile == "direct" else "review",
            },
        )
        self.write_json(runtime / "worktree-before.json", {"head": commit})
        self.write_json(runtime / "worktree-after.json", {"head": commit})
        requested_state = "verified" if profile == "direct" else "review"
        bundle = publish_bundle(
            attempt,
            requested_state=requested_state,
            summary="Frozen collect-status fixture.",
            direct_self_review={
                "performed": profile == "direct",
                "passed": profile == "direct",
                "summary": "Self-review passed." if profile == "direct" else "",
                "findings": [],
            },
            source_commit=commit,
            changed_paths=["tracked.txt"],
            worktree={
                "before": "runtime/worktree-before.json",
                "after": "runtime/worktree-after.json",
            },
            expected_task_id="T001",
            expected_attempt_id="A001",
        )
        status = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))
        status.update(
            profile=profile,
            state=state,
            previous_state="review" if state == "approved" else ("approved" if profile != "direct" else "verified"),
            owner="coordinator",
            branch="main",
            worktree=str(root),
            current_attempt_id="A001",
        )
        self.write_json(task / "STATUS.json", status)

        binding = artifact_binding(bundle)
        if profile != "direct":
            decision = {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_id": "T001",
                "revision": 1,
                "decision": "approved",
                "approved_commit": commit,
                "source_branch": "main",
                "target_branch": "main",
                "artifact_binding": binding,
            }
            decision_path = task / "reviews" / "DECISION-v001.json"
            self.write_json(decision_path, decision)
            self.write_json(
                task / "reviews" / "CURRENT_TASK_REVIEW.json",
                {
                    "revision": 1,
                    "decision_path": "reviews/DECISION-v001.json",
                    "decision_sha256": file_sha256(decision_path),
                },
            )
        return task, status, commit, binding

    def add_attempt(
        self,
        task: Path,
        attempt_id: str,
        commit: str,
        *,
        state: str = "running",
        outcome: str | None = None,
    ) -> Path:
        attempt = task / "attempts" / attempt_id
        attempt.mkdir(parents=True)
        inputs = build_task_inputs_payload(
            task_id="T001",
            attempt_id=attempt_id,
            source_bytes={name: f"{name}\n".encode() for name in TASK_INPUT_FILENAMES},
            task_base_commit=commit,
            resolved_dependencies=[],
        )
        self.write_json(attempt / "TASK_INPUTS.json", inputs)
        inputs_digest = hashlib.sha256(
            (attempt / "TASK_INPUTS.json").read_bytes()
        ).hexdigest()
        payload = {
            "schema_version": 2,
            "artifact_protocol_version": 2,
            "task_id": "T001",
            "attempt_id": attempt_id,
            "task_inputs_ref": "TASK_INPUTS.json",
            "task_inputs_sha256": inputs_digest,
            "state": state,
            "phase": "execution",
            "handoff_valid": False,
            "handoff_state": None,
        }
        if outcome is not None:
            payload["outcome"] = outcome
        self.write_json(attempt / "ATTEMPT.json", payload)
        return attempt

    def test_v2_projection_ignores_stale_status_result_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = self.make_run(root, 2)
            status_path = run / "tasks" / "T001" / "STATUS.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["summary"] = "stale previous result"
            status["evidence"] = {
                "commands_run": ["stale command"],
                "logs": [],
                "passed": True,
            }
            self.write_json(status_path, status)
            before = status_path.read_bytes()

            with patch("collect_status.repo_root", return_value=root):
                report = collect("run", 24)

            task = report["tasks"][0]
            projection = task["status_projection"]
            self.assertEqual("", task["summary"])
            self.assertEqual("none", task["summary_relation"])
            self.assertEqual("unpublished", projection["publication"]["state"])
            self.assertFalse(
                projection["compatibility"]["status_summary_authoritative"]
            )
            self.assertFalse(
                projection["compatibility"]["status_evidence_authoritative"]
            )
            self.assertNotIn("stale previous result", render_summary(report))
            self.assertIn("publication=unpublished", render_human(report))
            self.assertIn("evidence=no", render_human(report))
            self.assertEqual(before, status_path.read_bytes())

    def test_v2_projection_attributes_current_and_previous_publications(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, status, commit, _binding = self.make_published_task(
                root,
                profile="direct",
                state="approved",
            )
            status["summary"] = "stale STATUS summary"
            self.write_json(task / "STATUS.json", status)

            current = resolve_status_projection(task, status).projection
            self.assertEqual("published", current["publication"]["state"])
            self.assertEqual("A001", current["publication"]["attempt_id"])
            self.assertEqual("current", current["display"]["summary_relation"])
            self.assertEqual(
                "Frozen collect-status fixture.",
                current["display"]["summary"],
            )

            self.add_attempt(task, "A002", commit)
            status.update(state="running", current_attempt_id="A002")
            self.write_json(task / "STATUS.json", status)
            before = {
                path.relative_to(task): path.read_bytes()
                for path in task.rglob("*")
                if path.is_file()
            }

            resumed = resolve_status_projection(task, status).projection
            self.assertEqual("unpublished", resumed["publication"]["state"])
            self.assertEqual("A002", resumed["publication"]["attempt_id"])
            self.assertEqual(
                "A001", resumed["previous_publication"]["attempt_id"]
            )
            self.assertEqual("previous", resumed["display"]["summary_relation"])
            self.assertEqual("A001", resumed["display"]["summary_attempt_id"])
            self.assertEqual(
                "Frozen collect-status fixture.",
                resumed["display"]["summary"],
            )
            card = render_task_card(
                task.parent.parent,
                {
                    "task_id": "T001",
                    "state": "running",
                    "owner": "worker",
                    "current_attempt_id": "A002",
                    "summary": resumed["display"]["summary"],
                    "summary_relation": resumed["display"]["summary_relation"],
                    "summary_attempt_id": resumed["display"]["summary_attempt_id"],
                    "status_projection": resumed,
                    "artifact_resolution": {
                        "valid": True,
                        "protocol": "v2",
                        "artifact_refs": {},
                    },
                },
            )
            self.assertIn("Previous A001:", card)
            self.assertNotIn("stale STATUS summary", card)
            after = {
                path.relative_to(task): path.read_bytes()
                for path in task.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

            historical_path = task / "attempts" / "A001" / "ATTEMPT.json"
            historical = json.loads(historical_path.read_text(encoding="utf-8"))
            historical.update(state="invalid_handoff", handoff_valid=False)
            self.write_json(historical_path, historical)
            rejected_history = resolve_status_projection(task, status).projection
            self.assertIsNone(rejected_history["previous_publication"])
            self.assertEqual("none", rejected_history["display"]["summary_relation"])

    def test_v2_projection_never_promotes_rejected_or_invalid_publication(self):
        for publication_state in ("rejected", "invalid"):
            with self.subTest(publication_state=publication_state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task, status, commit, _binding = self.make_published_task(
                    root,
                    profile="direct",
                    state="approved",
                )
                if publication_state == "invalid":
                    attempt = self.add_attempt(
                        task,
                        "A002",
                        commit,
                        state="completed",
                    )
                    status.update(
                        state="verified",
                        previous_state="running",
                        current_attempt_id="A002",
                    )
                    self.write_json(
                        attempt / "runtime" / "HANDOFF_READY.json",
                        {"schema_version": 2},
                    )
                else:
                    self.add_attempt(
                        task,
                        "A002",
                        commit,
                        state="invalid_handoff",
                        outcome="timed_out_unfinalized",
                    )
                    status.update(
                        state="blocked",
                        previous_state="running",
                        current_attempt_id="A002",
                        blocker_type="budget",
                        blocking_reason="attempt timed out",
                    )
                self.write_json(task / "STATUS.json", status)

                projection = resolve_status_projection(task, status).projection
                self.assertEqual(
                    publication_state,
                    projection["publication"]["state"],
                )
                self.assertFalse(projection["publication"]["valid"])
                self.assertFalse(
                    projection["publication"]["evidence"]["available"]
                )
                self.assertEqual(
                    "A001", projection["previous_publication"]["attempt_id"]
                )
                self.assertEqual(
                    "previous", projection["display"]["summary_relation"]
                )

    def test_legacy_projection_preserves_status_or_task_root_handoff_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "tasks" / "T001"
            task.mkdir(parents=True)
            status = {
                "task_id": "T001",
                "artifact_protocol_version": 1,
                "state": "pending",
                "summary": "legacy STATUS summary",
            }
            self.write_json(task / "STATUS.json", status)
            self.write_json(
                task / "HANDOFF.json",
                {"_template": False, "summary": "legacy handoff summary"},
            )

            projection = resolve_status_projection(task, status).projection
            self.assertEqual("legacy-v1", projection["protocol"])
            self.assertEqual(
                "legacy STATUS summary", projection["display"]["summary"]
            )
            self.assertEqual(
                "legacy_status_or_handoff",
                projection["display"]["summary_relation"],
            )
            self.assertTrue(
                projection["compatibility"]["status_summary_authoritative"]
            )

    def test_v2_pending_task_is_explicitly_unpublished_not_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_run(root, 2)
            with patch("collect_status.repo_root", return_value=root):
                report = collect("run", 24)
            self.assertTrue(report["valid"], report["protocol_violations"])
            artifacts = report["tasks"][0]["artifact_resolution"]
            self.assertTrue(artifacts["valid"])
            self.assertEqual("v2", artifacts["protocol"])
            self.assertEqual("unpublished", artifacts["publication_state"])

    def test_v2_status_projects_optional_task_budget_without_mutating_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = self.make_run(root, 2)
            task = run / "tasks" / "T001"
            self.write_json(
                task / "EXECUTION_POLICY.json",
                {
                    "schema_version": 2,
                    "strategy_required": False,
                    "task_budget": {"max_attempts": 2},
                },
            )
            status_before = (task / "STATUS.json").read_bytes()
            with patch("collect_status.repo_root", return_value=root):
                report = collect("run", 24)
            projection = report["tasks"][0]["task_budget"]
            self.assertTrue(projection["enabled"])
            self.assertEqual(0, projection["consumed"]["attempts"])
            self.assertEqual(2, projection["remaining"]["attempts"])
            self.assertTrue(projection["admission"]["allowed"])
            self.assertEqual(status_before, (task / "STATUS.json").read_bytes())

    def test_v2_status_requires_an_explicit_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = self.make_run(root, 2)
            status_path = run / "tasks" / "T001" / "STATUS.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status.pop("profile")
            self.write_json(status_path, status)
            with patch("collect_status.repo_root", return_value=root):
                report = collect("run", 24)
            self.assertFalse(report["valid"])
            self.assertTrue(
                any(
                    "artifact-protocol-v2 STATUS requires an explicit profile" in item
                    for item in report["protocol_violations"]
                )
            )

    def test_v2_status_rejects_null_or_post_creation_profile_changes(self):
        for value, expected in (
            (None, "invalid execution profile None"),
            ("full", "STATUS.profile 'full' does not match task_created profile 'direct'"),
        ):
            with self.subTest(profile=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run = self.make_run(root, 2)
                status_path = run / "tasks" / "T001" / "STATUS.json"
                status = json.loads(status_path.read_text(encoding="utf-8"))
                status["profile"] = value
                self.write_json(status_path, status)
                with patch("collect_status.repo_root", return_value=root):
                    report = collect("run", 24)
                self.assertFalse(report["valid"])
                self.assertTrue(
                    any(expected in item for item in report["protocol_violations"]),
                    report["protocol_violations"],
                )

    def test_unknown_artifact_protocol_makes_report_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_run(root, 3)
            with patch("collect_status.repo_root", return_value=root):
                report = collect("run", 24)
            self.assertFalse(report["valid"])
            artifacts = report["tasks"][0]["artifact_resolution"]
            self.assertFalse(artifacts["valid"])
            self.assertIn("unsupported STATUS.artifact_protocol_version", artifacts["error"])
            self.assertTrue(
                any("artifact resolution failed" in item for item in report["protocol_violations"])
            )

    def test_v2_approved_revalidates_pointer_digest_and_bundle_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, status, _, _ = self.make_published_task(Path(temporary))
            self.assertEqual([], validate_approved_task(task, status))

            pointer_path = task / "reviews" / "CURRENT_TASK_REVIEW.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer["decision_sha256"] = "0" * 64
            self.write_json(pointer_path, pointer)
            violations = validate_approved_task(task, status)
            self.assertTrue(any("decision_sha256 does not match" in item for item in violations))

    def test_v2_approved_rejects_rehashed_tampered_artifact_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            task, status, _, _ = self.make_published_task(Path(temporary))
            decision_path = task / "reviews" / "DECISION-v001.json"
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            decision["artifact_binding"]["evidence_sha256"] = "0" * 64
            self.write_json(decision_path, decision)
            pointer_path = task / "reviews" / "CURRENT_TASK_REVIEW.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer["decision_sha256"] = file_sha256(decision_path)
            self.write_json(pointer_path, pointer)

            violations = validate_approved_task(task, status)
            self.assertTrue(any("artifact binding is invalid" in item for item in violations))

    def test_v2_merged_revalidates_event_binding_for_delegated_and_direct(self):
        for profile in ("delegated", "direct"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task, status, commit, binding = self.make_published_task(
                    root,
                    profile=profile,
                    state="merged",
                )
                event = {
                    "event": "task_merged",
                    "task_id": "T001",
                    "commit": commit,
                    "source_branch": "main",
                    "target_branch": "main",
                    "attempt_id": "A001",
                    "artifact_binding": binding,
                }
                violations, warnings = validate_merged_task(
                    root,
                    task,
                    status,
                    {"target_branch": "main"},
                    [event],
                )
                self.assertEqual([], violations)
                self.assertEqual([], warnings)

                event["artifact_binding"] = dict(binding)
                event["artifact_binding"]["ready_sha256"] = "0" * 64
                violations, _ = validate_merged_task(
                    root,
                    task,
                    status,
                    {"target_branch": "main"},
                    [event],
                )
                self.assertTrue(
                    any("task_merged artifact binding is invalid" in item for item in violations)
                )

    def test_v2_merged_rejects_event_attempt_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, status, commit, binding = self.make_published_task(
                root,
                profile="direct",
                state="merged",
            )
            event = {
                "event": "task_merged",
                "task_id": "T001",
                "commit": commit,
                "source_branch": "main",
                "target_branch": "main",
                "attempt_id": "A999",
                "artifact_binding": binding,
            }
            violations, _ = validate_merged_task(
                root,
                task,
                status,
                {"target_branch": "main"},
                [event],
            )
            self.assertTrue(any("attempt_id does not match" in item for item in violations))


if __name__ == "__main__":
    unittest.main()
