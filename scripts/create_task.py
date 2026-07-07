#!/usr/bin/env python3
"""Create a standard task packet."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from config import load_config
from protocol import append_event, render_template, repo_root, utc_now


def validate_task_id(task_id: str) -> None:
    if not re.match(r"^T[0-9]{3}[A-Za-z0-9-]*$", task_id):
        raise SystemExit("task_id must look like T001-name")


def render_list(values: list[str]) -> str:
    if not values:
        return "[]"
    return "\n".join(f"  - {value}" for value in values)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a task packet in an orchestration run.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--allowed-paths", nargs="+", required=True)
    parser.add_argument("--forbidden-paths", nargs="*", default=[])
    parser.add_argument("--dependencies", nargs="*", default=[])
    parser.add_argument("--branch", default="")
    parser.add_argument("--worktree", default="")
    args = parser.parse_args()

    validate_task_id(args.task_id)
    root = repo_root(Path.cwd())
    config_result = load_config(root)
    for warning in config_result.warnings:
        print(f"config warning: {warning}", file=sys.stderr)
    if config_result.errors:
        raise SystemExit("\n".join(f"config error: {error}" for error in config_result.errors))

    run_dir = root / ".agent-collab" / "runs" / args.run_id
    if not run_dir.exists():
        raise SystemExit(f"Run not found: {run_dir}")

    task_dir = run_dir / "tasks" / args.task_id
    if task_dir.exists():
        raise SystemExit(f"Task already exists: {task_dir}")
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "logs").mkdir(exist_ok=True)
    (task_dir / "attempts").mkdir(exist_ok=True)

    config = config_result.config
    branch = args.branch or f"{config.task_branch_prefix}{args.task_id}"
    worktree = args.worktree or str(Path(config.worktree_root) / args.task_id)
    now = utc_now()

    status = {
        "task_id": args.task_id,
        "state": "pending",
        "previous_state": None,
        "owner": "",
        "branch": branch,
        "worktree": worktree,
        "updated_at": now,
        "needs_coordinator": False,
        "summary": "",
        "blocking_reason": "",
        "blocker_type": "",
        "current_attempt_id": None,
        "assigned_worker": None,
        "evidence": {
            "commands_run": [],
            "logs": [],
            "passed": None,
        },
        "state_history": [],
    }

    (task_dir / "STATUS.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    task_values = {
        "TASK_ID": args.task_id,
        "GOAL": args.goal,
        "ALLOWED_PATHS": render_list(args.allowed_paths),
        "FORBIDDEN_PATHS": render_list(args.forbidden_paths),
        "DEPENDENCIES": render_list(args.dependencies),
        "BRANCH": branch,
        "WORKTREE": worktree,
    }
    (task_dir / "TASK.md").write_text(render_template("task/TASK.md", task_values), encoding="utf-8")
    for filename in ["CONTEXT.md", "ACCEPTANCE.md", "HANDOFF.md", "HANDOFF.json", "EVIDENCE.md"]:
        (task_dir / filename).write_text(render_template(f"task/{filename}"), encoding="utf-8")
    append_event(
        run_dir,
        {
            "at": now,
            "actor": "codex",
            "event": "task_created",
            "run_id": args.run_id,
            "task_id": args.task_id,
            "goal": args.goal,
            "allowed_paths": args.allowed_paths,
            "forbidden_paths": args.forbidden_paths,
        },
    )

    print(task_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
