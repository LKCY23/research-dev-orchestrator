#!/usr/bin/env python3
"""Best-effort Codex PreToolUse adapter for common shell reads."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from protocol import load_json, utc_now
from read_policy import evaluate_read


READ_COMMANDS = {"cat", "head", "tail", "sed"}
SEARCH_COMMANDS = {"rg", "grep"}
LIST_COMMANDS = {"find", "ls"}


def append_event(runtime: Path, payload: dict[str, Any]) -> None:
    with (runtime / "CONTEXT_HOOK_EVENTS.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    result: list[list[str]] = [[]]
    for token in lexer:
        if token and all(character in "|&;()" for character in token):
            if result[-1]:
                result.append([])
        else:
            result[-1].append(token)
    return [item for item in result if item]


def existing_paths(arguments: list[str], cwd: Path) -> list[Path]:
    paths: list[Path] = []
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument in {"-e", "--regexp", "-f", "--file", "-m", "--max-count", "-A", "-B", "-C"}:
            skip_next = True
            continue
        if argument.startswith("-") or argument.isdigit() or argument in {">", ">>", "<"}:
            continue
        candidate = Path(argument)
        candidate = (candidate if candidate.is_absolute() else cwd / candidate).resolve()
        if candidate.exists():
            paths.append(candidate)
    return paths


def classify(segment: list[str], cwd: Path) -> tuple[str, list[Path], bool] | None:
    if not segment:
        return None
    command = Path(segment[0]).name
    arguments = segment[1:]
    paths = existing_paths(arguments, cwd)
    if command in READ_COMMANDS:
        bounded = command in {"head", "tail"} or (command == "sed" and "-n" in arguments)
        return "Read", paths, bounded
    if command in SEARCH_COMMANDS:
        return "Grep", paths, True
    if command in LIST_COMMANDS:
        return "Glob", paths, True
    return None


def main() -> int:
    profile_path = os.environ.get("RDO_BACKEND_PROFILE", "")
    if not profile_path:
        return 0
    runtime = Path(profile_path).resolve().parent
    policy = load_json(runtime / "READ_POLICY.json")
    hook_input = json.load(sys.stdin)
    if hook_input.get("tool_name") != "Bash":
        return 0
    command = str((hook_input.get("tool_input") or {}).get("command") or "")
    cwd = Path(str(hook_input.get("cwd") or policy["worktree"])).resolve()
    classified = 0
    for segment in segments(command):
        details = classify(segment, cwd)
        if details is None:
            continue
        operation, paths, bounded = details
        classified += 1
        for path in paths:
            tool_input: dict[str, Any] = {"file_path": str(path)}
            if bounded:
                tool_input["limit"] = 1
            reason = evaluate_read(policy, tool_input, operation)
            if reason:
                append_event(runtime, {
                    "at": utc_now(), "backend": "codex", "event": "read_denied",
                    "operation": operation, "path": str(path), "reason": reason,
                })
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }))
                return 0
    append_event(runtime, {
        "at": utc_now(), "backend": "codex",
        "event": "shell_read_classified" if classified else "shell_read_unclassified",
        "classified_segments": classified,
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Codex interception is explicitly best-effort. Record diagnostics on
        # stderr and allow the tool rather than claiming a hard boundary.
        print(f"RDO Codex read hook allowed after adapter error: {exc}", file=sys.stderr)
        raise SystemExit(0)
