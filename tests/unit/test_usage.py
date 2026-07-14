import json
import tempfile
import unittest
from pathlib import Path

from usage import UsageSupervisor, normalize_usage_event


class UsageTests(unittest.TestCase):
    def test_opencode_completed_message_is_normalized_once(self):
        event = {
            "type": "message.updated",
            "properties": {"info": {
                "id": "msg-1", "role": "assistant", "time": {"completed": 123},
                "tokens": {"input": 100, "output": 20, "cache": {"read": 50}},
                "cost": 0.25,
            }},
        }
        record = normalize_usage_event("opencode", event)
        self.assertEqual(record["model_turns"], 1)
        self.assertEqual(record["context_tokens"], 170)
        self.assertEqual(record["cost_usd"], 0.25)

    def test_supervisor_records_and_enforces_turn_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            supervisor = UsageSupervisor(runtime, "opencode", {"max_model_turns": 1})
            event = lambda identifier: {
                "type": "message.updated",
                "properties": {"info": {
                    "id": identifier, "role": "assistant", "time": {"completed": 1},
                    "tokens": {"input": 10, "output": 2},
                }},
            }
            self.assertIsNone(supervisor.observe(event("one")))
            self.assertIsNone(supervisor.observe(event("one")))
            reason = supervisor.observe(event("two"))
            self.assertIn("max_model_turns", reason)
            records = [json.loads(line) for line in (runtime / "USAGE.ndjson").read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertTrue((runtime / "VIOLATIONS.ndjson").exists())

    def test_protocol_progress_resets_no_progress_turns(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            runtime.mkdir()
            supervisor = UsageSupervisor(runtime, "opencode", {"max_no_progress_turns": 1})
            def event(identifier):
                return {"type": "message.updated", "properties": {"info": {
                    "id": identifier, "role": "assistant", "time": {"completed": 1},
                    "tokens": {"input": 1, "output": 1},
                }}}
            self.assertIsNone(supervisor.observe(event("one")))
            (runtime / "WORKFLOWS.ndjson").write_text('{"event":"workflow_started"}\n')
            self.assertIsNone(supervisor.observe(event("two")))
            self.assertEqual(supervisor.summary()["no_progress_turns"], 0)


if __name__ == "__main__":
    unittest.main()
