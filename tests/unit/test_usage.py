import json
import tempfile
import unittest
from pathlib import Path

from usage import UsageSupervisor, normalize_usage_event


class UsageTests(unittest.TestCase):
    def test_codex_terminal_usage_preserves_public_metric_boundary(self):
        event = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 32161,
                "cached_input_tokens": 1920,
                "output_tokens": 47,
            },
        }
        record = normalize_usage_event("codex", event)
        self.assertEqual(32161, record["input_tokens"])
        self.assertEqual(1920, record["cached_input_tokens"])
        self.assertEqual(47, record["output_tokens"])
        self.assertIsNone(record["model_turns"])
        self.assertIsNone(record["context_tokens"])
        self.assertEqual(
            ["input_tokens", "output_tokens"],
            record["observed_metrics"],
        )

    def test_missing_codex_terminal_usage_is_not_reported_as_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = UsageSupervisor(Path(temporary), "codex", {})
            self.assertIsNone(supervisor.observe({"type": "turn.completed"}))
            summary = supervisor.summary()
            self.assertEqual([], summary["observed_metrics"])
            self.assertTrue(all(value is None for value in summary["totals"].values()))

    def test_codex_terminal_token_budget_is_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = UsageSupervisor(
                Path(temporary), "codex", {"max_input_tokens": 10}
            )
            reason = supervisor.observe({
                "type": "turn.completed",
                "usage": {"input_tokens": 11, "output_tokens": 1},
            })
            self.assertIn("max_input_tokens", reason)
            self.assertEqual(11, supervisor.summary()["totals"]["input_tokens"])

    def test_missing_required_codex_observation_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            supervisor = UsageSupervisor(
                runtime, "codex", {"max_input_tokens": 100}
            )
            supervisor.observe({"type": "turn.completed"})
            reason = supervisor.require_budget_observations()
            self.assertIn("input_tokens", reason)
            violation = json.loads(
                (runtime / "VIOLATIONS.ndjson").read_text().strip()
            )
            self.assertEqual("usage_observation_missing", violation["event"])

    def test_non_codex_empty_summary_remains_eager_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            summary = UsageSupervisor(
                Path(temporary), "claude-code", {}
            ).summary()
            self.assertEqual(0, summary["totals"]["model_turns"])
            self.assertEqual(0, summary["totals"]["input_tokens"])
            self.assertEqual(0, summary["totals"]["max_context_tokens"])

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
