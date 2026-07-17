import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rdo
from supervisor import SupervisionAuditResult


class CleanupAuditTests(unittest.TestCase):
    def test_cli_routes_cleanup_audit(self):
        arguments = rdo.build_parser().parse_args(
            ["cleanup", "audit", "--attempt-dir", "/tmp/task/attempts/A001"]
        )
        self.assertIs(rdo.cleanup_audit, arguments.func)

    def fixture(self, root: Path) -> Path:
        task = root / "tasks" / "T001"
        attempt = task / "attempts" / "A001"
        (attempt / "runtime").mkdir(parents=True)
        (task / "STATUS.json").write_text(
            json.dumps(
                {
                    "task_id": "T001",
                    "state": "review",
                    "current_attempt_id": "A001",
                }
            ),
            encoding="utf-8",
        )
        (attempt / "ATTEMPT.json").write_text(
            json.dumps(
                {
                    "task_id": "T001",
                    "attempt_id": "A001",
                    "state": "completed",
                }
            ),
            encoding="utf-8",
        )
        (attempt / "runtime" / "supervisor.json").write_text(
            json.dumps(
                {
                    "state": "completed",
                    "supervision_token": "a" * 32,
                    "cleanup_verified": True,
                    "cleanup_failure_reason": None,
                    "surviving_pids": [],
                    "observed_pids": [101],
                    "observed_pgids": [101],
                }
            ),
            encoding="utf-8",
        )
        return attempt

    def run_audit(self, attempt: Path) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = rdo.cleanup_audit(
                argparse.Namespace(attempt_dir=str(attempt))
            )
        return code, json.loads(output.getvalue())

    def test_empty_observation_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.fixture(Path(temporary))
            before = {
                path.relative_to(attempt.parent.parent): path.read_bytes()
                for path in attempt.parent.parent.rglob("*")
                if path.is_file()
            }
            with patch.object(
                rdo,
                "audit_supervision_token",
                return_value=SupervisionAuditResult(True, None, ()),
            ):
                code, payload = self.run_audit(attempt)
            after = {
                path.relative_to(attempt.parent.parent): path.read_bytes()
                for path in attempt.parent.parent.rglob("*")
                if path.is_file()
            }
            self.assertEqual(0, code)
            self.assertEqual("no_live_processes_observed", payload["status"])
            self.assertEqual(
                "current_token_visible_processes",
                payload["observation_scope"],
            )
            self.assertEqual(before, after)

    def test_live_token_processes_are_reported_without_signalling(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.fixture(Path(temporary))
            with patch.object(
                rdo,
                "audit_supervision_token",
                return_value=SupervisionAuditResult(
                    True,
                    None,
                    ((222, 1, 222),),
                ),
            ):
                code, payload = self.run_audit(attempt)
            self.assertEqual(1, code)
            self.assertEqual("live_processes", payload["status"])
            self.assertEqual(
                [{"pid": 222, "ppid": 1, "pgid": 222}],
                payload["live_processes"],
            )

    def test_process_inspection_failure_is_unknown_not_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.fixture(Path(temporary))
            with patch.object(
                rdo,
                "audit_supervision_token",
                return_value=SupervisionAuditResult(
                    False,
                    "process_table_unavailable",
                    (),
                ),
            ):
                code, payload = self.run_audit(attempt)
            self.assertEqual(126, code)
            self.assertEqual("inspection_unavailable", payload["status"])

    def test_active_attempt_is_ineligible(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.fixture(Path(temporary))
            metadata = json.loads((attempt / "ATTEMPT.json").read_text())
            metadata["state"] = "running"
            (attempt / "ATTEMPT.json").write_text(json.dumps(metadata), encoding="utf-8")
            with patch.object(rdo, "audit_supervision_token") as inspect:
                code, payload = self.run_audit(attempt)
            self.assertEqual(2, code)
            self.assertEqual("ineligible", payload["status"])
            self.assertEqual("attempt_not_completed", payload["reason"])
            inspect.assert_not_called()

    def test_invalid_supervision_token_is_invalid_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = self.fixture(Path(temporary))
            supervisor = attempt / "runtime" / "supervisor.json"
            payload = json.loads(supervisor.read_text())
            payload["supervision_token"] = "invalid"
            supervisor.write_text(json.dumps(payload), encoding="utf-8")
            code, result = self.run_audit(attempt)
            self.assertEqual(2, code)
            self.assertEqual("invalid_evidence", result["status"])


if __name__ == "__main__":
    unittest.main()
