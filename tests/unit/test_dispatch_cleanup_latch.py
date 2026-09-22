import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DispatchCleanupLatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.attempt = self.root / "attempt"
        (self.attempt / "runtime").mkdir(parents=True)
        (self.root / "status.json").write_text("{}", encoding="utf-8")
        self.lock = self.root / "dispatch-lock"
        source = (ROOT / "scripts/dispatch_claude.sh").read_text(encoding="utf-8")
        helpers, separator, _ = source.partition("\ntrap on_exit EXIT")
        self.assertTrue(separator, "Dispatcher helper boundary changed")
        self.helpers = self.root / "helpers.sh"
        self.helpers.write_text(helpers, encoding="utf-8")
        self.cli = self.root / "protocol.py"
        self.cli.write_text(
            "import json, os, sys\n"
            "if sys.argv[1] == 'terminate-attempt-processes':\n"
            "    code = int(os.environ['TEST_CLEANUP_EXIT'])\n"
            "    print(json.dumps({'terminated': code == 0, 'surviving_pids': []}))\n"
            "    sys.exit(code)\n",
            encoding="utf-8",
        )

    def run_cancel(self, cleanup_exit):
        script = f"""
source {shlex.quote(str(self.helpers))} test-run test-task
ATTEMPT_DIR={shlex.quote(str(self.attempt))}
ATTEMPT_ID=test-attempt
STATUS_PATH={shlex.quote(str(self.root / 'status.json'))}
PROTOCOL_CLI={shlex.quote(str(self.cli))}
EXIT_CODE_FILE="${{ATTEMPT_DIR}}/exit_code"
DISPATCH_LOCK_DIR={shlex.quote(str(self.lock))}
RDO_RUNTIME_BACKEND=tmux
TMUX_WORKER_LAUNCHED=1
DISPATCH_LOCK_ACQUIRED=1
mkdir -p "${{DISPATCH_LOCK_DIR}}"
printf '%s\\n' "${{ATTEMPT_ID}}" > "${{DISPATCH_LOCK_DIR}}/attempt_id"
printf '%s\\n' "$$" > "${{DISPATCH_LOCK_DIR}}/pid"
write_tmux_timeout_diagnostics() {{
  printf '0\\n' > "${{EXIT_CODE_FILE}}"
}}
trap on_exit EXIT
cancel_tmux_wait tmux_wait_timeout
"""
        return subprocess.run(
            ["/bin/bash", "-c", script],
            cwd=self.root,
            env={**os.environ, "TEST_CLEANUP_EXIT": str(cleanup_exit)},
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_late_exit_file_cannot_cancel_required_cleanup(self):
        result = self.run_cancel(0)
        self.assertEqual(5, result.returncode, result.stderr)
        self.assertEqual("0", (self.attempt / "exit_code").read_text().strip())
        cleanup = json.loads((self.attempt / "runtime/CLEANUP.json").read_text())
        self.assertTrue(cleanup["terminated"])
        self.assertFalse(self.lock.exists())

    def test_unverified_cleanup_keeps_the_dispatch_lock(self):
        result = self.run_cancel(2)
        self.assertEqual(5, result.returncode, result.stderr)
        cleanup = json.loads((self.attempt / "runtime/CLEANUP.json").read_text())
        self.assertFalse(cleanup["terminated"])
        self.assertTrue(self.lock.is_dir())

    def test_supervisor_receipt_must_be_readable_json(self):
        receipt = self.attempt / "supervisor-result.json"
        for content, expected in ((None, 1), ("broken", 1), ("[]", 1), ("{}", 0)):
            with self.subTest(content=content):
                if content is not None:
                    receipt.write_text(content, encoding="utf-8")
                result = subprocess.run(
                    ["/bin/bash", "-c", (
                        f"source {shlex.quote(str(self.helpers))} test-run test-task; "
                        f"SUPERVISOR_RESULT={shlex.quote(str(receipt))}; "
                        "supervisor_result_readable"
                    )],
                    cwd=self.root,
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(expected, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
