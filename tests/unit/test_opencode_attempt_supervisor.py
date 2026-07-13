import tempfile
import unittest
from pathlib import Path

from opencode_attempt_supervisor import Guardian, opencode_config


class FakeApi:
    def __init__(self):
        self.calls = []

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload))
        return True


class OpenCodeGuardianTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name)
        self.api = FakeApi()
        self.guardian = Guardian(
            api=self.api,
            runtime=self.runtime,
            root_session="ses_root",
            allowed_types={"explore", "general"},
            max_parallel=1,
            max_depth=1,
            permission_mode="auto",
            emit_events=False,
            stop_when_root_idle=True,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def permission(self, request_id, agent, session="ses_root"):
        self.guardian.handle_permission({
            "id": request_id,
            "sessionID": session,
            "permission": "task",
            "patterns": [agent],
            "metadata": {"subagent_type": agent},
        })

    def test_no_cumulative_limit_after_child_stops(self):
        self.permission("per_1", "explore")
        self.guardian.handle_session_created({
            "sessionID": "ses_child_1",
            "info": {"id": "ses_child_1", "parentID": "ses_root", "agent": "explore"},
        })
        self.guardian.handle({"type": "session.idle", "properties": {"sessionID": "ses_child_1"}})
        self.permission("per_2", "explore")
        replies = [call[2]["reply"] for call in self.api.calls if "/permission/" in call[1]]
        self.assertEqual(replies, ["once", "once"])

    def test_rejects_disallowed_parallel_and_nested_requests(self):
        self.permission("per_allowed", "explore")
        self.permission("per_parallel", "general")
        self.permission("per_type", "custom")
        self.permission("per_nested", "explore", session="ses_child")
        replies = [call[2]["reply"] for call in self.api.calls if "/permission/" in call[1]]
        self.assertEqual(replies, ["once", "reject", "reject", "reject"])

    def test_unapproved_child_is_aborted_and_recorded(self):
        self.guardian.handle_session_created({
            "sessionID": "ses_unapproved",
            "info": {"id": "ses_unapproved", "parentID": "ses_root", "agent": "explore"},
        })
        self.assertIn(("POST", "/session/ses_unapproved/abort", None), self.api.calls)
        self.assertTrue((self.runtime / "VIOLATIONS.ndjson").exists())

    def test_config_asks_for_root_tasks_and_denies_nested_tasks(self):
        config = opencode_config({
            "native_subagents_enabled": True,
            "allowed_subagent_types": ["explore", "general"],
        })
        self.assertEqual(config["permission"]["task"], {"*": "ask"})
        self.assertEqual(config["agent"]["explore"]["permission"]["task"], "deny")


if __name__ == "__main__":
    unittest.main()
