#!/usr/bin/env python3
"""Create a standard task packet."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

from config import load_config
from protocol import (
    ARTIFACT_PROTOCOL_VERSION,
    EXECUTION_PROFILES,
    append_event,
    render_template,
    repo_root,
    utc_now,
    write_json,
)
from strategy import DEFAULT_EXECUTION_POLICY


TASK_OBJECTIVE_PLACEHOLDER = "RDO_TEMPLATE_INCOMPLETE: state the single outcome this task must achieve."
TASK_DEPENDENCIES_BLOCK = re.compile(
    r"```json rdo-task-dependencies\n.*?\n```",
    re.DOTALL,
)


def validate_task_id(task_id: str) -> None:
    if not re.match(r"^T[0-9]{3}[A-Za-z0-9-]*$", task_id):
        raise SystemExit("task_id must look like T001-name")


def dependency_contract(task_id: str, values: list[str]) -> dict[str, object]:
    dependencies: list[dict[str, str]] = []
    seen: set[str] = set()
    for dependency_id in values:
        if not re.fullmatch(r"T[0-9]{3}[A-Za-z0-9-]*", dependency_id):
            raise SystemExit(
                f"dependency must be a task_id like T001-name, got {dependency_id!r}"
            )
        if dependency_id == task_id:
            raise SystemExit("a task cannot depend on itself")
        if dependency_id in seen:
            raise SystemExit(f"duplicate dependency task_id: {dependency_id}")
        seen.add(dependency_id)
        dependencies.append({"task_id": dependency_id, "required_state": "merged"})
    return {"schema_version": ARTIFACT_PROTOCOL_VERSION, "dependencies": dependencies}


def render_task(goal: str, dependencies: dict[str, object]) -> str:
    rendered = render_template("task/TASK.md")
    if rendered.count(TASK_OBJECTIVE_PLACEHOLDER) != 1:
        raise RuntimeError("TASK template must contain exactly one objective placeholder")
    rendered = rendered.replace(TASK_OBJECTIVE_PLACEHOLDER, goal, 1)
    dependency_block = (
        "```json rdo-task-dependencies\n"
        + json.dumps(dependencies, indent=2)
        + "\n```"
    )
    rendered, replacements = TASK_DEPENDENCIES_BLOCK.subn(dependency_block, rendered, count=1)
    if replacements != 1:
        raise RuntimeError("TASK template must contain exactly one dependency contract block")
    return rendered.rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a task packet in an orchestration run.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--allowed-paths", nargs="+", required=True)
    parser.add_argument(
        "--read-paths",
        nargs="+",
        default=None,
        help="repository paths visible to worker discovery; defaults to allowed-paths",
    )
    parser.add_argument("--forbidden-paths", nargs="*", default=[])
    parser.add_argument(
        "--context-sources",
        nargs="*",
        default=[],
        help="repository-relative context documents available through the context broker",
    )
    parser.add_argument("--dependencies", nargs="*", default=[])
    parser.add_argument(
        "--profile",
        choices=sorted(EXECUTION_PROFILES),
        default="full",
        help="direct=self-reviewed worker; delegated=coordinator code review; full=strategy-gated workflow",
    )
    parser.add_argument("--branch", default="")
    parser.add_argument("--worktree", default="")
    args = parser.parse_args()

    validate_task_id(args.task_id)
    if not args.goal.strip():
        raise SystemExit("goal must be non-empty")
    dependencies = dependency_contract(args.task_id, args.dependencies)
    task_document = render_task(args.goal.strip(), dependencies)
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
    (task_dir / "strategy").mkdir(exist_ok=True)

    config = config_result.config
    branch = args.branch or f"{config.task_branch_prefix}{args.task_id}"
    worktree = args.worktree or str(Path(config.worktree_root) / args.task_id)
    now = utc_now()

    status = {
        "task_id": args.task_id,
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "profile": args.profile,
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
    execution_policy = copy.deepcopy(DEFAULT_EXECUTION_POLICY)
    execution_policy["schema_version"] = ARTIFACT_PROTOCOL_VERSION
    execution_policy["strategy_required"] = args.profile == "full"
    execution_policy["allowed_paths"] = list(args.allowed_paths)
    execution_policy["read_paths"] = list(args.read_paths or args.allowed_paths)
    execution_policy["forbidden_paths"] = list(args.forbidden_paths)
    execution_policy["context_sources"] = list(args.context_sources)
    write_json(task_dir / "EXECUTION_POLICY.json", execution_policy)
    (task_dir / "TASK.md").write_text(task_document, encoding="utf-8")
    for filename in ["CONTEXT.md", "ACCEPTANCE.md"]:
        (task_dir / filename).write_text(render_template(f"task/{filename}"), encoding="utf-8")
    append_event(
        run_dir,
        {
            "at": now,
            "actor": "coordinator",
            "event": "task_created",
            "run_id": args.run_id,
            "task_id": args.task_id,
            "goal": args.goal,
            "profile": args.profile,
            "allowed_paths": args.allowed_paths,
            "read_paths": args.read_paths or args.allowed_paths,
            "forbidden_paths": args.forbidden_paths,
            "context_sources": args.context_sources,
            "dependencies": dependencies["dependencies"],
        },
    )

    print(task_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
