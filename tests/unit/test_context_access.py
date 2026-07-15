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
        (self.worktree / "docs" / "DESIGN.md").write_text("# Design\n" + ("detail\n" * 3000), encoding="utf-8")

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


if __name__ == "__main__":
    unittest.main()
