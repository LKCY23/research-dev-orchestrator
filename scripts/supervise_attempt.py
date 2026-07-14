#!/usr/bin/env python3
"""Run one worker command under an attempt-local deterministic supervisor."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from completion import validate_completion
from supervisor import run_supervised


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervise one worker attempt.")
    parser.add_argument("--timeout-seconds", required=True, type=float)
    parser.add_argument("--grace-seconds", type=float, default=2.0)
    parser.add_argument("--result", required=True)
    parser.add_argument("--cwd", default="")
    parser.add_argument("--shell-command", required=True)
    parser.add_argument("--strategy-id", default="")
    parser.add_argument("--strategy-sha256", default="")
    parser.add_argument("--completion-path", default="")
    parser.add_argument("--task-dir", default="")
    parser.add_argument("--attempt-id", default="")
    parser.add_argument("--completion-grace-seconds", type=float, default=0.5)
    parser.add_argument("--finalization-path", default="")
    parser.add_argument("--finalization-timeout-seconds", type=float, default=90.0)
    args = parser.parse_args()
    completion_state: dict[str, object] = {"valid": False, "reasons": [], "payload": None}

    def completion_requested() -> bool:
        if not args.completion_path:
            return False
        path = Path(args.completion_path)
        if not path.exists():
            return False
        result = validate_completion(path, task_dir=Path(args.task_dir), attempt_id=args.attempt_id)
        completion_state.update(valid=result.valid, reasons=list(result.reasons), payload=result.payload)
        return result.valid

    def finalization_started() -> bool:
        return bool(args.finalization_path) and Path(args.finalization_path).exists()

    result = run_supervised(
        ["/bin/bash", "-c", args.shell_command],
        timeout_seconds=args.timeout_seconds,
        grace_seconds=args.grace_seconds,
        cwd=Path(args.cwd) if args.cwd else None,
        stdin=0,
        stdout=1,
        stderr=2,
        state_path=Path(args.result).parent / "runtime" / "supervisor.json",
        completion_requested=completion_requested if args.completion_path else None,
        completion_grace_seconds=args.completion_grace_seconds,
        finalization_started=finalization_started if args.finalization_path else None,
        finalization_timeout_seconds=args.finalization_timeout_seconds,
    )
    payload = {
        "exit_code": result.exit_code,
        "child_exit_code": result.child_exit_code,
        "timed_out": result.timed_out,
        "completion_requested": result.completion_requested,
        "finalization_timed_out": result.finalization_timed_out,
        "completion": completion_state if args.completion_path else None,
        "elapsed_seconds": result.elapsed_seconds,
        "observed_pids": list(result.observed_pids),
        "observed_pgids": list(result.observed_pgids),
        "surviving_pids": list(result.surviving_pids),
        "strategy_id": args.strategy_id or None,
        "strategy_sha256": args.strategy_sha256 or None,
    }
    path = Path(args.result)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
