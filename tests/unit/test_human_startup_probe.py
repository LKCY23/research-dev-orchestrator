import unittest

from backend_startup import classify_human_startup
from human_startup_probe import waiting_reason


class HumanStartupProbeTests(unittest.TestCase):
    def test_detects_workspace_trust_gate(self):
        self.assertIsNotNone(waiting_reason("Do you trust this folder? [y/N]"))

    def test_authentication_gate_is_fatal(self):
        assessment = classify_human_startup(
            "claude-code",
            "Authentication required. Sign in to continue.",
        )
        self.assertEqual("failed", assessment["kind"])
        self.assertEqual("authentication_required", assessment["failure"]["code"])

    def test_detects_claude_bypass_confirmation_as_fatal(self):
        assessment = classify_human_startup(
            "claude-code",
            "Claude Code running in Bypass Permissions mode\nYes, I accept",
        )
        self.assertEqual("failed", assessment["kind"])
        self.assertEqual(
            "permission_confirmation_required",
            assessment["failure"]["code"],
        )

    def test_detects_missing_session_as_recoverable_startup_failure(self):
        assessment = classify_human_startup(
            "claude-code",
            "No conversation found with session ID: 11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual("failed", assessment["kind"])
        self.assertEqual("session_not_found", assessment["failure"]["code"])
        self.assertTrue(assessment["failure"]["recoverable_resume_failure"])

    def test_normal_tui_output_is_not_waiting(self):
        self.assertIsNone(waiting_reason("Claude Code\n> working on task"))


if __name__ == "__main__":
    unittest.main()
