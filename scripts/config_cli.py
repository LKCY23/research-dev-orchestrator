#!/usr/bin/env python3
"""Narrow CLI for inspecting operational configuration."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from config import load_config
from protocol import repo_root


def as_env_bool(value: bool) -> str:
    return "1" if value else "0"


def config_to_env(config: Any) -> dict[str, str]:
    return {
        "CLAUDE_CODE_CMD": config.worker_command,
        "CLAUDE_AGENT_NAME": config.worker_agent_name,
        "CLAUDE_SESSION_ID": config.worker_session_id,
        "RDO_WORKER_BACKEND": config.worker_backend,
        "RDO_TMUX_SESSION_PREFIX": config.tmux_session_prefix,
        "RDO_TMUX_KEEP_SESSION": as_env_bool(config.tmux_keep_session),
        "RDO_TMUX_WAIT_TIMEOUT_SECONDS": str(config.tmux_wait_timeout_seconds),
        "RDO_TMUX_EXIT_CODE_GRACE_SECONDS": str(config.tmux_exit_code_grace_seconds),
        "RDO_STALE_LOCK_HOURS": str(config.stale_lock_hours),
        "RDO_STALE_CREATED_MINUTES": str(config.stale_created_minutes),
        "RDO_TASK_BRANCH_PREFIX": config.task_branch_prefix,
        "RDO_WORKTREE_ROOT": config.worktree_root,
    }


def load_current_config() -> Any:
    root = repo_root(Path.cwd())
    return load_config(root)


def print_diagnostics(result: Any) -> None:
    for warning in result.warnings:
        print(f"config warning: {warning}", file=sys.stderr)
    for error in result.errors:
        print(f"config error: {error}", file=sys.stderr)


def cmd_export_env(_: argparse.Namespace) -> int:
    result = load_current_config()
    print_diagnostics(result)
    for key, value in config_to_env(result.config).items():
        print(f"{key}={shlex.quote(value)}")
    return 2 if result.errors else 0


def cmd_json(_: argparse.Namespace) -> int:
    result = load_current_config()
    print_diagnostics(result)
    print(json.dumps({"config": result.config.__dict__, "warnings": result.warnings, "errors": result.errors}, indent=2))
    return 2 if result.errors else 0


def cmd_validate(_: argparse.Namespace) -> int:
    result = load_current_config()
    print_diagnostics(result)
    if result.errors:
        return 2
    print(f"Config valid: {result.path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect research-dev-orchestrator runtime config.")
    sub = parser.add_subparsers(dest="command", required=True)
    export_env = sub.add_parser("export-env", help="Print shell assignments for resolved config.")
    export_env.set_defaults(func=cmd_export_env)
    json_cmd = sub.add_parser("json", help="Print resolved config as JSON.")
    json_cmd.set_defaults(func=cmd_json)
    validate = sub.add_parser("validate", help="Validate config and print diagnostics.")
    validate.set_defaults(func=cmd_validate)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
