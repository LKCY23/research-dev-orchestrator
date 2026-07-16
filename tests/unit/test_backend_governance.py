import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from backend_governance import (
    BackendGovernanceError, compile_backend_profile, materialize_backend_profile,
    require_resource_observability,
)
from agent_backends import build_command
from protocol import utc_now, write_json
from strategy import DEFAULT_EXECUTION_POLICY
from validation import validate_worker_handoff


def strategy_payload(backend_id: str = "claude-code"):
    return {
        "schema_version": 2,
        "backend_id": backend_id,
        "strategy_id": "T001-test-S001",
        "task_id": "T001-test",
        "revision": 1,
        "supersedes": None,
        "objective": "Test backend governance",
        "global_budget": {
            "wall_seconds": 60,
            "max_workflows": 1,
            "max_workflow_instances": 1,
            "max_parallel_workflows": 1,
            "max_subagents": 1,
            "max_parallel_subagents": 1,
        },
        "workflows": [{
            "workflow_id": "WF-test",
            "kind": "test",
            "purpose": "Exercise compilation",
            "depends_on": [],
            "required": True,
            "executor": {
                "mode": "primary_worker",
                "write_access": False,
                "max_agents": 0,
                "max_parallel": 0,
                "allowed_paths": ["."],
            },
            "budget": {
                "wall_seconds": 60,
                "command_seconds": 10,
                "max_enumerated_cases": 1,
                "max_instances": 1,
            },
            "completion": {"evidence": "test"},
            "on_timeout": "block",
        }],
        "runtime_change_policy": {
            "allow_new_instances_of_approved_workflows": True,
            "require_revision_for_new_workflow_kind": True,
            "require_revision_for_budget_increase": True,
            "allow_unbounded_search": False,
        },
        "completion_gate": {
            "required_workflows_complete": False,
            "acceptance_commands_pass": False,
            "optional_workflows_may_timeout": True,
        },
    }


class BackendGovernanceTests(unittest.TestCase):
    def test_native_resume_commands_reuse_backend_session(self):
        claude = build_command(
            backend_id="claude-code",
            io_mode="machine",
            permission_mode="auto",
            cwd="/tmp/repo",
            prompt="continue",
            agent_name="worker",
            execution_mode="resume",
            session_id="11111111-1111-1111-1111-111111111111",
        ).argv
        self.assertEqual(claude[1:3], ["--resume", "11111111-1111-1111-1111-111111111111"])

        codex = build_command(
            backend_id="codex",
            io_mode="machine",
            permission_mode="default",
            cwd="/tmp/repo",
            prompt="continue",
            agent_name="worker",
            execution_mode="resume",
            session_id="22222222-2222-2222-2222-222222222222",
        ).argv
        self.assertIn("resume", codex)
        self.assertEqual(codex[-2:], ["22222222-2222-2222-2222-222222222222", "continue"])
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.task = self.root / "task"
        self.task.mkdir()
        write_json(self.task / "EXECUTION_POLICY.json", DEFAULT_EXECUTION_POLICY)
        self.strategy = self.task / "STRATEGY-v001.json"
        write_json(self.strategy, strategy_payload())

    def tearDown(self):
        self.temporary.cleanup()

    def compile(self):
        return compile_backend_profile(
            repo_root=self.root,
            task_dir=self.task,
            backend_id="claude-code",
            phase="execution",
            strategy_path=self.strategy,
        )

    def compile_codex(self, *, native_subagents: bool = True):
        strategy = strategy_payload("codex")
        if native_subagents:
            strategy["workflows"][0]["executor"].update(
                mode="native_subagents",
                max_agents=1,
                max_parallel=1,
            )
        write_json(self.strategy, strategy)
        return compile_backend_profile(
            repo_root=self.root,
            task_dir=self.task,
            backend_id="codex",
            phase="execution",
            strategy_path=self.strategy,
        )

    def compile_backend(self, backend_id: str, *, native_subagents: bool = True):
        strategy = strategy_payload(backend_id)
        if native_subagents:
            strategy["workflows"][0]["executor"].update(
                mode="native_subagents",
                max_agents=1,
                max_parallel=1,
            )
        write_json(self.strategy, strategy)
        return compile_backend_profile(
            repo_root=self.root,
            task_dir=self.task,
            backend_id=backend_id,
            phase="execution",
            strategy_path=self.strategy,
        )

    def test_compile_is_pure_and_merges_shipped_governance(self):
        profile = self.compile()
        self.assertFalse((self.task / "attempts").exists())
        self.assertIn("superpowers@claude-plugins-official", profile["governance"]["disabled_plugins"])
        self.assertEqual(2, profile["context_access"]["adapter"]["version"])
        self.assertEqual(
            "CONTEXT_ACCESS.ndjson",
            profile["context_access"]["adapter"]["request_log"],
        )
        self.assertEqual(
            "native_tool",
            profile["context_access"]["adapter"]["telemetry_coverage"],
        )
        self.assertEqual(profile["native_agent_limits"], {
            "max_spawns": 1,
            "max_parallel": 1,
            "enforce_max_spawns": False,
        })

    def test_hard_budget_requires_metric_observability_for_io_mode(self):
        strategy = strategy_payload()
        strategy["resource_budget"] = {"max_model_turns": 10, "max_cost_usd": 1.0}
        write_json(self.strategy, strategy)
        profile = self.compile()
        require_resource_observability(profile, "machine")
        with self.assertRaisesRegex(BackendGovernanceError, "not observable"):
            require_resource_observability(profile, "human")

    def test_materialize_writes_attempt_local_settings_and_hooks(self):
        runtime = self.root / "attempt" / "runtime"
        result = materialize_backend_profile(self.compile(), runtime)
        settings = json.loads((runtime / "claude-settings.json").read_text(encoding="utf-8"))
        self.assertFalse(settings["enabledPlugins"]["superpowers@claude-plugins-official"])
        self.assertIn("PreToolUse", settings["hooks"])
        self.assertEqual(result["profile_path"], str(runtime / "BACKEND_PROFILE.json"))
        initialized = json.loads(
            (runtime / "CONTEXT_ACCESS.ndjson").read_text(encoding="utf-8").strip()
        )
        self.assertEqual("context_telemetry_initialized", initialized["event"])
        self.assertEqual("native_tool", initialized["coverage"])

        command = build_command(
            backend_id="claude-code",
            io_mode="machine",
            permission_mode="auto",
            cwd=str(self.root),
            prompt="OK",
            agent_name="test",
            backend_profile=result["profile_path"],
        ).command
        self.assertIn("--settings", command)
        self.assertIn("--permission-mode auto", command)
        self.assertIn("--disable-slash-commands", command)
        self.assertIn("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1", command)
        self.assertIn("RDO_BACKEND_PROFILE_SHA256=", command)

        human_command = build_command(
            backend_id="claude-code",
            io_mode="human",
            permission_mode="auto",
            cwd=str(self.root),
            prompt="OK",
            agent_name="test",
            backend_profile=result["profile_path"],
        ).command
        self.assertIn("--disable-slash-commands", human_command)

    def test_project_policy_tightens_shipped_policy(self):
        config = self.root / ".agent-collab" / "rdo.toml"
        config.parent.mkdir()
        config.write_text(
            '[backends."claude-code"]\n'
            'disabled_plugins = ["extra@example"]\n'
            'enable_agent_teams = false\n'
            'max_tool_use_concurrency = 2\n',
            encoding="utf-8",
        )
        profile = self.compile()
        self.assertEqual(profile["governance"]["max_tool_use_concurrency"], 2)
        self.assertFalse(profile["governance"]["enable_agent_teams"])
        self.assertEqual(
            profile["governance"]["disabled_plugins"],
            ["extra@example", "superpowers@claude-plugins-official"],
        )

    def test_backend_mismatch_fails(self):
        with self.assertRaises(BackendGovernanceError):
            compile_backend_profile(
                repo_root=self.root,
                task_dir=self.task,
                backend_id="codex",
                phase="execution",
                strategy_path=self.strategy,
            )

    def test_codex_profile_uses_native_limits_without_spawn_monitor_by_default(self):
        profile = self.compile_codex()
        self.assertFalse(profile["backend_settings"]["stream_monitor_required"])
        self.assertFalse(profile["native_agent_limits"]["enforce_max_spawns"])
        self.assertIn("features.multi_agent=true", profile["backend_settings"]["config_overrides"])
        self.assertIn("features.enable_fanout=false", profile["backend_settings"]["config_overrides"])
        self.assertIn("features.multi_agent_v2=false", profile["backend_settings"]["config_overrides"])
        self.assertIn("agents.max_threads=1", profile["backend_settings"]["config_overrides"])
        controls = {item["name"]: item for item in profile["controls"]}
        self.assertEqual(controls["native_agent_limits"]["enforcement"], "observed")
        self.assertFalse(controls["native_agent_limits"]["hard"])

        runtime = self.root / "codex-attempt" / "runtime"
        result = materialize_backend_profile(profile, runtime)
        command = build_command(
            backend_id="codex",
            io_mode="machine",
            permission_mode="auto",
            cwd=str(self.root),
            prompt="OK",
            agent_name="test",
            backend_profile=result["profile_path"],
        ).command
        self.assertNotIn("codex_stream_monitor.py", command)
        self.assertIn("--strict-config", command)
        self.assertIn("--ask-for-approval on-request", command)
        self.assertIn("--sandbox workspace-write", command)
        self.assertIn("--enable guardian_approval", command)
        self.assertIn("approvals_reviewer", command)
        self.assertIn("agents.max_threads=1", command)
        self.assertIn("features.enable_fanout=false", command)
        self.assertIn("skills.include_instructions=false", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--dangerously-bypass-hook-trust", command)
        self.assertIn("hooks.PreToolUse", command)
        self.assertEqual(profile["context_access"]["adapter"]["enforcement_level"], "best_effort")
        self.assertEqual(2, profile["context_access"]["adapter"]["version"])
        self.assertEqual("CONTEXT_ACCESS.ndjson", profile["context_access"]["adapter"]["request_log"])
        self.assertEqual("best_effort", profile["context_access"]["adapter"]["telemetry_coverage"])

        human_command = build_command(
            backend_id="codex",
            io_mode="human",
            permission_mode="auto",
            cwd=str(self.root),
            prompt="OK",
            agent_name="test",
            backend_profile=result["profile_path"],
        ).command
        self.assertNotIn("codex_stream_monitor.py", human_command)
        self.assertIn("agents.max_threads=1", human_command)
        self.assertIn("skills.include_instructions=false", human_command)
        self.assertIn("--dangerously-bypass-hook-trust", human_command)
        self.assertNotIn("--ignore-user-config", human_command)

    def test_codex_spawn_monitor_can_be_explicitly_enabled(self):
        config = self.root / ".agent-collab" / "rdo.toml"
        config.parent.mkdir()
        config.write_text('[backends.codex]\nenforce_spawn_limit = true\n', encoding="utf-8")
        profile = self.compile_codex()
        self.assertTrue(profile["native_agent_limits"]["enforce_max_spawns"])
        self.assertTrue(profile["backend_settings"]["stream_monitor_required"])
        runtime = self.root / "codex-enforced-attempt" / "runtime"
        result = materialize_backend_profile(profile, runtime)
        machine_command = build_command(
            backend_id="codex",
            io_mode="machine",
            permission_mode="auto",
            cwd=str(self.root),
            prompt="OK",
            agent_name="test",
            backend_profile=result["profile_path"],
        ).command
        self.assertIn("codex_stream_monitor.py", machine_command)
        with self.assertRaisesRegex(ValueError, "requires machine IO"):
            build_command(
                backend_id="codex",
                io_mode="human",
                permission_mode="auto",
                cwd=str(self.root),
                prompt="OK",
                agent_name="test",
                backend_profile=result["profile_path"],
            )

    def test_codex_disables_undeclared_native_subagents(self):
        profile = self.compile_codex(native_subagents=False)
        self.assertFalse(profile["backend_settings"]["stream_monitor_required"])
        self.assertIn("features.multi_agent=false", profile["backend_settings"]["config_overrides"])

    def test_codex_project_policy_only_tightens_defaults(self):
        config = self.root / ".agent-collab" / "rdo.toml"
        config.parent.mkdir()
        config.write_text(
            '[backends.codex]\n'
            'enable_multi_agent = true\n'
            'max_agent_threads = 2\n'
            'max_agent_depth = 1\n',
            encoding="utf-8",
        )
        profile = self.compile_codex()
        self.assertEqual(profile["governance"]["max_agent_threads"], 2)
        self.assertEqual(profile["governance"]["max_agent_depth"], 1)
        self.assertIn("agents.max_depth=1", profile["backend_settings"]["config_overrides"])

    def test_codex_native_strategy_fails_when_multi_agent_disabled(self):
        config = self.root / ".agent-collab" / "rdo.toml"
        config.parent.mkdir()
        config.write_text('[backends.codex]\nenable_multi_agent = false\n', encoding="utf-8")
        with self.assertRaisesRegex(BackendGovernanceError, "multi-agent is disabled"):
            self.compile_codex()

    def test_codex_planning_remains_available_when_multi_agent_disabled(self):
        config = self.root / ".agent-collab" / "rdo.toml"
        config.parent.mkdir()
        config.write_text('[backends.codex]\nenable_multi_agent = false\n', encoding="utf-8")
        profile = compile_backend_profile(
            repo_root=self.root,
            task_dir=self.task,
            backend_id="codex",
            phase="planning",
        )
        self.assertFalse(profile["backend_settings"]["stream_monitor_required"])
        self.assertIn("features.multi_agent=false", profile["backend_settings"]["config_overrides"])

    def test_codex_native_strategy_fails_when_depth_is_zero(self):
        config = self.root / ".agent-collab" / "rdo.toml"
        config.parent.mkdir()
        config.write_text('[backends.codex]\nmax_agent_depth = 0\n', encoding="utf-8")
        with self.assertRaisesRegex(BackendGovernanceError, "max_agent_depth is zero"):
            self.compile_codex()

    def test_kimi_profile_uses_native_concurrency_without_cumulative_cap(self):
        profile = self.compile_backend("kimi-code")
        self.assertEqual(profile["native_agent_limits"], {"max_parallel": 1})
        self.assertTrue(profile["backend_settings"]["native_subagents_enabled"])
        self.assertTrue(profile["backend_settings"]["agent_swarm_enabled"])
        self.assertEqual(profile["environment"]["KIMI_CODE_AGENT_SWARM_MAX_CONCURRENCY"], "1")
        runtime = self.root / "kimi-attempt" / "runtime"
        result = materialize_backend_profile(profile, runtime)
        fragment = (runtime / "kimi-governance.toml").read_text(encoding="utf-8")
        self.assertIn('pattern = "Agent"', fragment)
        self.assertIn('event = "PreToolUse"', fragment)
        self.assertIn('matcher = "Read|Grep|Glob"', fragment)
        self.assertIn("read_policy_hook.py", fragment)
        self.assertEqual(
            profile["context_access"]["adapter"]["enforcement_level"],
            "fail_open_tool_blocking",
        )
        self.assertEqual(2, profile["context_access"]["adapter"]["version"])
        self.assertEqual("CONTEXT_ACCESS.ndjson", profile["context_access"]["adapter"]["request_log"])
        self.assertEqual("native_tool", profile["context_access"]["adapter"]["telemetry_coverage"])
        command = build_command(
            backend_id="kimi-code",
            io_mode="machine",
            permission_mode="auto",
            cwd=str(self.root),
            prompt="OK",
            agent_name="test",
            backend_profile=result["profile_path"],
        ).command
        self.assertIn("kimi_attempt_wrapper.py", command)
        self.assertIn("KIMI_CODE_AGENT_SWARM_MAX_CONCURRENCY=1", command)

    def test_kimi_disables_undeclared_native_subagents(self):
        profile = self.compile_backend("kimi-code", native_subagents=False)
        self.assertFalse(profile["backend_settings"]["native_subagents_enabled"])
        runtime = self.root / "kimi-disabled" / "runtime"
        materialize_backend_profile(profile, runtime)
        fragment = (runtime / "kimi-governance.toml").read_text(encoding="utf-8")
        self.assertIn('decision = "deny"\npattern = "Agent"', fragment)

    def test_kimi_hook_limits_concurrency_but_not_cumulative_launches(self):
        runtime = self.root / "kimi-hook" / "runtime"
        materialize_backend_profile(self.compile_backend("kimi-code"), runtime)
        hook = Path(__file__).resolve().parents[2] / "scripts" / "kimi_governance_hook.py"
        prefix = [sys.executable, str(hook), "--runtime-dir", str(runtime), "--event"]

        first = subprocess.run(
            [*prefix, "pre-tool-use"], input='{"tool_name":"Agent"}',
            text=True, capture_output=True, check=False,
        )
        concurrent = subprocess.run(
            [*prefix, "pre-tool-use"], input='{"tool_name":"Agent"}',
            text=True, capture_output=True, check=False,
        )
        subprocess.run(
            [*prefix, "post-tool-use"], input='{"tool_name":"Agent"}',
            text=True, capture_output=True, check=True,
        )
        sequential = subprocess.run(
            [*prefix, "pre-tool-use"], input='{"tool_name":"Agent"}',
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(first.returncode, 0)
        self.assertEqual(concurrent.returncode, 2)
        self.assertEqual(sequential.returncode, 0)

    def test_kimi_wrapper_isolates_user_configuration_assets(self):
        runtime = self.root / "kimi-wrapper" / "runtime"
        materialize_backend_profile(self.compile_backend("kimi-code"), runtime)
        source_home = self.root / "kimi-user-home"
        source_home.mkdir()
        (source_home / "config.toml").write_text('[background]\nmax_running_tasks = 9\n', encoding="utf-8")
        (source_home / "AGENTS.md").write_text("user instructions\n", encoding="utf-8")
        (source_home / "credentials").mkdir()
        (source_home / "credentials" / "test.json").write_text("{}\n", encoding="utf-8")
        wrapper = Path(__file__).resolve().parents[2] / "scripts" / "kimi_attempt_wrapper.py"
        child = (
            "import os,pathlib; h=pathlib.Path(os.environ['KIMI_CODE_HOME']); "
            "assert (h/'AGENTS.md').read_text() == 'user instructions\\n'; "
            "assert 'max_running_tasks = 1' in (h/'config.toml').read_text(); "
            "(h/'AGENTS.md').write_text('attempt mutation\\n')"
        )
        environment = os.environ.copy()
        environment["RDO_KIMI_SOURCE_HOME"] = str(source_home)
        result = subprocess.run(
            [sys.executable, str(wrapper), "--runtime-dir", str(runtime), "--",
             sys.executable, "-c", child],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((source_home / "AGENTS.md").read_text(encoding="utf-8"), "user instructions\n")

    def test_opencode_profile_wraps_machine_and_human_commands(self):
        profile = self.compile_backend("opencode")
        self.assertEqual(profile["native_agent_limits"], {"max_parallel": 1})
        self.assertEqual(
            profile["backend_settings"]["allowed_subagent_types"],
            ["explore", "general", "scout"],
        )
        runtime = self.root / "opencode-attempt" / "runtime"
        result = materialize_backend_profile(profile, runtime)
        plugin = runtime / "opencode-config" / "plugins" / "rdo-context.js"
        self.assertTrue(plugin.exists())
        self.assertIn('"tool.execute.before"', plugin.read_text(encoding="utf-8"))
        plugin_module = runtime / "rdo-context.mjs"
        plugin_module.write_text(plugin.read_text(encoding="utf-8"), encoding="utf-8")
        node_check = subprocess.run(
            ["node", "--check", str(plugin_module)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(node_check.returncode, 0, node_check.stderr)
        self.assertEqual(
            profile["context_access"]["adapter"]["enforcement_level"],
            "tool_blocking",
        )
        self.assertEqual(2, profile["context_access"]["adapter"]["version"])
        self.assertEqual("CONTEXT_ACCESS.ndjson", profile["context_access"]["adapter"]["request_log"])
        self.assertEqual("native_tool", profile["context_access"]["adapter"]["telemetry_coverage"])
        for io_mode in ("machine", "human"):
            command = build_command(
                backend_id="opencode",
                io_mode=io_mode,
                permission_mode="auto",
                cwd=str(self.root),
                prompt="OK",
                agent_name="test",
                backend_profile=result["profile_path"],
            ).command
            self.assertIn("opencode_attempt_supervisor.py", command)
            self.assertIn(f"--io-mode {io_mode}", command)

    def test_opencode_project_policy_intersects_agent_types(self):
        config = self.root / ".agent-collab" / "rdo.toml"
        config.parent.mkdir()
        config.write_text(
            '[backends.opencode]\nallowed_subagent_types = ["explore", "unknown"]\n'
            'max_parallel_subagents = 2\npure_mode = true\n',
            encoding="utf-8",
        )
        profile = self.compile_backend("opencode")
        self.assertEqual(profile["governance"]["allowed_subagent_types"], ["explore"])
        self.assertTrue(profile["backend_settings"]["pure_mode"])
        self.assertFalse(profile["backend_settings"]["context_plugin_enabled"])
        self.assertEqual(
            profile["context_access"]["adapter"]["enforcement_level"],
            "advisory",
        )

    def test_agent_team_request_fails_when_project_disables_teams(self):
        config = self.root / ".agent-collab" / "rdo.toml"
        config.parent.mkdir()
        config.write_text('[backends."claude-code"]\nenable_agent_teams = false\n', encoding="utf-8")
        strategy = strategy_payload()
        executor = strategy["workflows"][0]["executor"]
        executor.update(
            mode="native_subagents",
            max_agents=1,
            max_parallel=1,
            backend_options={"claude-code": {"coordination": "agent_team"}},
        )
        write_json(self.strategy, strategy)
        with self.assertRaises(BackendGovernanceError):
            self.compile()

    def test_hook_records_but_does_not_enforce_spawn_budget_by_default(self):
        runtime = self.root / "attempt" / "runtime"
        materialize_backend_profile(self.compile(), runtime)
        hook = Path(__file__).resolve().parents[2] / "scripts" / "claude_governance_hook.py"
        prefix = [sys.executable, str(hook), "--runtime-dir", str(runtime), "--event"]
        for event in ("pre-tool-use", "post-tool-use", "pre-tool-use"):
            result = subprocess.run(
                [*prefix, event],
                input='{"tool_name":"Agent"}',
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(result.stdout, "")
        state = json.loads((runtime / "AGENTS.json").read_text(encoding="utf-8"))
        self.assertEqual(state["total_requests"], 2)

    def test_hook_denies_spawn_over_budget_when_enabled(self):
        config = self.root / ".agent-collab" / "rdo.toml"
        config.parent.mkdir()
        config.write_text(
            '[backends."claude-code"]\nenforce_spawn_limit = true\n',
            encoding="utf-8",
        )
        runtime = self.root / "attempt" / "runtime"
        profile = self.compile()
        self.assertTrue(profile["native_agent_limits"]["enforce_max_spawns"])
        materialize_backend_profile(profile, runtime)
        hook = Path(__file__).resolve().parents[2] / "scripts" / "claude_governance_hook.py"
        prefix = [sys.executable, str(hook), "--runtime-dir", str(runtime), "--event"]
        first = subprocess.run(
            [*prefix, "pre-tool-use"],
            input='{"tool_name":"Agent"}',
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [*prefix, "post-tool-use"],
            input='{"tool_name":"Agent"}',
            text=True,
            capture_output=True,
            check=True,
        )
        second = subprocess.run(
            [*prefix, "pre-tool-use"],
            input='{"tool_name":"Agent"}',
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(first.stdout, "")
        denial = json.loads(second.stdout)
        self.assertEqual(denial["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_unblockable_team_expansion_records_hard_violation(self):
        runtime = self.root / "attempt" / "runtime"
        materialize_backend_profile(self.compile(), runtime)
        hook = Path(__file__).resolve().parents[2] / "scripts" / "claude_governance_hook.py"
        command = [sys.executable, str(hook), "--runtime-dir", str(runtime), "--event", "subagent-start"]
        for agent_id in ("one", "two"):
            subprocess.run(
                command,
                input=json.dumps({"agent_id": agent_id, "agent_type": "general-purpose"}),
                text=True,
                check=True,
            )
        violations = (runtime / "VIOLATIONS.ndjson").read_text(encoding="utf-8").splitlines()
        self.assertTrue(any(json.loads(line)["hard"] for line in violations))

    def test_handoff_rejects_modified_backend_settings(self):
        attempt_id = "A001-test"
        attempt = self.task / "attempts" / attempt_id
        runtime = attempt / "runtime"
        result = materialize_backend_profile(self.compile(), runtime)
        settings_path = runtime / "claude-settings.json"
        settings_digest = hashlib.sha256(settings_path.read_bytes()).hexdigest()
        write_json(attempt / "ATTEMPT.json", {
            "backend_profile_sha256": result["profile_sha256"],
            "backend_settings_sha256": settings_digest,
        })
        now = utc_now()
        status = {
            "state": "running",
            "current_attempt_id": attempt_id,
            "state_history": [{"from": "strategy_review", "to": "running", "actor": "dispatch", "at": now}],
        }
        (self.task / "HANDOFF.md").write_text("# Handoff\n\nComplete.\n", encoding="utf-8")
        (self.task / "EVIDENCE.md").write_text("# Evidence\n\nPassed.\n", encoding="utf-8")
        write_json(self.task / "HANDOFF.json", {
            "_template": False,
            "requested_state": "review",
            "commands_run": [],
            "files_changed": [],
            "known_limitations": [],
        })
        settings_path.write_text("{}\n", encoding="utf-8")
        validation = validate_worker_handoff(status, attempt_id, self.task, "0")
        self.assertFalse(validation.valid)
        self.assertIn("backend settings changed during the attempt", validation.reasons)

    def test_handoff_rejects_modified_read_policy(self):
        attempt_id = "A001-read-policy"
        attempt = self.task / "attempts" / attempt_id
        runtime = attempt / "runtime"
        result = materialize_backend_profile(self.compile(), runtime)
        write_json(attempt / "ATTEMPT.json", {
            "backend_profile_sha256": result["profile_sha256"],
            "read_policy_sha256": result["read_policy_sha256"],
        })
        now = utc_now()
        status = {
            "state": "running",
            "current_attempt_id": attempt_id,
            "state_history": [{"from": "strategy_review", "to": "running", "actor": "dispatch", "at": now}],
        }
        (self.task / "HANDOFF.md").write_text("# Handoff\n\nComplete.\n", encoding="utf-8")
        (self.task / "EVIDENCE.md").write_text("# Evidence\n\nPassed.\n", encoding="utf-8")
        write_json(self.task / "HANDOFF.json", {
            "_template": False,
            "requested_state": "review",
            "commands_run": [],
            "files_changed": [],
            "known_limitations": [],
        })
        write_json(runtime / "READ_POLICY.json", {"schema_version": 1, "worktree": "/tampered"})
        validation = validate_worker_handoff(status, attempt_id, self.task, "0")
        self.assertFalse(validation.valid)
        self.assertIn("read policy changed during the attempt", validation.reasons)


if __name__ == "__main__":
    unittest.main()
