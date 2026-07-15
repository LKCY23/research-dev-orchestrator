import unittest
from pathlib import Path

from render_dashboard import render_task_card


class RenderDashboardV2Tests(unittest.TestCase):
    def test_v2_card_links_attempt_local_truth_not_task_root_markdown(self):
        run_dir = Path("/repo/.agent-collab/runs/run")
        task = {
            "task_id": "T001",
            "state": "review",
            "current_attempt_id": "A001",
            "artifact_resolution": {
                "valid": True,
                "protocol": "v2",
                "artifact_protocol_version": 2,
                "artifact_refs": {
                    "task_inputs": str(run_dir / "tasks/T001/attempts/A001/TASK_INPUTS.json"),
                    "evidence": str(run_dir / "tasks/T001/attempts/A001/EVIDENCE.json"),
                    "handoff": str(run_dir / "tasks/T001/attempts/A001/HANDOFF.json"),
                    "handoff_ready": str(
                        run_dir / "tasks/T001/attempts/A001/runtime/HANDOFF_READY.json"
                    ),
                },
            },
        }
        rendered = render_task_card(run_dir, task)
        self.assertIn("tasks/T001/attempts/A001/EVIDENCE.json", rendered)
        self.assertIn("tasks/T001/attempts/A001/runtime/HANDOFF_READY.json", rendered)
        self.assertNotIn("tasks/T001/EVIDENCE.md", rendered)
        self.assertNotIn("tasks/T001/HANDOFF.md", rendered)

    def test_unknown_or_invalid_protocol_is_visible_and_gets_no_legacy_fallback_links(self):
        run_dir = Path("/repo/.agent-collab/runs/run")
        task = {
            "task_id": "T001",
            "state": "review",
            "artifact_resolution": {
                "valid": False,
                "protocol": 3,
                "error": "unsupported STATUS.artifact_protocol_version: 3",
            },
        }
        rendered = render_task_card(run_dir, task)
        self.assertIn("artifact invalid", rendered)
        self.assertIn("unsupported STATUS.artifact_protocol_version", rendered)
        self.assertNotIn("tasks/T001/EVIDENCE.md", rendered)


if __name__ == "__main__":
    unittest.main()
