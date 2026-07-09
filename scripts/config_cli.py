#!/usr/bin/env python3
"""Narrow CLI for inspecting operational configuration."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from config import load_config
from protocol import repo_root


def as_env_bool(value: bool) -> str:
    return "1" if value else "0"


ENV_KEYS = {
    "RDO_WORKER_COMMAND": "worker_command",
    "CLAUDE_CODE_CMD": "worker_command",
    "RDO_WORKER_BACKEND": "worker_backend",
    "RDO_WORKER_AGENT_NAME": "worker_agent_name",
    "CLAUDE_AGENT_NAME": "worker_agent_name",
    "RDO_BACKEND_SESSION_ID": "worker_session_id",
    "CLAUDE_SESSION_ID": "worker_session_id",
    "RDO_PERMISSION_MODE": "permission_mode",
    "RDO_RUNTIME_BACKEND": "runtime_backend",
    "RDO_IO_MODE": "io_mode",
    "RDO_TMUX_SESSION_PREFIX": "tmux_session_prefix",
    "RDO_TMUX_KEEP_SESSION": "tmux_keep_session",
    "RDO_TMUX_WAIT_TIMEOUT_SECONDS": "tmux_wait_timeout_seconds",
    "RDO_TMUX_EXIT_CODE_GRACE_SECONDS": "tmux_exit_code_grace_seconds",
    "RDO_STALE_LOCK_HOURS": "stale_lock_hours",
    "RDO_STALE_CREATED_MINUTES": "stale_created_minutes",
    "RDO_TASK_BRANCH_PREFIX": "task_branch_prefix",
    "RDO_WORKTREE_ROOT": "worktree_root",
}


def config_to_env(config: Any, *, prefix: str = "") -> dict[str, str]:
    payload: dict[str, str] = {}
    for env_key, attr in ENV_KEYS.items():
        value = getattr(config, attr)
        if isinstance(value, bool):
            rendered = as_env_bool(value)
        else:
            rendered = str(value)
        payload[f"{prefix}{env_key}"] = rendered
    return payload


def load_current_config(*, use_env: bool = True) -> Any:
    root = repo_root(Path.cwd())
    return load_config(root, use_env=use_env)


def print_diagnostics(result: Any) -> None:
    for warning in result.warnings:
        print(f"config warning: {warning}", file=sys.stderr)
    for error in result.errors:
        print(f"config error: {error}", file=sys.stderr)


def validate_prefix(prefix: str) -> None:
    if prefix and prefix != "CONFIG_":
        raise SystemExit("--prefix only supports CONFIG_ in this version")
    if prefix and not re.match(r"^[A-Z_][A-Z0-9_]*$", prefix):
        raise SystemExit("--prefix must match ^[A-Z_][A-Z0-9_]*$")


def cmd_export_env(args: argparse.Namespace) -> int:
    validate_prefix(args.prefix)
    result = load_current_config(use_env=not args.no_env)
    print_diagnostics(result)
    for key, value in config_to_env(result.config, prefix=args.prefix).items():
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
    export_env.add_argument("--no-env", action="store_true", help="Ignore current environment overrides.")
    export_env.add_argument("--prefix", default="", help="Prefix exported variable names. Only CONFIG_ is supported.")
    export_env.set_defaults(func=cmd_export_env)
    json_cmd = sub.add_parser("json", help="Print resolved config as JSON.")
    json_cmd.set_defaults(func=cmd_json)
    validate = sub.add_parser("validate", help="Validate config and print diagnostics.")
    validate.set_defaults(func=cmd_validate)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
