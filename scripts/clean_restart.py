#!/usr/bin/env python3
"""Create a fresh task workspace for a new attempt without cloning the task."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from protocol import load_json, task_state_lock, utc_now, write_json
from task_contract import write_json_immutable


class CleanRestartError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise CleanRestartError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def _apply_status_binding(status_path: Path, receipt: dict[str, Any]) -> None:
    status = load_json(status_path)
    if status.get("current_attempt_id") != receipt["parent_attempt_id"]:
        raise CleanRestartError("clean restart parent is no longer the current attempt")
    if status.get("state") != "blocked":
        raise CleanRestartError("clean restart requires STATUS.state=blocked")
    status["branch"] = receipt["new_branch"]
    status["worktree"] = receipt["new_worktree"]
    status["task_branch_root"] = receipt["root_branch"]
    status["workspace_generation"] = int(status.get("workspace_generation") or 0) + 1
    status["updated_at"] = receipt["prepared_at"]
    write_json(status_path, status)


def prepare_clean_restart(
    *,
    repo_root: Path,
    task_dir: Path,
    attempt_id: str,
    task_base_commit: str,
    new_branch: str,
    new_worktree: str,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    task_dir = task_dir.resolve()
    status_path = task_dir / "STATUS.json"
    receipt_path = task_dir / "attempts" / attempt_id / "runtime" / "CLEAN_RESTART.json"
    new_worktree_path = Path(new_worktree)
    if not new_worktree_path.is_absolute():
        new_worktree_path = repo_root / new_worktree_path
    new_worktree_path = new_worktree_path.resolve()

    with task_state_lock(task_dir):
        if receipt_path.exists():
            receipt = load_json(receipt_path)
            if not isinstance(receipt, dict) or any(
                receipt.get(field) != expected
                for field, expected in {
                    "attempt_id": attempt_id,
                    "task_base_commit": task_base_commit,
                    "new_branch": new_branch,
                    "new_worktree": new_worktree,
                }.items()
            ):
                raise CleanRestartError("existing clean-restart receipt does not match request")
            status = load_json(status_path)
            if (
                status.get("branch") != new_branch
                or status.get("worktree") != new_worktree
            ):
                _apply_status_binding(status_path, receipt)
            return receipt

        status = load_json(status_path)
        if status.get("state") != "blocked":
            raise CleanRestartError("clean restart requires STATUS.state=blocked")
        parent_attempt_id = status.get("current_attempt_id")
        if not isinstance(parent_attempt_id, str) or not parent_attempt_id:
            raise CleanRestartError("clean restart requires a current parent attempt")
        parent = load_json(task_dir / "attempts" / parent_attempt_id / "ATTEMPT.json")
        parent_restartable = parent.get("state") in {"terminated", "invalid_handoff"} or (
            parent.get("state") == "completed" and parent.get("handoff_state") == "blocked"
        )
        if not parent_restartable:
            raise CleanRestartError(
                "clean restart requires a terminated, invalid_handoff, or blocked-handoff parent attempt"
            )
        old_branch = str(status.get("branch") or "")
        old_worktree = str(status.get("worktree") or "")
        root_branch = str(status.get("task_branch_root") or old_branch)
        if not old_branch or not old_worktree:
            raise CleanRestartError("clean restart requires the current branch and worktree")
        if new_branch == old_branch or new_worktree == old_worktree:
            raise CleanRestartError("clean restart must use a fresh branch and worktree")
        _git(repo_root, "check-ref-format", "--branch", new_branch)
        _git(repo_root, "cat-file", "-e", f"{task_base_commit}^{{commit}}")
        branch_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{new_branch}"],
            cwd=repo_root,
            check=False,
        ).returncode == 0
        if branch_exists:
            raise CleanRestartError(f"clean restart branch already exists: {new_branch}")
        if new_worktree_path.exists():
            raise CleanRestartError(f"clean restart worktree already exists: {new_worktree_path}")

        _git(
            repo_root,
            "worktree",
            "add",
            "-b",
            new_branch,
            str(new_worktree_path),
            task_base_commit,
        )
        receipt = {
            "schema_version": 1,
            "task_id": status.get("task_id"),
            "attempt_id": attempt_id,
            "parent_attempt_id": parent_attempt_id,
            "task_base_commit": task_base_commit,
            "old_branch": old_branch,
            "old_worktree": old_worktree,
            "root_branch": root_branch,
            "new_branch": new_branch,
            "new_worktree": new_worktree,
            "prepared_at": utc_now(),
        }
        try:
            write_json_immutable(receipt_path, receipt)
        except Exception:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(new_worktree_path)],
                cwd=repo_root,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "branch", "-D", new_branch],
                cwd=repo_root,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            raise
        _apply_status_binding(status_path, receipt)
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare an attempt-local clean restart")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--task-base-commit", required=True)
    parser.add_argument("--new-branch", required=True)
    parser.add_argument("--new-worktree", required=True)
    args = parser.parse_args()
    try:
        result = prepare_clean_restart(
            repo_root=Path(args.repo_root),
            task_dir=Path(args.task_dir),
            attempt_id=args.attempt_id,
            task_base_commit=args.task_base_commit,
            new_branch=args.new_branch,
            new_worktree=args.new_worktree,
        )
    except (CleanRestartError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"clean restart error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
