#!/usr/bin/env python3
"""CLI for cumulative task-budget admission and immutable snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from task_budget import (
    TaskBudgetError,
    assess_task_budget,
    write_assessment_immutable,
)


def assess_action(args: argparse.Namespace) -> int:
    payload = assess_task_budget(
        Path(args.task_dir),
        requested_attempt_wall_seconds=args.attempt_wall_seconds,
        next_attempt_id=args.next_attempt_id or None,
        artifact_protocol_version=args.artifact_protocol_version,
    )
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["admission"]["allowed"] or args.allow_denied else 3


def freeze_action(args: argparse.Namespace) -> int:
    payload = json.loads(args.assessment_json)
    path, digest = write_assessment_immutable(Path(args.attempt_dir), payload)
    print(json.dumps({"path": str(path), "sha256": digest}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    assess = sub.add_parser("assess")
    assess.add_argument("--task-dir", required=True)
    assess.add_argument("--attempt-wall-seconds", type=float)
    assess.add_argument("--next-attempt-id", default="")
    assess.add_argument("--artifact-protocol-version", type=int, choices=[1, 2], default=2)
    assess.add_argument("--allow-denied", action="store_true")
    assess.set_defaults(func=assess_action)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--attempt-dir", required=True)
    freeze.add_argument("--assessment-json", required=True)
    freeze.set_defaults(func=freeze_action)
    args = parser.parse_args()
    try:
        return args.func(args)
    except (TaskBudgetError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"task budget error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
