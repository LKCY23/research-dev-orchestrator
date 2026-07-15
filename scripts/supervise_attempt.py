#!/usr/bin/env python3
"""Run one worker command under an attempt-local deterministic supervisor."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

from completion import publication_path, validate_publication
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
    parser.add_argument("--artifact-protocol-version", choices=(1, 2), type=int, default=1)
    parser.add_argument("--publication-path", default="")
    parser.add_argument("--completion-path", default="")
    parser.add_argument("--task-dir", default="")
    parser.add_argument("--attempt-id", default="")
    parser.add_argument(
        "--completion-grace-seconds",
        "--handoff-grace-seconds",
        dest="publication_grace_seconds",
        type=float,
        default=0.5,
    )
    parser.add_argument("--finalization-path", default="")
    parser.add_argument("--finalization-timeout-seconds", type=float, default=90.0)
    args = parser.parse_args()
    if not math.isfinite(args.publication_grace_seconds) or args.publication_grace_seconds < 0:
        parser.error("publication grace seconds must be finite and non-negative")

    legacy_path = args.completion_path
    if args.publication_path and legacy_path:
        if Path(args.publication_path).resolve(strict=False) != Path(legacy_path).resolve(strict=False):
            parser.error("--publication-path and --completion-path disagree")
    configured_path = args.publication_path or legacy_path
    monitor_publication = bool(
        configured_path or args.task_dir or args.attempt_id
    ) or args.artifact_protocol_version == 2
    signal_path: Path | None = None
    if monitor_publication:
        if not args.task_dir or not args.attempt_id:
            parser.error("publication monitoring requires --task-dir and --attempt-id")
        expected = publication_path(
            Path(args.task_dir),
            args.attempt_id,
            args.artifact_protocol_version,
        )
        signal_path = Path(configured_path) if configured_path else expected
        if signal_path.resolve(strict=False) != expected.resolve(strict=False):
            parser.error(
                "publication path must be the protocol-specific path for the supervised attempt"
            )

    publication_state: dict[str, object] = {
        "artifact_protocol_version": args.artifact_protocol_version,
        "path": str(signal_path) if signal_path is not None else None,
        "valid": False,
        "reasons": [],
        "payload": None,
    }

    def publication_requested() -> bool:
        if signal_path is None:
            return False
        if not signal_path.exists():
            publication_state.update(
                valid=False,
                reasons=["publication marker is missing"],
                payload=None,
            )
            return False
        result = validate_publication(
            signal_path,
            artifact_protocol_version=args.artifact_protocol_version,
            task_dir=Path(args.task_dir),
            attempt_id=args.attempt_id,
        )
        publication_state.update(
            valid=result.valid,
            reasons=list(result.reasons),
            payload=result.payload,
        )
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
        completion_requested=publication_requested if monitor_publication else None,
        completion_grace_seconds=args.publication_grace_seconds,
        finalization_started=finalization_started if args.finalization_path else None,
        finalization_timeout_seconds=args.finalization_timeout_seconds,
    )
    publication_invalidated = bool(
        result.completion_requested and not publication_requested()
    )
    exit_code = 125 if publication_invalidated else result.exit_code
    payload = {
        "exit_code": exit_code,
        "child_exit_code": result.child_exit_code,
        "timed_out": result.timed_out,
        "completion_requested": result.completion_requested,
        "publication_invalidated": publication_invalidated,
        "finalization_timed_out": result.finalization_timed_out,
        "artifact_protocol_version": args.artifact_protocol_version,
        "publication": publication_state if monitor_publication else None,
        "completion": (
            publication_state
            if monitor_publication and args.artifact_protocol_version == 1
            else None
        ),
        "handoff_ready": (
            publication_state
            if monitor_publication and args.artifact_protocol_version == 2
            else None
        ),
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
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
