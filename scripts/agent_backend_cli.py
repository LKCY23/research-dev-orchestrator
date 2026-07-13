#!/usr/bin/env python3
"""CLI for inspecting and rendering agent backend commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_backends import BACKENDS_DIR, build_command, load_backend, validate_backend


def cmd_list(_: argparse.Namespace) -> int:
    for path in sorted(BACKENDS_DIR.glob("*.toml")):
        print(path.stem)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    ids = [path.stem for path in sorted(BACKENDS_DIR.glob("*.toml"))] if args.backend == "all" else [args.backend]
    failed = False
    for backend_id in ids:
        try:
            payload = load_backend(backend_id)
            errors = validate_backend(payload)
        except Exception as exc:
            errors = [str(exc)]
        if errors:
            failed = True
            for error in errors:
                print(f"{backend_id}: {error}", file=sys.stderr)
        else:
            print(f"{backend_id}: valid")
    return 2 if failed else 0


def cmd_command(args: argparse.Namespace) -> int:
    prompt = args.prompt
    if args.prompt_path:
        prompt = Path(args.prompt_path).read_text(encoding="utf-8")
    rendered = build_command(
        backend_id=args.backend,
        io_mode=args.io_mode,
        permission_mode=args.permission_mode,
        cwd=args.cwd,
        prompt=prompt,
        agent_name=args.agent_name,
        backend_profile=args.backend_profile,
    )
    if args.json:
        print(json.dumps(rendered.__dict__, indent=2))
    else:
        print(rendered.command)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent backend registry utilities.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list")
    list_cmd.set_defaults(func=cmd_list)

    validate = sub.add_parser("validate")
    validate.add_argument("--backend", default="all")
    validate.set_defaults(func=cmd_validate)

    command = sub.add_parser("command")
    command.add_argument("--backend", required=True)
    command.add_argument("--io-mode", required=True, choices=["machine", "human"])
    command.add_argument("--permission-mode", required=True, choices=["default", "auto", "yolo"])
    command.add_argument("--cwd", required=True)
    command.add_argument("--prompt", default="")
    command.add_argument("--prompt-path", default="")
    command.add_argument("--agent-name", default="")
    command.add_argument("--backend-profile", default="")
    command.add_argument("--json", action="store_true")
    command.set_defaults(func=cmd_command)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
