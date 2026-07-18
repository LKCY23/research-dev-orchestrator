import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from process_test_support import require_process_integration
from check_broker import (
    BROKER_ATTEMPT_ENV,
    BROKER_DIR_ENV,
    BROKER_INSTANCE_ENV,
    CheckBrokerServer,
    broker_directory_for_attempt,
    run_brokered,
)
from supervisor import _process_table


ROOT = Path(__file__).resolve().parents[2]
MACHINE_SUPERVISOR = ROOT / "scripts" / "machine_attempt_supervisor.py"


class CheckBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        if self._testMethodName != "test_declared_broker_identity_mismatch_fails_closed":
            require_process_integration()

    def run_with_broker(
        self,
        argv: list[str],
        *,
        timeout_seconds: float = 2,
    ):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        attempt = root / "tasks" / "T001" / "attempts" / "A001"
        (attempt / "runtime").mkdir(parents=True)
        server = CheckBrokerServer(attempt / "runtime", attempt.name)
        result: list[object] = []
        failure: list[BaseException] = []

        def client() -> None:
            try:
                result.append(
                    run_brokered(
                        server.directory,
                        attempt_id=attempt.name,
                        task_id="T001",
                        task_inputs_sha256="a" * 64,
                        check_id="unit",
                        argv=argv,
                        timeout_seconds=timeout_seconds,
                        cwd=root,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                failure.append(exc)

        with patch.dict(os.environ, server.environment(), clear=False):
            self.assertEqual(server.directory, broker_directory_for_attempt(attempt))
            thread = threading.Thread(target=client)
            thread.start()
            deadline = time.monotonic() + timeout_seconds + 5
            while thread.is_alive() and time.monotonic() < deadline:
                server.poll(os.getpid(), _process_table())
                time.sleep(0.02)
            thread.join(timeout=1)
        if failure:
            raise failure[0]
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(result))
        return temporary, root, result[0]

    def test_success_uses_outer_cleanup_receipt(self):
        temporary, _root, result = self.run_with_broker(["/bin/sh", "-c", "exit 0"])
        try:
            self.assertEqual(0, result.exit_code)
            self.assertTrue(result.cleanup_verified)
            self.assertEqual((), result.surviving_pids)
        finally:
            temporary.cleanup()

    def test_timeout_is_killed_by_outer_supervisor(self):
        temporary, _root, result = self.run_with_broker(
            ["/bin/sh", "-c", "sleep 30 & wait"],
            timeout_seconds=0.15,
        )
        try:
            self.assertEqual(124, result.exit_code)
            self.assertTrue(result.timed_out)
            self.assertTrue(result.cleanup_verified)
            self.assertEqual((), result.surviving_pids)
        finally:
            temporary.cleanup()

    def test_detached_child_cannot_write_after_parent_exit(self):
        with tempfile.TemporaryDirectory() as temporary_path:
            sentinel = Path(temporary_path) / "late.txt"
            child = (
                "import pathlib,time; time.sleep(0.7); "
                f"pathlib.Path({str(sentinel)!r}).write_text('late')"
            )
            parent = (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)"
            )
            temporary, _root, result = self.run_with_broker(
                [sys.executable, "-c", parent]
            )
            try:
                self.assertEqual(0, result.exit_code)
                self.assertTrue(result.cleanup_verified)
                time.sleep(0.85)
                self.assertFalse(sentinel.exists())
            finally:
                temporary.cleanup()

    def test_declared_broker_identity_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt = root / "tasks" / "T001" / "attempts" / "A001"
            (attempt / "runtime").mkdir(parents=True)
            server = CheckBrokerServer(attempt / "runtime", attempt.name)
            environment = {
                BROKER_DIR_ENV: str(server.directory),
                BROKER_ATTEMPT_ENV: "A999",
                BROKER_INSTANCE_ENV: server.instance_id,
            }
            with patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                    broker_directory_for_attempt(attempt)

    def test_machine_attempt_supervisor_serves_worker_broker_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "tasks" / "T001"
            attempt = task / "attempts" / "A001"
            (attempt / "runtime").mkdir(parents=True)
            prompt = root / "prompt.md"
            prompt.write_text("test\n", encoding="utf-8")
            output = root / "result.json"
            worker = (
                "import dataclasses,json,os,pathlib,subprocess; "
                "from check_broker import broker_directory_for_attempt,run_brokered; "
                f"attempt=pathlib.Path({str(attempt)!r}); "
                "broker=broker_directory_for_attempt(attempt); "
                "result=run_brokered(broker,attempt_id='A001',task_id='T001',"
                "task_inputs_sha256='a'*64,check_id='unit',"
                "argv=['/bin/sh','-c','exit 0'],timeout_seconds=2,cwd=pathlib.Path('.'),"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"pathlib.Path({str(output)!r}).write_text(json.dumps(dataclasses.asdict(result)))"
            )
            command = [
                sys.executable,
                str(MACHINE_SUPERVISOR),
                "--backend",
                "codex",
                "--argv-json",
                json.dumps([sys.executable, "-c", worker]),
                "--environment-json",
                json.dumps({"PYTHONPATH": str(ROOT / "scripts")}),
                "--cwd",
                str(root),
                "--prompt-path",
                str(prompt),
                "--prompt-transport",
                "arg",
                "--startup-timeout-seconds",
                "1",
                "--timeout-seconds",
                "5",
                "--startup-result",
                str(attempt / "runtime" / "STARTUP.json"),
                "--supervisor-result",
                str(attempt / "supervisor-result.json"),
                "--supervisor-state",
                str(attempt / "runtime" / "supervisor.json"),
                "--transcript",
                str(attempt / "runtime" / "transcript.log"),
                "--custom-command",
                "--artifact-protocol-version",
                "2",
                "--task-dir",
                str(task),
                "--attempt-id",
                attempt.name,
                "--publication-path",
                str(attempt / "runtime" / "HANDOFF_READY.json"),
                "--deadline-path",
                str(attempt / "runtime" / "DEADLINE.json"),
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(
                0,
                completed.returncode,
                completed.stderr
                + completed.stdout
                + (attempt / "runtime" / "transcript.log").read_text(
                    encoding="utf-8", errors="replace"
                ),
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(0, payload["exit_code"])
            self.assertTrue(payload["cleanup_verified"])


if __name__ == "__main__":
    unittest.main()
