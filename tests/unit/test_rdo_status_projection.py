import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import rdo


class RdoStatusProjectionTests(unittest.TestCase):
    def test_status_action_marks_raw_v2_result_fields_non_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "T001"
            task.mkdir()
            (task / "STATUS.json").write_text(
                json.dumps(
                    {
                        "task_id": "T001",
                        "artifact_protocol_version": 2,
                        "profile": "direct",
                        "state": "pending",
                        "owner": "",
                        "current_attempt_id": None,
                        "summary": "stale result",
                        "evidence": {"passed": True},
                    }
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    0,
                    rdo.status_action(argparse.Namespace(task_dir=str(task))),
                )

            payload = json.loads(output.getvalue())
            self.assertEqual("stale result", payload["status"]["summary"])
            projection = payload["projection"]
            self.assertEqual("unpublished", projection["publication"]["state"])
            self.assertEqual("", projection["display"]["summary"])
            self.assertFalse(
                projection["compatibility"]["status_summary_authoritative"]
            )
            self.assertFalse(
                projection["compatibility"]["status_evidence_authoritative"]
            )


if __name__ == "__main__":
    unittest.main()
