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
from read_policy import evaluate_read, record_context_access


READ_COMMANDS = {"cat", "head", "tail", "sed"}
SEARCH_COMMANDS = {"rg", "grep"}
LIST_COMMANDS = {"find", "ls"}


def append_event(runtime: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(
        runtime / "CONTEXT_HOOK_EVENTS.ndjson",
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise OSError(f"short context hook log write: {written}/{len(encoded)} bytes")
    finally:
        os.close(descriptor)


def append_event_safely(runtime: Path, payload: dict[str, Any]) -> None:
    try:
        append_event(runtime, payload)
    except Exception as exc:
        print(f"RDO context diagnostic append failed: {exc}", file=sys.stderr)


def display_scope(policy: dict[str, Any], cwd: Path) -> str:
    worktree = Path(policy["worktree"]).resolve()
    try:
        return cwd.relative_to(worktree).as_posix() or "."
    except ValueError:
        return "outside_worktree"


def display_path(policy: dict[str, Any], path: Path) -> str:
    worktree = Path(policy["worktree"]).resolve()
    try:
        return path.resolve().relative_to(worktree).as_posix() or "."
    except ValueError:
        return "outside_worktree"


def record_context_access_safely(**kwargs: Any) -> None:
    try:
        record_context_access(**kwargs)
    except Exception as exc:
        # Codex interception is best-effort, but telemetry health must not
        # replace a policy decision that was already computed.
        print(f"RDO context telemetry append failed: {exc}", file=sys.stderr)


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
        if not paths:
            # Grep/Glob without a path use cwd as their native search scope.
            # A Read command with no resolvable path was already allowed by
            # this best-effort adapter; keep that behavior and expose the
            # coverage gap rather than inventing a hard denial.
            tool_input: dict[str, Any] = {}
            reason = evaluate_read(policy, tool_input, operation) if operation != "Read" else None
            record_context_access_safely(
                runtime=runtime,
                policy=policy,
                backend="codex",
                operation=operation,
                tool_input=tool_input,
                decision="deny" if reason else "allow",
                reason=reason or (
                    "" if operation != "Read" else "no resolvable path in shell command"
                ),
                coverage="best_effort",
                scope=display_scope(policy, cwd),
                bounded=bounded,
            )
            if reason:
                append_event_safely(runtime, {
                    "at": utc_now(), "backend": "codex", "event": "read_denied",
                    "operation": operation, "scope": display_scope(policy, cwd), "reason": reason,
                })
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }))
                return 0
        for path in paths:
            tool_input: dict[str, Any] = {"file_path": str(path)}
            policy_input = dict(tool_input)
            if bounded:
                policy_input["limit"] = 1
            reason = evaluate_read(policy, policy_input, operation)
            record_context_access_safely(
                runtime=runtime,
                policy=policy,
                backend="codex",
                operation=operation,
                tool_input=tool_input,
                decision="deny" if reason else "allow",
                reason=reason or "",
                coverage="best_effort",
                bounded=bounded,
            )
            if reason:
                append_event_safely(runtime, {
                    "at": utc_now(), "backend": "codex", "event": "read_denied",
                    "operation": operation, "path": display_path(policy, path), "reason": reason,
                })
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }))
                return 0
    append_event_safely(runtime, {
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
