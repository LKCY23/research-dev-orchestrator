import tempfile
import unittest
from pathlib import Path

from agent_backends import build_command
from backend_governance import (
    BackendGovernanceError,
    compile_backend_profile,
    materialize_backend_profile,
)
from config import load_config
from protocol import write_json
from strategy import DEFAULT_EXECUTION_POLICY


class ModelConfigurationTests(unittest.TestCase):
    def fixture(self) -> tuple[tempfile.TemporaryDirectory, Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        task = root / "task"
        task.mkdir()
        write_json(task / "EXECUTION_POLICY.json", DEFAULT_EXECUTION_POLICY)
        return temporary, root, task

    def test_toml_and_environment_resolve_typed_model_fields(self) -> None:
        temporary, root, _task = self.fixture()
        self.addCleanup(temporary.cleanup)
        config_dir = root / ".agent-collab"
        config_dir.mkdir()
        (config_dir / "rdo.toml").write_text(
            """[worker]
backend = "codex"
model = "gpt-5.6-luna"
reasoning_effort = "high"
""",
            encoding="utf-8",
        )

        result = load_config(
            root,
            environ={"RDO_WORKER_REASONING_EFFORT": "max"},
        )

        self.assertEqual([], result.errors)
        self.assertEqual("gpt-5.6-luna", result.config.worker_model)
        self.assertEqual("max", result.config.worker_reasoning_effort)

    def test_codex_model_contract_is_frozen_and_rendered(self) -> None:
        temporary, root, task = self.fixture()
        self.addCleanup(temporary.cleanup)
        profile = compile_backend_profile(
            repo_root=root,
            task_dir=task,
            backend_id="codex",
            phase="planning",
            io_mode="human",
            model="gpt-5.6-luna",
            reasoning_effort="max",
        )
        runtime = root / "runtime"
        materialize_backend_profile(profile, runtime)

        command = build_command(
            backend_id="codex",
            io_mode="human",
            permission_mode="default",
            cwd=str(root),
            prompt="work",
            agent_name="worker",
            backend_profile=str(runtime / "BACKEND_PROFILE.json"),
        )

        self.assertEqual(
            {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
            profile["model_config"],
        )
        self.assertIn("--model", command.argv)
        self.assertEqual("gpt-5.6-luna", command.argv[command.argv.index("--model") + 1])
        self.assertIn('model_reasoning_effort="max"', command.argv)

    def test_claude_model_contract_uses_effort_flag(self) -> None:
        temporary, root, task = self.fixture()
        self.addCleanup(temporary.cleanup)
        profile = compile_backend_profile(
            repo_root=root,
            task_dir=task,
            backend_id="claude-code",
            phase="planning",
            io_mode="machine",
            model="claude-opus-4-6",
            reasoning_effort="xhigh",
        )
        runtime = root / "runtime"
        materialize_backend_profile(profile, runtime)
        command = build_command(
            backend_id="claude-code",
            io_mode="machine",
            permission_mode="auto",
            cwd=str(root),
            prompt="work",
            agent_name="worker",
            backend_profile=str(runtime / "BACKEND_PROFILE.json"),
        )
        self.assertIn("--model", command.argv)
        self.assertIn("--effort", command.argv)
        self.assertEqual("xhigh", command.argv[command.argv.index("--effort") + 1])

    def test_unsupported_or_ambiguous_model_requests_fail_closed(self) -> None:
        temporary, root, task = self.fixture()
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(BackendGovernanceError, "requires an explicit model"):
            compile_backend_profile(
                repo_root=root,
                task_dir=task,
                backend_id="codex",
                phase="planning",
                reasoning_effort="max",
            )
        with self.assertRaisesRegex(BackendGovernanceError, "governed model-selection"):
            compile_backend_profile(
                repo_root=root,
                task_dir=task,
                backend_id="opencode",
                phase="planning",
                model="provider/model",
            )


if __name__ == "__main__":
    unittest.main()
