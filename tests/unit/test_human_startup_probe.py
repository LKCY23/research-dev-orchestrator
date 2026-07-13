import unittest

from human_startup_probe import waiting_reason


class HumanStartupProbeTests(unittest.TestCase):
    def test_detects_workspace_trust_gate(self):
        self.assertIsNotNone(waiting_reason("Do you trust this folder? [y/N]"))

    def test_detects_login_gate(self):
        self.assertIsNotNone(waiting_reason("Authentication required. Sign in to continue."))

    def test_normal_tui_output_is_not_waiting(self):
        self.assertIsNone(waiting_reason("Claude Code\n> working on task"))


if __name__ == "__main__":
    unittest.main()
