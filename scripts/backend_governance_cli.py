#!/usr/bin/env python3
"""CLI bridge for pure backend-profile compilation and locked materialization."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend_governance import BackendGovernanceError, compile_backend_profile, materialize_backend_profile


def compile_action(args: argparse.Namespace) -> int:
    profile = compile_backend_profile(
        repo_root=Path(args.repo_root).resolve(),
        task_dir=Path(args.task_dir).resolve(),
        backend_id=args.backend,
        phase=args.phase,
        strategy_path=Path(args.strategy).resolve() if args.strategy else None,
    )
    print(json.dumps(profile, sort_keys=True))
    return 0


def materialize_action(args: argparse.Namespace) -> int:
    profile = json.loads(args.profile_json)
    result = materialize_backend_profile(profile, Path(args.runtime_dir).resolve())
    print(json.dumps(result, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile backend governance for one attempt")
    sub = parser.add_subparsers(dest="action", required=True)
    compile_cmd = sub.add_parser("compile")
    compile_cmd.add_argument("--repo-root", required=True)
    compile_cmd.add_argument("--task-dir", required=True)
    compile_cmd.add_argument("--backend", required=True)
    compile_cmd.add_argument("--phase", choices=["planning", "execution"], required=True)
    compile_cmd.add_argument("--strategy", default="")
    compile_cmd.set_defaults(func=compile_action)
    materialize = sub.add_parser("materialize")
    materialize.add_argument("--profile-json", required=True)
    materialize.add_argument("--runtime-dir", required=True)
    materialize.set_defaults(func=materialize_action)
    args = parser.parse_args()
    try:
        return args.func(args)
    except (BackendGovernanceError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"backend governance error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
