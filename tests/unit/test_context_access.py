import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from protocol import write_json
from read_policy import compile_read_policy, evaluate_read


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


class ContextAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.task = self.root / "task"
        self.task.mkdir()
        (self.task / "CONTEXT.md").write_text("# Context\n", encoding="utf-8")
        self.policy = compile_read_policy(
            repo_root=self.root,
            task_dir=self.task,
            status={"worktree": "worktree"},
            execution_policy={
                "allowed_paths": ["src"],
                "read_paths": ["src"],
                "forbidden_paths": [],
                "context_sources": ["docs/DESIGN.md"],
            },
        )
        self.worktree = self.root / "worktree"

    def tearDown(self):
        self.temporary.cleanup()

    def create_sources(self):
        (self.worktree / "src").mkdir(parents=True)
        (self.worktree / "docs").mkdir()
        (self.worktree / "src" / "small.py").write_text("print('ok')\n", encoding="utf-8")
        (self.worktree / "docs" / "DESIGN.md").write_text(
            "# Design\n" + ("detail\n" * 3000) + "\n## Appendix\nextra\n",
            encoding="utf-8",
        )

    def test_policy_context_sources_survive_pre_worktree_compilation(self):
        self.assertEqual(self.policy["context_sources"], ["docs/DESIGN.md"])

    def test_context_markdown_does_not_grant_visibility(self):
        (self.task / "CONTEXT.md").write_text(
            "# Context\n\n## Source Index\n\n- `private.txt`\n", encoding="utf-8"
        )
        policy = compile_read_policy(
            repo_root=self.root,
            task_dir=self.task,
            status={"worktree": "worktree"},
            execution_policy={
                "allowed_paths": ["src"], "read_paths": ["src"],
                "forbidden_paths": [], "context_sources": [],
            },
        )
        self.assertEqual(policy["context_sources"], [])

    def test_large_indexed_markdown_requires_bounded_read_but_not_search(self):
        self.create_sources()
        source = str(self.worktree / "docs" / "DESIGN.md")
        self.assertIn("offset/limit", evaluate_read(self.policy, {"file_path": source}, "Read"))
        self.assertIsNone(evaluate_read(self.policy, {"file_path": source, "limit": 50}, "Read"))
        self.assertIsNone(evaluate_read(self.policy, {"file_path": source}, "Grep"))

    def test_path_outside_read_scope_and_source_index_is_denied(self):
        self.create_sources()
        hidden = self.worktree / "private.txt"
        hidden.write_text("secret\n", encoding="utf-8")
        self.assertIn("outside task read_paths", evaluate_read(
            self.policy, {"file_path": str(hidden)}, "Read"
        ))

    def test_pathless_native_search_is_not_turned_into_a_root_read(self):
        self.create_sources()
        self.assertIsNone(evaluate_read(self.policy, {}, "Grep"))
        self.assertIsNone(evaluate_read(self.policy, {}, "Glob"))

    def test_broker_index_catalog_is_bounded_and_headings_are_on_demand(self):
        self.create_sources()
        runtime = self.root / "runtime"
        runtime.mkdir()
        write_json(runtime / "READ_POLICY.json", self.policy)
        catalog = subprocess.run(
            [sys.executable, str(SCRIPTS / "context_broker.py"),
             "--policy", str(runtime / "READ_POLICY.json"), "index"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        catalog_payload = json.loads(catalog.stdout)
        self.assertNotIn("headings", catalog_payload["sources"][0])
        selected = subprocess.run(
            [sys.executable, str(SCRIPTS / "context_broker.py"),
             "--policy", str(runtime / "READ_POLICY.json"), "index",
             "--source", "docs/DESIGN.md", "--limit", "1"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        selected_payload = json.loads(selected.stdout)["sources"][0]
        self.assertEqual(len(selected_payload["headings"]), 1)
        requests = [
            json.loads(line)
            for line in (runtime / "CONTEXT_REQUESTS.ndjson").read_text().splitlines()
        ]
        self.assertTrue(requests[-1]["result_truncated"])
        self.assertEqual(len(selected.stdout.encode("utf-8")), requests[-1]["result_bytes"])
        section = subprocess.run(
            [sys.executable, str(SCRIPTS / "context_broker.py"),
             "--policy", str(runtime / "READ_POLICY.json"), "get",
             "--source", "docs/DESIGN.md", "--section", "Design",
             "--question", "What does this section say?"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        section_payload = json.loads(section.stdout)
        requests = [
            json.loads(line)
            for line in (runtime / "CONTEXT_REQUESTS.ndjson").read_text().splitlines()
        ]
        self.assertEqual(1, requests[-1]["schema_version"])
        self.assertEqual(
            len(section_payload["content"].encode("utf-8")),
            requests[-1]["result_content_bytes"],
        )
        self.assertEqual(len(section.stdout.encode("utf-8")), requests[-1]["result_bytes"])
        self.assertTrue(requests[-1]["result_truncated"])

    def test_opencode_adapter_normalizes_file_path_and_denies(self):
        self.create_sources()
        runtime = self.root / "runtime"
        runtime.mkdir()
        write_json(runtime / "READ_POLICY.json", self.policy)
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "read_policy_hook.py"),
                "--runtime-dir",
                str(runtime),
                "--backend",
                "opencode",
                "--format",
                "decision",
            ],
            input=json.dumps({
                "tool_name": "read",
                "tool_input": {"filePath": str(self.worktree / "docs" / "DESIGN.md")},
            }),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertEqual(json.loads(completed.stdout)["decision"], "deny")
        event = json.loads((runtime / "CONTEXT_ACCESS.ndjson").read_text().strip())
        self.assertEqual("context_access", event["event"])
        self.assertEqual(1, event["schema_version"])
        self.assertEqual("opencode", event["backend"])
        self.assertEqual("Read", event["operation"])
        self.assertEqual("docs/DESIGN.md", event["path"])
        self.assertFalse(event["bounded"])
        self.assertEqual("deny", event["decision"])
        self.assertEqual("native_tool", event["coverage"])
        self.assertEqual(
            (self.worktree / "docs" / "DESIGN.md").stat().st_size,
            event["source_size_bytes"],
        )
        self.assertIsNone(event["offset"])
        self.assertIsNone(event["limit"])

    def test_native_pathless_search_records_worktree_scope(self):
        self.create_sources()
        runtime = self.root / "runtime"
        runtime.mkdir()
        write_json(runtime / "READ_POLICY.json", self.policy)
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "read_policy_hook.py"),
                "--runtime-dir",
                str(runtime),
                "--backend",
                "claude-code",
                "--format",
                "decision",
            ],
            input=json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "needle"}}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertEqual("allow", json.loads(completed.stdout)["decision"])
        event = json.loads((runtime / "CONTEXT_ACCESS.ndjson").read_text().strip())
        self.assertEqual(".", event["scope"])
        self.assertTrue(event["bounded"])
        self.assertEqual("allow", event["decision"])
        self.assertIsNone(event["source_size_bytes"])

    def test_telemetry_failure_does_not_replace_allow_decision(self):
        self.create_sources()
        runtime = self.root / "runtime"
        runtime.mkdir()
        write_json(runtime / "READ_POLICY.json", self.policy)
        (runtime / "CONTEXT_ACCESS.ndjson").mkdir()
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "read_policy_hook.py"),
                "--runtime-dir", str(runtime),
                "--backend", "opencode",
                "--format", "decision",
            ],
            input=json.dumps({
                "tool_name": "read",
                "tool_input": {"filePath": "src/small.py", "limit": 5},
            }),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertEqual("allow", json.loads(completed.stdout)["decision"])
        self.assertIn("telemetry append failed", completed.stderr)

    def test_denied_outside_path_is_redacted_in_telemetry(self):
        self.create_sources()
        outside = self.root / "private.txt"
        outside.write_text("private\n", encoding="utf-8")
        runtime = self.root / "runtime"
        runtime.mkdir()
        write_json(runtime / "READ_POLICY.json", self.policy)
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "read_policy_hook.py"),
                "--runtime-dir", str(runtime),
                "--backend", "opencode",
                "--format", "decision",
            ],
            input=json.dumps({
                "tool_name": "read",
                "tool_input": {"filePath": str(outside)},
            }),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertEqual("deny", json.loads(completed.stdout)["decision"])
        raw = (runtime / "CONTEXT_ACCESS.ndjson").read_text(encoding="utf-8")
        event = json.loads(raw)
        self.assertEqual("outside_worktree", event["path"])
        self.assertNotIn(str(outside), raw)

    def test_codex_bash_adapter_is_explicitly_best_effort(self):
        self.create_sources()
        runtime = self.root / "runtime"
        runtime.mkdir()
        write_json(runtime / "READ_POLICY.json", self.policy)
        env = {**os.environ, "RDO_BACKEND_PROFILE": str(runtime / "BACKEND_PROFILE.json")}
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "codex_read_policy_hook.py")],
            input=json.dumps({
                "tool_name": "Bash",
                "tool_input": {"command": "cat docs/DESIGN.md"},
                "cwd": str(self.worktree),
            }),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(
            payload["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        event = json.loads((runtime / "CONTEXT_ACCESS.ndjson").read_text().strip())
        self.assertEqual("codex", event["backend"])
        self.assertEqual("docs/DESIGN.md", event["path"])
        self.assertEqual("deny", event["decision"])
        self.assertEqual("best_effort", event["coverage"])

    def test_codex_telemetry_failure_does_not_swallow_deny(self):
        self.create_sources()
        runtime = self.root / "runtime"
        runtime.mkdir()
        write_json(runtime / "READ_POLICY.json", self.policy)
        write_json(runtime / "BACKEND_PROFILE.json", {})
        (runtime / "CONTEXT_ACCESS.ndjson").mkdir()
        (runtime / "CONTEXT_HOOK_EVENTS.ndjson").mkdir()
        environment = os.environ.copy()
        environment["RDO_BACKEND_PROFILE"] = str(runtime / "BACKEND_PROFILE.json")
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "codex_read_policy_hook.py")],
            input=json.dumps({
                "tool_name": "Bash",
                "cwd": str(self.worktree),
                "tool_input": {"command": "cat docs/DESIGN.md"},
            }),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(
            "deny", payload["hookSpecificOutput"]["permissionDecision"]
        )
        self.assertIn("telemetry append failed", completed.stderr)
        self.assertIn("diagnostic append failed", completed.stderr)

    def test_codex_pathless_search_preserves_native_worktree_semantics(self):
        self.create_sources()
        runtime = self.root / "runtime"
        runtime.mkdir()
        write_json(runtime / "READ_POLICY.json", self.policy)
        env = {**os.environ, "RDO_BACKEND_PROFILE": str(runtime / "BACKEND_PROFILE.json")}
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "codex_read_policy_hook.py")],
            input=json.dumps({
                "tool_name": "Bash",
                "tool_input": {"command": "rg needle"},
                "cwd": str(self.worktree),
            }),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        self.assertEqual("", completed.stdout)
        events = [
            json.loads(line)
            for line in (runtime / "CONTEXT_HOOK_EVENTS.ndjson").read_text().splitlines()
        ]
        self.assertEqual("shell_read_classified", events[-1]["event"])
        self.assertEqual(1, events[-1]["classified_segments"])
        access = json.loads((runtime / "CONTEXT_ACCESS.ndjson").read_text().strip())
        self.assertEqual("Grep", access["operation"])
        self.assertEqual(".", access["scope"])
        self.assertTrue(access["bounded"])
        self.assertEqual("allow", access["decision"])
        self.assertEqual("best_effort", access["coverage"])

    def test_concurrent_native_hooks_append_complete_records(self):
        self.create_sources()
        runtime = self.root / "runtime"
        runtime.mkdir()
        write_json(runtime / "READ_POLICY.json", self.policy)
        command = [
            sys.executable,
            str(SCRIPTS / "read_policy_hook.py"),
            "--runtime-dir",
            str(runtime),
            "--backend",
            "opencode",
            "--format",
            "decision",
        ]
        payload = json.dumps({
            "tool_name": "read",
            "tool_input": {"filePath": str(self.worktree / "src" / "small.py"), "limit": 5},
        })
        processes = [
            subprocess.Popen(
                command,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(16)
        ]
        for process in processes:
            assert process.stdin is not None
            process.stdin.write(payload)
            process.stdin.close()
            process.stdin = None
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(0, process.returncode, stderr)
            self.assertEqual("allow", json.loads(stdout)["decision"])
        records = [
            json.loads(line)
            for line in (runtime / "CONTEXT_ACCESS.ndjson").read_text().splitlines()
        ]
        self.assertEqual(16, len(records))
        self.assertTrue(all(record["path"] == "src/small.py" for record in records))
        self.assertTrue(all(record["bounded"] for record in records))


if __name__ == "__main__":
    unittest.main()
