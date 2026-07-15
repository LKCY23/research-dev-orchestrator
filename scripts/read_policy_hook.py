#!/usr/bin/env python3
"""Command-hook adapter for backend-neutral context read policy."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from protocol import load_json
from read_policy import evaluate_read, normalize_tool_input


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--backend", required=True, choices=["claude-code", "kimi-code", "opencode"])
    parser.add_argument("--format", choices=["hook", "decision"], default="hook")
    args = parser.parse_args()
    if args.runtime_dir:
        runtime = Path(args.runtime_dir).resolve()
    else:
        profile_path = os.environ.get("RDO_BACKEND_PROFILE", "")
        if not profile_path:
            raise ValueError("runtime directory or RDO_BACKEND_PROFILE is required")
        runtime = Path(profile_path).resolve().parent
    policy = load_json(runtime / "READ_POLICY.json")
    hook_input = json.load(sys.stdin)
    tool_name, tool_input = normalize_tool_input(
        args.backend,
        str(hook_input.get("tool_name") or ""),
        hook_input.get("tool_input", {}),
    )
    reason = evaluate_read(policy, tool_input, tool_name)
    if args.format == "decision":
        print(json.dumps({"decision": "deny" if reason else "allow", "reason": reason or ""}))
    elif reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"RDO read policy hook failed closed: {exc}", file=sys.stderr)
        raise SystemExit(2)
