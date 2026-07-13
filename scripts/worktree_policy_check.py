#!/usr/bin/env python3
"""Validate worktree content changes against one approved strategy envelope."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def contains(parent: str, child: str) -> bool:
    parent = parent.replace("\\", "/").rstrip("/") or "."
    child = child.replace("\\", "/").rstrip("/") or "."
    return parent == "." or child == parent or child.startswith(parent + "/")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check changed files against approved workflow write paths.")
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--policy", required=True)
    args = parser.parse_args()
    before = {item["path"]: item["sha256"] for item in json.loads(Path(args.before).read_text())["entries"]}
    after = {item["path"]: item["sha256"] for item in json.loads(Path(args.after).read_text())["entries"]}
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    strategy = json.loads(Path(args.strategy).read_text())
    policy = json.loads(Path(args.policy).read_text())
    allowed = [
        path
        for workflow in strategy["workflows"]
        if workflow["executor"]["write_access"]
        for path in workflow["executor"]["allowed_paths"]
    ]
    forbidden = policy["forbidden_paths"]
    violations = [
        path for path in changed
        if not any(contains(parent, path) for parent in allowed)
        or any(contains(parent, path) for parent in forbidden)
    ]
    print(json.dumps({"changed_paths": changed, "violations": violations}, indent=2))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
