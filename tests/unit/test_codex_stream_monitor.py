import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from protocol import write_json


class CodexStreamMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name) / "runtime"
        self.runtime.mkdir()
        write_json(self.runtime / "BACKEND_PROFILE.json", {
            "backend_id": "codex",
            "profile_sha256": "test-profile",
            "native_agent_limits": {
                "max_spawns": 1,
                "max_parallel": 1,
                "enforce_max_spawns": True,
            },
        })
        self.monitor = Path(__file__).resolve().parents[2] / "scripts" / "codex_stream_monitor.py"

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def spawn_event(item_id, receiver, status="running"):
        return {
            "type": "item.started",
            "item": {
                "id": item_id,
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "sender_thread_id": "root",
                "receiver_thread_ids": [receiver],
                "agents_states": {receiver: {"status": status, "message": None}},
                "status": "in_progress",
            },
        }

    def run_fake(self, events, *, sleep_after=0):
        source = (
            "import json,time\n"
            f"events={events!r}\n"
            "for event in events:\n"
            " print(json.dumps(event), flush=True)\n"
            f"time.sleep({sleep_after!r})\n"
        )
        return subprocess.run(
            [
                sys.executable,
                str(self.monitor),
                "--runtime-dir",
                str(self.runtime),
                "--",
                sys.executable,
                "-c",
                source,
            ],
            text=True,
            capture_output=True,
            timeout=5,
        )

    def test_passes_through_valid_stream_and_tracks_agent(self):
        event = self.spawn_event("spawn-1", "agent-1")
        result = self.run_fake([event, {**event, "type": "item.completed"}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 2)
        state = json.loads((self.runtime / "AGENTS.json").read_text(encoding="utf-8"))
        self.assertEqual(state["total_requests"], 1)
        self.assertEqual(state["total_starts"], 1)
        self.assertEqual(state["peak_active"], 1)
        self.assertFalse((self.runtime / "VIOLATIONS.ndjson").exists())

    def test_terminates_child_when_spawn_budget_is_exceeded(self):
        started = time.monotonic()
        result = self.run_fake(
            [self.spawn_event("spawn-1", "agent-1"), self.spawn_event("spawn-2", "agent-2")],
            sleep_after=30,
        )
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertLess(time.monotonic() - started, 5)
        violations = [
            json.loads(line)
            for line in (self.runtime / "VIOLATIONS.ndjson").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(violations), 1)
        self.assertTrue(violations[0]["hard"])
        self.assertIn("spawn budget exceeded", violations[0]["reason"])

    def test_invalid_json_stream_is_a_hard_violation(self):
        result = subprocess.run(
            [
                sys.executable,
                str(self.monitor),
                "--runtime-dir",
                str(self.runtime),
                "--",
                "/bin/echo",
                "not-json",
            ],
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 125)
        violation = json.loads(
            (self.runtime / "VIOLATIONS.ndjson").read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertIn("JSONL stream is invalid", violation["reason"])


if __name__ == "__main__":
    unittest.main()
