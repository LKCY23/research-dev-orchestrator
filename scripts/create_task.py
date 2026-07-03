#!/usr/bin/env python3
"""Create a standard task packet."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_git(args: list[str], cwd: Path, default: str = "") -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return default


def repo_root(cwd: Path) -> Path:
    root = run_git(["rev-parse", "--show-toplevel"], cwd)
    return Path(root) if root else cwd


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
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    validate_task_id(args.task_id)
    root = repo_root(Path.cwd())
    run_dir = root / ".agent-collab" / "runs" / args.run_id
    if not run_dir.exists():
        raise SystemExit(f"Run not found: {run_dir}")

    task_dir = run_dir / "tasks" / args.task_id
    if task_dir.exists() and not args.force:
        raise SystemExit(f"Task already exists: {task_dir}")
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "logs").mkdir(exist_ok=True)
    (task_dir / "attempts").mkdir(exist_ok=True)

    branch = args.branch or f"agent/{args.task_id}"
    worktree = args.worktree or f".agent-worktrees/{args.task_id}"
    now = utc_now()

    status = {
        "task_id": args.task_id,
        "state": "pending",
        "previous_state": None,
        "owner": "",
        "branch": branch,
        "worktree": worktree,
        "updated_at": now,
        "needs_codex": False,
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
    (task_dir / "TASK.md").write_text(
        f"""# Task {args.task_id}

```yaml
task_id: {args.task_id}
goal: {args.goal}
allowed_paths:
{render_list(args.allowed_paths)}
forbidden_paths:
{render_list(args.forbidden_paths)}
dependencies:
{render_list(args.dependencies)}
branch: {branch}
worktree: {worktree}
non_goals:
  - Do not expand scope beyond this task packet.
```
""",
        encoding="utf-8",
    )
    (task_dir / "CONTEXT.md").write_text("# Context\n\n## Relevant Requirements\n\n## Design Notes\n\n## Interfaces\n\n## Constraints\n", encoding="utf-8")
    (task_dir / "ACCEPTANCE.md").write_text("# Acceptance\n\n## Required Commands\n\n## Expected Outputs\n\n## Metrics Or Thresholds\n\n## Smoke Test\n\n## Failure Handoff Condition\n\n## Post-Merge Smoke Test\n", encoding="utf-8")
    (task_dir / "HANDOFF.md").write_text("# Handoff\n\n## What Changed\n\n## What Failed\n\n## Evidence\n\n## Decision Needed\n\n## Suggested Next Action\n", encoding="utf-8")
    (task_dir / "EVIDENCE.md").write_text("# Evidence\n\n## Commands Run\n\n## Tests Passed\n\n## Metrics / Outputs\n\n## Logs\n\n## Known Limitations\n", encoding="utf-8")

    print(task_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
