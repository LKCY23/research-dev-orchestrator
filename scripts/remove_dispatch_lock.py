#!/usr/bin/env python3
"""Remove a dispatch lock after user-approved Lock Recovery Review."""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protocol import append_event, load_json, repo_root, utc_now, write_json


def stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
    operation = {
        "at": utc_now(),
        "actor": "coordinator",
        "operation": "remove_dispatch_lock",
        "run_id": args.run_id,
        "task_id": args.task_id,
        "attempt_id": attempt_id,
        "reason": args.reason,
        "target": str(dispatch_lock),
        "snapshot": str(rel_snapshot),
        "will_not_modify": [
            "STATUS.json",
            "ATTEMPT.json",
            "LOCK",
            "HANDOFF.md",
            "EVIDENCE.md",
            "FSM state",
        ],
    }
    write_json(snapshot_dir / "recovery-operation.json", operation)
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
    try:
        append_event(run_dir, event)
    except Exception as exc:
        failure = {
            "at": utc_now(),
            "operation": "append_dispatch_lock_removed_event",
            "run_id": args.run_id,
            "task_id": args.task_id,
            "attempt_id": attempt_id,
            "event": event,
            "error": repr(exc),
            "status": "failed_after_dispatch_lock_removed",
        }
        write_json(snapshot_dir / "recovery-event-append-failed.json", failure)
        print("Removed .dispatch-lock, but failed to append dispatch_lock_removed event.", flush=True)
        print(f"Emergency audit record: {snapshot_dir / 'recovery-event-append-failed.json'}", flush=True)
        return 2

    print("Removed .dispatch-lock and appended dispatch_lock_removed event.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
