import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from machine_attempt_supervisor import startup_event


ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = ROOT / "scripts" / "machine_attempt_supervisor.py"


class MachineAttemptSupervisorTests(unittest.TestCase):
    def test_backend_startup_event_decoders(self):
        self.assertEqual(
            startup_event("claude-code", b'{"type":"system","subtype":"init"}'),
            "system/init",
        )
        self.assertEqual(startup_event("codex", b'{"type":"thread.started"}'), "thread.started")
        self.assertEqual(startup_event("kimi-code", b'{"type":"session.start"}'), "session.start")
        self.assertIsNone(startup_event("claude-code", b'{"type":"assistant"}'))
        self.assertIsNone(startup_event("codex", b"not-json"))

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


if __name__ == "__main__":
    unittest.main()
