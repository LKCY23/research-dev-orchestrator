import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import rdo
from tmux_lifecycle import (
    TmuxLifecycleError,
    build_tmux_inventory,
    kill_live_tmux_session,
    list_live_tmux_sessions,
    record_tmux_session_identity,
)


class TmuxLifecycleTests(unittest.TestCase):
    def write_task(
        self,
        root: Path,
        *,
        run_id: str,
        task_id: str,
        session_name: str,
        task_state: str,
        attempt_state: str,
        outcome: str | None,
        clean_terminal: bool = False,
        retained_lock: bool = False,
    ) -> Path:
        task = root / ".agent-collab" / "runs" / run_id / "tasks" / task_id
        attempt = task / "attempts" / "A001"
        (attempt / "runtime").mkdir(parents=True)
        (task / "STATUS.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "state": task_state,
                    "current_attempt_id": "A001",
                }
            ),
            encoding="utf-8",
        )
        payload = {
            "task_id": task_id,
            "attempt_id": "A001",
            "state": attempt_state,
            "outcome": outcome,
            "handoff_valid": clean_terminal,
            "handoff_state": "review" if clean_terminal else None,
            "exit_code": 0 if clean_terminal else None,
            "ended_at": "2026-07-17T06:00:00Z" if clean_terminal else None,
            "runtime": {
                "backend": "tmux",
                "tmux_session": session_name,
            },
        }
        (attempt / "ATTEMPT.json").write_text(json.dumps(payload), encoding="utf-8")
        if clean_terminal:
            (attempt / "supervisor-result.json").write_text(
                json.dumps({"cleanup_verified": True, "surviving_pids": []}),
                encoding="utf-8",
            )
            (attempt / "runtime" / "transcript.log").write_text(
                "preserved transcript\n", encoding="utf-8"
            )
            (attempt / "runtime" / "TMUX_SESSION.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": run_id,
                        "task_id": task_id,
                        "attempt_id": "A001",
                        "session_id": "$1",
                        "created_at_epoch": 1,
                        "session_name": session_name,
                    }
                ),
                encoding="utf-8",
            )
        if retained_lock:
            lock = task / ".dispatch-lock"
            lock.mkdir()
            (lock / "attempt_id").write_text("A001\n", encoding="utf-8")
            (lock / "tmux_session").write_text(
                f"{session_name}\n", encoding="utf-8"
            )
        return task

    @staticmethod
    def live(session_id: str, session_name: str, created: int) -> dict:
        return {
            "session_id": session_id,
            "session_name": session_name,
            "created_at_epoch": created,
        }

    def test_inventory_classifies_only_clean_terminal_sessions_as_prunable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_task(
                root,
                run_id="run-1",
                task_id="T001-clean",
                session_name="rdo-clean",
                task_state="review",
                attempt_state="completed",
                outcome="completed",
                clean_terminal=True,
            )
            self.write_task(
                root,
                run_id="run-1",
                task_id="T002-active",
                session_name="rdo-active",
                task_state="running",
                attempt_state="running",
                outcome=None,
                retained_lock=True,
            )
            self.write_task(
                root,
                run_id="run-1",
                task_id="T003-failed",
                session_name="rdo-failed",
                task_state="blocked",
                attempt_state="invalid_handoff",
                outcome="execution_failed",
                retained_lock=True,
            )
            inventory = build_tmux_inventory(
                root,
                [
                    self.live("$1", "rdo-clean", 1),
                    self.live("$2", "rdo-active", 2),
                    self.live("$3", "rdo-failed", 3),
                    self.live("$4", "unrelated", 4),
                ],
            )

            rows = {row["session_name"]: row for row in inventory["sessions"]}
            self.assertEqual("terminal_prunable", rows["rdo-clean"]["classification"])
            self.assertTrue(rows["rdo-clean"]["prunable"])
            self.assertEqual("active", rows["rdo-active"]["classification"])
            self.assertEqual(
                "attention_required", rows["rdo-failed"]["classification"]
            )
            self.assertEqual(["unrelated"], [row["session_name"] for row in inventory["untracked_sessions"]])

    def test_active_and_run_filters_do_not_expose_other_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for run_id, task_id, session_name in (
                ("run-1", "T001", "rdo-one"),
                ("run-2", "T002", "rdo-two"),
            ):
                self.write_task(
                    root,
                    run_id=run_id,
                    task_id=task_id,
                    session_name=session_name,
                    task_state="running",
                    attempt_state="running",
                    outcome=None,
                )
            inventory = build_tmux_inventory(
                root,
                [
                    self.live("$1", "rdo-one", 1),
                    self.live("$2", "rdo-two", 2),
                    self.live("$3", "unrelated", 3),
                ],
                run_id="run-1",
                active_only=True,
            )
            self.assertEqual(["rdo-one"], [row["session_name"] for row in inventory["sessions"]])
            self.assertEqual([], inventory["untracked_sessions"])

    def test_duplicate_artifact_mapping_is_ambiguous_and_not_prunable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for run_id, task_id in (("run-1", "T001"), ("run-2", "T002")):
                self.write_task(
                    root,
                    run_id=run_id,
                    task_id=task_id,
                    session_name="rdo-duplicate",
                    task_state="review",
                    attempt_state="completed",
                    outcome="completed",
                    clean_terminal=True,
                )
            inventory = build_tmux_inventory(
                root,
                [self.live("$1", "rdo-duplicate", 1)],
                run_id="run-1",
            )
            row = inventory["sessions"][0]
            self.assertEqual("ambiguous", row["classification"])
            self.assertFalse(row["prunable"])
            self.assertEqual(2, len(row["references"]))

    def test_reused_session_name_without_matching_receipt_is_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_task(
                root,
                run_id="run-1",
                task_id="T001",
                session_name="rdo-reused",
                task_state="review",
                attempt_state="completed",
                outcome="completed",
                clean_terminal=True,
            )
            inventory = build_tmux_inventory(
                root, [self.live("$99", "rdo-reused", 99)]
            )
            row = inventory["sessions"][0]
            self.assertEqual("attention_required", row["classification"])
            self.assertFalse(row["prunable"])
            self.assertIn("does not match", row["reasons"][0])

    def test_kill_revalidates_stable_tmux_identity_and_uses_session_id(self):
        expected = self.live("$7", "rdo-clean", 7)
        with patch(
            "tmux_lifecycle.inspect_live_tmux_session", return_value=expected
        ), patch(
            "tmux_lifecycle.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stderr=""),
        ) as run:
            result = kill_live_tmux_session(expected)
        self.assertEqual("killed", result["status"])
        self.assertEqual(
            ["tmux", "kill-session", "-t", "$7"], run.call_args.args[0]
        )

        changed = self.live("$8", "rdo-clean", 8)
        with patch(
            "tmux_lifecycle.inspect_live_tmux_session", return_value=changed
        ), patch("tmux_lifecycle.subprocess.run") as run:
            result = kill_live_tmux_session(expected)
        self.assertEqual("identity_changed", result["status"])
        run.assert_not_called()

    def test_prune_cli_kills_only_prunable_rows_and_reports_failures(self):
        terminal = {
            **self.live("$1", "rdo-clean", 1),
            "run_id": "run-1",
            "task_id": "T001",
            "attempt_id": "A001",
            "classification": "terminal_prunable",
            "prunable": True,
        }
        active = {
            **self.live("$2", "rdo-active", 2),
            "run_id": "run-1",
            "task_id": "T002",
            "attempt_id": "A001",
            "classification": "active",
            "prunable": False,
        }
        inventory = {
            "repo_root": "/repo",
            "run_filter": None,
            "sessions": [terminal, active],
            "summary": {
                "active": 1,
                "attention_required": 0,
                "ambiguous": 0,
                "untracked_live": 0,
            },
        }
        args = argparse.Namespace(repo_root="/repo", run="", active=False, terminal=True)
        output = io.StringIO()
        with patch("rdo._tmux_inventory", return_value=inventory), patch(
            "rdo.kill_live_tmux_session",
            return_value={"status": "killed", "reason": None},
        ) as kill, contextlib.redirect_stdout(output):
            self.assertEqual(0, rdo.tmux_prune(args))
        kill.assert_called_once_with(terminal)
        payload = json.loads(output.getvalue())
        self.assertEqual(1, payload["summary"]["killed"])
        self.assertEqual(1, payload["summary"]["retained_active"])

        with patch("rdo._tmux_inventory", return_value=inventory), patch(
            "rdo.kill_live_tmux_session",
            return_value={"status": "identity_changed", "reason": "changed"},
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, rdo.tmux_prune(args))

    def test_tmux_query_distinguishes_no_server_from_other_failures(self):
        with patch(
            "tmux_lifecycle.subprocess.run",
            return_value=SimpleNamespace(
                returncode=0, stdout="$1\t17\trdo-one\n", stderr=""
            ),
        ):
            self.assertEqual(
                [self.live("$1", "rdo-one", 17)], list_live_tmux_sessions()
            )
        with patch(
            "tmux_lifecycle.subprocess.run",
            return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="no server running on /tmp/tmux"
            ),
        ):
            self.assertEqual([], list_live_tmux_sessions())
        with patch(
            "tmux_lifecycle.subprocess.run",
            return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="permission denied"
            ),
        ):
            with self.assertRaisesRegex(TmuxLifecycleError, "permission denied"):
                list_live_tmux_sessions()

    def test_dispatch_identity_receipt_is_atomically_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "runtime" / "TMUX_SESSION.json"
            identity = self.live("$5", "rdo-recorded", 5)
            with patch(
                "tmux_lifecycle.inspect_live_tmux_session", return_value=identity
            ):
                payload = record_tmux_session_identity(
                    output,
                    run_id="run-1",
                    task_id="T001",
                    attempt_id="A001",
                    session_name="rdo-recorded",
                )
            self.assertEqual(payload, json.loads(output.read_text(encoding="utf-8")))
            self.assertFalse(output.with_suffix(".json.tmp").exists())

    def test_prune_parser_requires_explicit_terminal_flag(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rdo.build_parser().parse_args(["tmux", "prune", "--repo-root", "/repo"])
        parsed = rdo.build_parser().parse_args(
            ["tmux", "prune", "--repo-root", "/repo", "--terminal"]
        )
        self.assertTrue(parsed.terminal)
        with self.assertRaisesRegex(SystemExit, "explicit --terminal"):
            rdo.tmux_prune(
                argparse.Namespace(
                    repo_root="/repo", run="", active=False, terminal=False
                )
            )


if __name__ == "__main__":
    unittest.main()
