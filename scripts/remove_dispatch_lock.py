#!/usr/bin/env python3
"""Remove a dispatch lock after user-approved Lock Recovery Review."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_git(args: list[str], cwd: Path, default: str = "") -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return default


def repo_root(cwd: Path) -> Path:
    root = run_git(["rev-parse", "--show-toplevel"], cwd)
    return Path(root) if root else cwd


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def append_event(run_dir: Path, event: dict[str, Any]) -> None:
    with (run_dir / "EVENTS.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def unique_snapshot_dir(diagnostics_dir: Path, task_id: str, stamp: str) -> Path:
    base = diagnostics_dir / f"dispatch-lock-removed-{task_id}-{stamp}"
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = diagnostics_dir / f"dispatch-lock-removed-{task_id}-{stamp}-{index}"
        if not candidate.exists():
            return candidate
    raise SystemExit("Could not allocate unique diagnostics snapshot directory")


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove .dispatch-lock after approved Lock Recovery Review.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--confirmed", action="store_true", help="Actually snapshot and remove .dispatch-lock.")
    args = parser.parse_args()

    if not args.reason.strip():
        raise SystemExit("--reason must be non-empty")

    root = repo_root(Path.cwd())
    run_dir = root / ".agent-collab" / "runs" / args.run_id
    task_dir = run_dir / "tasks" / args.task_id
    status_path = task_dir / "STATUS.json"
    dispatch_lock = task_dir / ".dispatch-lock"
    diagnostics_dir = run_dir / "diagnostics"

    if not run_dir.exists():
        raise SystemExit(f"Run not found: {run_dir}")
    if not task_dir.exists():
        raise SystemExit(f"Task not found: {task_dir}")
    if not status_path.exists():
        raise SystemExit(f"STATUS.json not found: {status_path}")
    if not dispatch_lock.is_dir():
        raise SystemExit(f".dispatch-lock not found: {dispatch_lock}")

    status = load_json(status_path)
    attempt_id = status.get("current_attempt_id")
    stamp = stamp_now()
    snapshot_dir = unique_snapshot_dir(diagnostics_dir, args.task_id, stamp)
    rel_snapshot = snapshot_dir.relative_to(run_dir)

    print("Lock Recovery Removal")
    print(f"- run_id: {args.run_id}")
    print(f"- task_id: {args.task_id}")
    print(f"- attempt_id: {attempt_id or ''}")
    print(f"- reason: {args.reason}")
    print(f"- snapshot: {snapshot_dir}")
    print(f"- target: {dispatch_lock}")

    if not args.confirmed:
        print("")
        print("DRY RUN: no files were modified. Re-run with --confirmed after user approval.")
        return 1

    diagnostics_dir.mkdir(exist_ok=True)
    shutil.copytree(dispatch_lock, snapshot_dir)
    shutil.rmtree(dispatch_lock)

    event: dict[str, Any] = {
        "at": utc_now(),
        "actor": "coordinator",
        "event": "dispatch_lock_removed",
        "run_id": args.run_id,
        "task_id": args.task_id,
        "reason": args.reason,
        "snapshot": str(rel_snapshot),
    }
    if attempt_id:
        event["attempt_id"] = attempt_id
    append_event(run_dir, event)

    print("Removed .dispatch-lock and appended dispatch_lock_removed event.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
