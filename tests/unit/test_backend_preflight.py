import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend_preflight import auth_state, capability_state, preflight
from backend_startup import session_state


class BackendPreflightTests(unittest.TestCase):
    def test_missing_override_executable_is_hard_failure(self):
        result = preflight("claude-code", "rdo-definitely-missing --flag")
        self.assertTrue(result["errors"])
        self.assertIn("executable not found", result["errors"][0])

    @mock.patch("backend_preflight.run_probe")
    def test_claude_auth_status_is_parsed(self, probe):
        probe.return_value = subprocess.CompletedProcess([], 0, '{"loggedIn": true}\n', "")
        state, detail = auth_state("claude-code", "/usr/bin/claude")
        self.assertEqual(state, "authenticated")
        self.assertIn("loggedIn=true", detail)

    @mock.patch("backend_preflight.run_probe")
    @mock.patch("backend_preflight.shutil.which", return_value="/usr/bin/claude")
    def test_unauthenticated_registered_backend_fails(self, _which, probe):
        probe.side_effect = [
            subprocess.CompletedProcess([], 0, "1.2.3\n", ""),
            subprocess.CompletedProcess([], 0, '{"loggedIn": false}\n', ""),
        ]
        result = preflight("claude-code")
        self.assertEqual(result["auth"], "unauthenticated")
        self.assertTrue(result["errors"])

    def test_claude_session_lookup_distinguishes_present_and_missing(self):
        session_id = "11111111-1111-1111-1111-111111111111"
        with tempfile.TemporaryDirectory() as temporary:
            projects = Path(temporary) / "projects" / "project"
            projects.mkdir(parents=True)
            with mock.patch.dict(
                "os.environ",
                {"CLAUDE_CONFIG_DIR": temporary},
                clear=False,
            ):
                self.assertEqual("missing", session_state("claude-code", session_id)[0])
                (projects / f"{session_id}.jsonl").write_text("{}\n")
                self.assertEqual("present", session_state("claude-code", session_id)[0])

    @mock.patch("backend_preflight.run_probe")
    @mock.patch("backend_preflight.shutil.which", return_value="/usr/bin/claude")
    def test_missing_resume_session_requests_full_context_fallback(self, _which, probe):
        probe.side_effect = [
            subprocess.CompletedProcess([], 0, "2.1.185\n", ""),
            subprocess.CompletedProcess([], 0, '{"loggedIn": true}\n', ""),
            subprocess.CompletedProcess([], 0, "Claude help\n", ""),
        ]
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            "os.environ",
            {"CLAUDE_CONFIG_DIR": temporary},
            clear=False,
        ):
            (Path(temporary) / "projects").mkdir()
            result = preflight(
                "claude-code",
                execution_mode="resume",
                session_id="22222222-2222-2222-2222-222222222222",
                cwd=temporary,
            )
        self.assertFalse(result["errors"])
        self.assertEqual("missing", result["resume"]["session_state"])
        self.assertTrue(result["resume"]["fallback_required"])
        self.assertEqual("session_missing", result["resume"]["fallback_reason"])

    @mock.patch("backend_preflight.run_probe")
    def test_codex_capability_probe_matches_selected_io_mode(self, probe):
        probe.return_value = subprocess.CompletedProcess([], 0, "help\n", "")
        capability_state("codex", "/usr/bin/codex", cwd="/tmp/work", io_mode="human")
        self.assertEqual(
            ["/usr/bin/codex", "--cd", "/tmp/work", "resume", "--help"],
            probe.call_args_list[-1].args[0],
        )


if __name__ == "__main__":
    unittest.main()
