import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from artifact_bundle import publish_bundle
from completion import write_completion
from machine_attempt_supervisor import (
    session_id_from_event,
    startup_event,
    worker_progress_event,
)
from supervisor import pid_alive
from task_contract import TASK_INPUT_FILENAMES, build_task_inputs_payload


ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = ROOT / "scripts" / "machine_attempt_supervisor.py"


class MachineAttemptSupervisorTests(unittest.TestCase):
    def write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    def make_v2_publication(self, root: Path, attempt_id: str = "A001") -> tuple[Path, Path]:
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
        digest = hashlib.sha256((attempt / "TASK_INPUTS.json").read_bytes()).hexdigest()
        self.write_json(
            attempt / "ATTEMPT.json",
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_id": "T002",
                "attempt_id": attempt_id,
                "state": "running",
                "task_inputs_ref": "TASK_INPUTS.json",
                "task_inputs_sha256": digest,
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

    def publication_command(
        self,
        root: Path,
        task: Path,
        attempt: Path,
        worker_argv: list[str],
        *,
        protocol_version: int = 2,
        timeout: float = 2,
    ) -> list[str]:
        prompt = root / "prompt.md"
        prompt.write_text("work\n", encoding="utf-8")
        signal = (
            attempt / "runtime" / "HANDOFF_READY.json"
            if protocol_version == 2
            else attempt / "COMPLETION.json"
        )
        return [
            sys.executable,
            str(SUPERVISOR),
            "--backend",
            "claude-code",
            "--argv-json",
            json.dumps(worker_argv),
            "--cwd",
            str(root),
            "--prompt-path",
            str(prompt),
            "--prompt-transport",
            "arg",
            "--startup-timeout-seconds",
            "1",
            "--timeout-seconds",
            str(timeout),
            "--startup-result",
            str(attempt / "runtime" / "STARTUP.json"),
            "--supervisor-result",
            str(attempt / "supervisor-result.json"),
            "--supervisor-state",
            str(attempt / "runtime" / "supervisor.json"),
            "--transcript",
            str(
                attempt / "runtime" / "transcript.log"
                if protocol_version == 2
                else attempt / "transcript.log"
            ),
            "--custom-command",
            "--artifact-protocol-version",
            str(protocol_version),
            "--publication-path",
            str(signal),
            "--task-dir",
            str(task),
            "--attempt-id",
            attempt.name,
            "--handoff-grace-seconds",
            "0.05",
        ]

    def test_backend_session_id_decoders(self):
        self.assertEqual(
            session_id_from_event("claude-code", b'{"type":"system","subtype":"init","session_id":"claude-s1"}'),
            "claude-s1",
        )
        self.assertEqual(
            session_id_from_event("codex", b'{"type":"thread.started","thread_id":"codex-s1"}'),
            "codex-s1",
        )
    def test_backend_startup_event_decoders(self):
        self.assertEqual(
            startup_event("claude-code", b'{"type":"system","subtype":"init"}'),
            "system/init",
        )
        self.assertEqual(startup_event("codex", b'{"type":"thread.started"}'), "thread.started")
        self.assertEqual(startup_event("kimi-code", b'{"type":"session.start"}'), "session.start")
        self.assertIsNone(startup_event("claude-code", b'{"type":"assistant"}'))
        self.assertIsNone(startup_event("codex", b"not-json"))

    def test_codex_progress_requires_model_or_tool_output(self):
        self.assertIsNone(worker_progress_event("codex", b'{"type":"thread.started"}'))
        self.assertIsNone(worker_progress_event("codex", b'{"type":"turn.started"}'))
        self.assertIsNone(
            worker_progress_event(
                "codex",
                b'{"type":"item.completed","item":{"type":"error"}}',
            )
        )
        self.assertEqual(
            "item.completed:reasoning",
            worker_progress_event(
                "codex",
                b'{"type":"item.completed","item":{"type":"reasoning"}}',
            ),
        )

    def run_supervisor(self, *, transport: str, event: str | None):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        prompt = "initial prompt\nwith a second line\n"
        (root / "prompt.md").write_text(prompt, encoding="utf-8")
        helper = root / "worker.py"
        helper.write_text(
            textwrap.dedent(
                """
                import json, pathlib, sys
                output = pathlib.Path(sys.argv[1])
                expected_arg = sys.argv[2]
                stdin_text = sys.stdin.read()
                output.write_text(json.dumps({"argv_prompt": expected_arg, "stdin": stdin_text}))
                if sys.argv[3] != "NONE":
                    print(sys.argv[3], flush=True)
                """
            ),
            encoding="utf-8",
        )
        worker_output = root / "worker.json"
        event_arg = event if event is not None else "NONE"
        argv = [sys.executable, str(helper), str(worker_output), prompt if transport == "arg" else "", event_arg]
        command = [
            sys.executable,
            str(SUPERVISOR),
            "--backend",
            "claude-code",
            "--argv-json",
            json.dumps(argv),
            "--cwd",
            str(root),
            "--prompt-path",
            str(root / "prompt.md"),
            "--prompt-transport",
            transport,
            "--startup-timeout-seconds",
            "0.3",
            "--timeout-seconds",
            "2",
            "--startup-result",
            str(root / "STARTUP.json"),
            "--supervisor-result",
            str(root / "result.json"),
            "--supervisor-state",
            str(root / "state.json"),
            "--transcript",
            str(root / "transcript.log"),
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        observed = json.loads(worker_output.read_text(encoding="utf-8"))
        startup = json.loads((root / "STARTUP.json").read_text(encoding="utf-8"))
        temporary.cleanup()
        return prompt, result, observed, startup

    def test_arg_transport_uses_argv_and_closes_stdin(self):
        prompt, result, observed, startup = self.run_supervisor(
            transport="arg",
            event='{"type":"system","subtype":"init"}',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(observed, {"argv_prompt": prompt, "stdin": ""})
        self.assertEqual(startup["state"], "worker_started")

    def test_stdin_transport_writes_prompt_once(self):
        prompt, result, observed, startup = self.run_supervisor(
            transport="stdin",
            event='{"type":"system","subtype":"init"}',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(observed, {"argv_prompt": "", "stdin": prompt})
        self.assertEqual(startup["state"], "worker_started")

    def test_exit_without_startup_event_fails_startup(self):
        _, result, _, startup = self.run_supervisor(transport="arg", event=None)
        self.assertEqual(result.returncode, 125)
        self.assertEqual(startup["state"], "worker_startup_failed")
        self.assertEqual(startup["failure"]["code"], "early_exit")

    def test_missing_session_error_is_classified_before_handoff(self):
        _, result, _, startup = self.run_supervisor(
            transport="arg",
            event=json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "errors": [
                        "No conversation found with session ID: "
                        "11111111-1111-1111-1111-111111111111"
                    ],
                }
            ),
        )
        self.assertEqual(result.returncode, 125)
        self.assertEqual(startup["state"], "worker_startup_failed")
        self.assertEqual(startup["failure"]["code"], "session_not_found")
        self.assertTrue(startup["failure"]["recoverable_resume_failure"])

    def test_codex_model_rejection_after_thread_start_is_startup_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("work\n", encoding="utf-8")
            worker = root / "worker.py"
            worker.write_text(
                textwrap.dedent(
                    """
                    import json

                    print(json.dumps({
                        "type": "thread.started",
                        "thread_id": "11111111-1111-1111-1111-111111111111",
                    }), flush=True)
                    print(json.dumps({"type": "turn.started"}), flush=True)
                    print(json.dumps({
                        "type": "error",
                        "message": (
                            "The 'gpt-5.6-lune' model is not supported when "
                            "using Codex with a ChatGPT account."
                        ),
                    }), flush=True)
                    print(json.dumps({
                        "type": "turn.failed",
                        "error": {"message": "model unavailable"},
                    }), flush=True)
                    raise SystemExit(1)
                    """
                ),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(SUPERVISOR),
                "--backend",
                "codex",
                "--argv-json",
                json.dumps([sys.executable, str(worker)]),
                "--cwd",
                str(root),
                "--prompt-path",
                str(root / "prompt.md"),
                "--prompt-transport",
                "arg",
                "--startup-timeout-seconds",
                "1",
                "--timeout-seconds",
                "2",
                "--startup-result",
                str(root / "STARTUP.json"),
                "--supervisor-result",
                str(root / "result.json"),
                "--supervisor-state",
                str(root / "state.json"),
                "--transcript",
                str(root / "transcript.log"),
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(125, result.returncode, result.stderr)
            startup = json.loads((root / "STARTUP.json").read_text())
            supervisor = json.loads((root / "result.json").read_text())
            self.assertEqual("worker_startup_failed", startup["state"])
            self.assertEqual("model_unavailable", startup["failure"]["code"])
            self.assertTrue(startup["failure_detected_after_start_event"])
            self.assertEqual(
                "thread.started",
                startup["startup_evidence"]["event"],
            )
            self.assertIsNone(startup["worker_progress_evidence"])
            self.assertTrue(supervisor["startup_failed"])

    def test_codex_model_phrase_after_real_progress_is_execution_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("work\n", encoding="utf-8")
            worker = root / "worker.py"
            worker.write_text(
                textwrap.dedent(
                    """
                    import json

                    print(json.dumps({
                        "type": "thread.started",
                        "thread_id": "11111111-1111-1111-1111-111111111111",
                    }), flush=True)
                    print(json.dumps({"type": "turn.started"}), flush=True)
                    print(json.dumps({
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "The fixture says model example is not supported.",
                        },
                    }), flush=True)
                    print(json.dumps({
                        "type": "error",
                        "message": (
                            "The 'example' model is not supported when using "
                            "Codex with a ChatGPT account."
                        ),
                    }), flush=True)
                    raise SystemExit(1)
                    """
                ),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(SUPERVISOR),
                "--backend",
                "codex",
                "--argv-json",
                json.dumps([sys.executable, str(worker)]),
                "--cwd",
                str(root),
                "--prompt-path",
                str(root / "prompt.md"),
                "--prompt-transport",
                "arg",
                "--startup-timeout-seconds",
                "1",
                "--timeout-seconds",
                "2",
                "--startup-result",
                str(root / "STARTUP.json"),
                "--supervisor-result",
                str(root / "result.json"),
                "--supervisor-state",
                str(root / "state.json"),
                "--transcript",
                str(root / "transcript.log"),
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(1, result.returncode, result.stderr)
            startup = json.loads((root / "STARTUP.json").read_text())
            supervisor = json.loads((root / "result.json").read_text())
            self.assertEqual("worker_started", startup["state"])
            self.assertEqual(
                "item.completed:agent_message",
                startup["worker_progress_evidence"]["event"],
            )
            self.assertIsNone(startup["failure"])
            self.assertFalse(supervisor["startup_failed"])

    def test_hard_turn_budget_terminates_machine_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("work\n")
            worker = root / "worker.py"
            worker.write_text(textwrap.dedent("""
                import json, time
                print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
                for identifier in ("one", "two"):
                    print(json.dumps({"type": "assistant", "message": {
                        "id": identifier, "usage": {"input_tokens": 10, "output_tokens": 2}
                    }}), flush=True)
                time.sleep(10)
            """))
            profile = root / "profile.json"
            profile.write_text(json.dumps({"resource_budget": {"max_model_turns": 1}}))
            command = [
                sys.executable, str(SUPERVISOR), "--backend", "claude-code",
                "--argv-json", json.dumps([sys.executable, str(worker)]),
                "--cwd", str(root), "--prompt-path", str(root / "prompt.md"),
                "--prompt-transport", "arg", "--startup-timeout-seconds", "1",
                "--timeout-seconds", "20", "--startup-result", str(root / "STARTUP.json"),
                "--supervisor-result", str(root / "result.json"),
                "--supervisor-state", str(root / "state.json"),
                "--transcript", str(root / "transcript.log"),
                "--backend-profile", str(profile),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 125, result.stderr)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["state"], "budget_exceeded")
            self.assertTrue((root / "runtime" / "VIOLATIONS.ndjson").exists())

    def test_natural_worker_exit_cleans_reparented_setsid_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompt.md").write_text("work\n")
            sentinel = root / "late.txt"
            worker = root / "worker.py"
            worker.write_text(textwrap.dedent(f"""
                import subprocess, sys
                print('{{"type":"system","subtype":"init"}}', flush=True)
                child = "import pathlib,time; time.sleep(0.6); pathlib.Path({str(sentinel)!r}).write_text('late')"
                subprocess.Popen([sys.executable, "-c", child], start_new_session=True)
            """))
            command = [
                sys.executable, str(SUPERVISOR), "--backend", "claude-code",
                "--argv-json", json.dumps([sys.executable, str(worker)]),
                "--cwd", str(root), "--prompt-path", str(root / "prompt.md"),
                "--prompt-transport", "arg", "--startup-timeout-seconds", "1",
                "--timeout-seconds", "2", "--startup-result", str(root / "STARTUP.json"),
                "--supervisor-result", str(root / "result.json"),
                "--supervisor-state", str(root / "state.json"),
                "--transcript", str(root / "transcript.log"),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stderr)
            time.sleep(0.75)
            self.assertFalse(sentinel.exists())
            payload = json.loads((root / "result.json").read_text())
            self.assertGreaterEqual(len(payload["observed_pids"]), 2)
            self.assertEqual([], payload["surviving_pids"])

    def test_valid_v2_ready_stops_machine_worker_and_cleans_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, attempt = self.make_v2_publication(root)
            command = self.publication_command(
                root,
                task,
                attempt,
                ["/bin/sh", "-c", "sleep 30 & wait"],
            )
            result = subprocess.run(command, capture_output=True, text=True, timeout=8)
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads((attempt / "supervisor-result.json").read_text())
            self.assertTrue(payload["publication_requested"])
            self.assertTrue(payload["handoff_ready"]["valid"])
            self.assertEqual([], payload["surviving_pids"])
            self.assertFalse(any(pid_alive(pid) for pid in payload["observed_pids"]))
            state = json.loads((attempt / "runtime" / "supervisor.json").read_text())
            self.assertEqual("handoff_ready", state["state"])

    def test_invalid_v2_ready_does_not_stop_machine_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, attempt = self.make_v2_publication(root)
            marker = attempt / "runtime" / "HANDOFF_READY.json"
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
            marker_payload["evidence_sha256"] = "0" * 64
            self.write_json(marker, marker_payload)
            sentinel = root / "worker-finished"
            worker = [
                sys.executable,
                "-c",
                "import pathlib,time; time.sleep(.35); pathlib.Path(%r).write_text('done')"
                % str(sentinel),
            ]
            command = self.publication_command(root, task, attempt, worker)
            result = subprocess.run(command, capture_output=True, text=True, timeout=8)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(sentinel.exists())
            payload = json.loads((attempt / "supervisor-result.json").read_text())
            self.assertFalse(payload["publication_requested"])
            self.assertFalse(payload["handoff_ready"]["valid"])
            self.assertTrue(
                any("evidence_sha256" in reason for reason in payload["handoff_ready"]["reasons"])
            )

    def test_semantically_invalid_but_digest_closed_ready_does_not_stop_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, attempt = self.make_v2_publication(root)
            evidence_path = attempt / "EVIDENCE.json"
            handoff_path = attempt / "HANDOFF.json"
            marker = attempt / "runtime" / "HANDOFF_READY.json"
            evidence = json.loads(evidence_path.read_text())
            handoff = json.loads(handoff_path.read_text())
            ready = json.loads(marker.read_text())
            evidence["source_commit"] = "abc123"
            self.write_json(evidence_path, evidence)
            handoff["source_commit"] = "abc123"
            handoff["evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            self.write_json(handoff_path, handoff)
            ready.update(
                source_commit="abc123",
                source_commit_sha256=hashlib.sha256(b"abc123").hexdigest(),
                evidence_sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                handoff_sha256=hashlib.sha256(handoff_path.read_bytes()).hexdigest(),
            )
            self.write_json(marker, ready)

            sentinel = root / "worker-finished"
            worker = [
                sys.executable,
                "-c",
                "import pathlib,time; time.sleep(.35); pathlib.Path(%r).write_text('done')"
                % str(sentinel),
            ]
            result = subprocess.run(
                self.publication_command(root, task, attempt, worker),
                capture_output=True,
                text=True,
                timeout=8,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(sentinel.exists())
            payload = json.loads((attempt / "supervisor-result.json").read_text())
            self.assertFalse(payload["publication_requested"])
            self.assertTrue(
                any("exact full Git object id" in reason for reason in payload["handoff_ready"]["reasons"])
            )

    def test_machine_supervisor_still_accepts_legacy_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "tasks" / "T001"
            attempt = task / "attempts" / "A001"
            attempt.mkdir(parents=True)
            self.write_json(
                task / "STATUS.json",
                {
                    "artifact_protocol_version": 1,
                    "task_id": "T001",
                    "state": "planning",
                    "current_attempt_id": "A001",
                },
            )
            self.write_json(
                attempt / "ATTEMPT.json",
                {"attempt_id": "A001", "state": "running", "phase": "planning"},
            )
            self.write_json(
                task / "HANDOFF.json",
                {"requested_state": "strategy_review", "strategy_sha256": "abc"},
            )
            write_completion(
                task,
                attempt_id="A001",
                phase="planning",
                requested_state="strategy_review",
                strategy_sha256="abc",
            )
            command = self.publication_command(
                root,
                task,
                attempt,
                ["/bin/sh", "-c", "sleep 30 & wait"],
                protocol_version=1,
            )
            result = subprocess.run(command, capture_output=True, text=True, timeout=8)
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads((attempt / "supervisor-result.json").read_text())
            self.assertTrue(payload["publication_requested"])
            self.assertTrue(payload["completion"]["valid"])


if __name__ == "__main__":
    unittest.main()
