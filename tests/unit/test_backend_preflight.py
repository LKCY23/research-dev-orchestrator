import subprocess
import unittest
from unittest import mock

from backend_preflight import auth_state, preflight


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


if __name__ == "__main__":
    unittest.main()
