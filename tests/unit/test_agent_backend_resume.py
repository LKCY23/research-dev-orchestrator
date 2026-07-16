import unittest

from agent_backends import build_command


class AgentBackendResumeTests(unittest.TestCase):
    def test_codex_machine_resume_keeps_exec_options_before_subcommand(self):
        command = build_command(
            backend_id="codex",
            io_mode="machine",
            permission_mode="default",
            cwd="/tmp/worktree",
            prompt="continue",
            agent_name="worker",
            execution_mode="resume",
            session_id="11111111-1111-1111-1111-111111111111",
        )
        exec_index = command.argv.index("exec")
        resume_index = command.argv.index("resume")
        cd_index = command.argv.index("--cd")
        self.assertLess(exec_index, cd_index)
        self.assertLess(cd_index, resume_index)
        self.assertEqual(
            [
                "resume",
                "11111111-1111-1111-1111-111111111111",
                "continue",
            ],
            command.argv[-3:],
        )

    def test_codex_human_resume_keeps_top_level_options_before_subcommand(self):
        command = build_command(
            backend_id="codex",
            io_mode="human",
            permission_mode="default",
            cwd="/tmp/worktree",
            prompt="continue",
            agent_name="worker",
            execution_mode="resume",
            session_id="22222222-2222-2222-2222-222222222222",
        )
        self.assertLess(command.argv.index("--cd"), command.argv.index("resume"))
        self.assertEqual(
            [
                "resume",
                "22222222-2222-2222-2222-222222222222",
                "continue",
            ],
            command.argv[-3:],
        )


if __name__ == "__main__":
    unittest.main()
