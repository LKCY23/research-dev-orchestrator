#!/usr/bin/env python3
"""Render dispatch-time worker assets.

This module intentionally does not mutate protocol state. It only renders
attempt-local files used by dispatch_claude.sh.
"""

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").rstrip()


def render_worker_prompt(
    *,
    worktree_path: str,
    task_dir: Path,
    status_path: Path,
    attempt_dir: Path,
    worker_backend: str = "claude-code",
    agent_name: str = "",
) -> str:
    return "\n".join(
        [
            "# Worker Task Prompt",
            "",
            f"You are a {worker_backend} worker. Execute only this task packet.",
            f"Agent name: {agent_name or worker_backend}.",
            "",
            "## Protocol File Paths",
            "",
            "You are running in this worktree:",
            "",
            f"- WORKTREE_PATH: {worktree_path}",
            "",
            "The orchestration protocol files are outside the worktree. Write to these absolute paths:",
            "",
            f"- TASK_DIR: {task_dir}",
            f"- STATUS_PATH: {status_path}",
            f"- EVIDENCE_PATH: {task_dir / 'EVIDENCE.md'}",
            f"- HANDOFF_PATH: {task_dir / 'HANDOFF.md'}",
            f"- HANDOFF_JSON_PATH: {task_dir / 'HANDOFF.json'}",
            f"- ATTEMPT_DIR: {attempt_dir}",
            f"- LOGS_DIR: {task_dir / 'logs'}",
            "",
            "Do not create alternate STATUS/EVIDENCE/HANDOFF files inside the worktree.",
            "",
            "## Protocol Reminders",
            "",
            "- Do not edit STATUS.json. Dispatch owns task state transitions.",
            "- Request terminal state by writing HANDOFF.json with requested_state=review or requested_state=blocked.",
            "- If blocked, blocker_type must be one of: needs_coordinator, needs_user, environment, budget, irrecoverable.",
            "- Remove RDO_TEMPLATE markers from EVIDENCE.md or HANDOFF.md before ending.",
            "- Write substantive EVIDENCE.md and HANDOFF.md before ending.",
            "- HANDOFF.json is required for handoff. Set _template=false and keep HANDOFF.md as the human-readable source.",
            "- Keep code changes inside allowed_paths.",
            "",
            "## TASK.md",
            read_text(task_dir / "TASK.md"),
            "",
            "## CONTEXT.md",
            read_text(task_dir / "CONTEXT.md"),
            "",
            "## ACCEPTANCE.md",
            read_text(task_dir / "ACCEPTANCE.md"),
            "",
        ]
    )


def render_tmux_runner(
    *,
    worktree_path: str,
    command: str,
    prompt_path: str,
    transcript_path: str,
    exit_code_file: str,
    done_signal: str,
    keep_session: str,
    prompt_transport: str,
    submit_key: str,
    post_paste_delay_ms: str,
) -> str:
    delay_seconds = "0"
    try:
        delay_seconds = str(max(0, int(post_paste_delay_ms)) / 1000)
    except ValueError:
        delay_seconds = "0"
    return f"""#!/usr/bin/env bash
set +e
WORKTREE_PATH={shlex.quote(worktree_path)}
WORKER_COMMAND={shlex.quote(command)}
PROMPT_PATH={shlex.quote(prompt_path)}
TRANSCRIPT_PATH={shlex.quote(transcript_path)}
EXIT_CODE_FILE={shlex.quote(exit_code_file)}
DONE_SIGNAL={shlex.quote(done_signal)}
KEEP_SESSION={shlex.quote(keep_session)}
PROMPT_TRANSPORT={shlex.quote(prompt_transport)}
SUBMIT_KEY={shlex.quote(submit_key)}
POST_PASTE_DELAY_SECONDS={shlex.quote(delay_seconds)}

finish() {{
  local rc="$?"
  local tmp="${{EXIT_CODE_FILE}}.tmp"
  echo "${{rc}}" > "${{tmp}}"
  mv "${{tmp}}" "${{EXIT_CODE_FILE}}"
  tmux wait-for -S "${{DONE_SIGNAL}}" 2>/dev/null || true
  if [[ "${{KEEP_SESSION}}" == "1" ]]; then
    echo
    echo "Worker finished with exit code ${{rc}}."
    echo "Press Ctrl-D or run exit to close this tmux session."
    exec bash -l
  fi
  exit "${{rc}}"
}}
trap finish EXIT

cd "${{WORKTREE_PATH}}" || exit 127
set -o pipefail
if [[ "${{PROMPT_TRANSPORT}}" == "tmux_send_keys" ]]; then
  (
    sleep 3
    if [[ -s "${{PROMPT_PATH}}" ]]; then
      tmux load-buffer -b rdo-worker-prompt "${{PROMPT_PATH}}" 2>/dev/null || true
      tmux paste-buffer -b rdo-worker-prompt 2>/dev/null || true
      sleep "${{POST_PASTE_DELAY_SECONDS}}"
      if [[ -n "${{SUBMIT_KEY}}" ]]; then
        tmux send-keys "${{SUBMIT_KEY}}" 2>/dev/null || true
      fi
    fi
  ) &
  eval "${{WORKER_COMMAND}}"
  exit "$?"
fi
if [[ "${{PROMPT_TRANSPORT}}" == "arg" ]]; then
  eval "${{WORKER_COMMAND}}"
  exit "$?"
fi
eval "${{WORKER_COMMAND}}" < "${{PROMPT_PATH}}" 2>&1 | tee "${{TRANSCRIPT_PATH}}"
exit "${{PIPESTATUS[0]}}"
"""


def cmd_render_prompt(args: argparse.Namespace) -> int:
    output = Path(args.output)
    output.write_text(
        render_worker_prompt(
            worktree_path=args.worktree_path,
            task_dir=Path(args.task_dir),
            status_path=Path(args.status_path),
            attempt_dir=Path(args.attempt_dir),
            worker_backend=args.worker_backend,
            agent_name=args.agent_name,
        ),
        encoding="utf-8",
    )
    return 0


def cmd_render_tmux_runner(args: argparse.Namespace) -> int:
    output = Path(args.output)
    output.write_text(
        render_tmux_runner(
            worktree_path=args.worktree_path,
            command=args.command,
            prompt_path=args.prompt_path,
            transcript_path=args.transcript_path,
            exit_code_file=args.exit_code_file,
            done_signal=args.done_signal,
            keep_session=args.keep_session,
            prompt_transport=args.prompt_transport,
            submit_key=args.submit_key,
            post_paste_delay_ms=args.post_paste_delay_ms,
        ),
        encoding="utf-8",
    )
    os.chmod(output, 0o755)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render dispatch worker assets.")
    sub = parser.add_subparsers(dest="command", required=True)

    prompt = sub.add_parser("render-prompt")
    prompt.add_argument("--output", required=True)
    prompt.add_argument("--worktree-path", required=True)
    prompt.add_argument("--task-dir", required=True)
    prompt.add_argument("--status-path", required=True)
    prompt.add_argument("--attempt-dir", required=True)
    prompt.add_argument("--worker-backend", default="claude-code")
    prompt.add_argument("--agent-name", default="")
    prompt.set_defaults(func=cmd_render_prompt)

    runner = sub.add_parser("render-tmux-runner")
    runner.add_argument("--output", required=True)
    runner.add_argument("--worktree-path", required=True)
    runner.add_argument("--command", required=True)
    runner.add_argument("--prompt-path", required=True)
    runner.add_argument("--transcript-path", required=True)
    runner.add_argument("--exit-code-file", required=True)
    runner.add_argument("--done-signal", required=True)
    runner.add_argument("--keep-session", required=True)
    runner.add_argument("--prompt-transport", default="stdin")
    runner.add_argument("--submit-key", default="")
    runner.add_argument("--post-paste-delay-ms", default="0")
    runner.set_defaults(func=cmd_render_tmux_runner)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
