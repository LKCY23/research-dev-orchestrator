#!/usr/bin/env python3
"""Narrow CLI for dispatch protocol operations."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protocol import (
    append_event as append_event_line,
    load_json,
    utc_now,
    write_json,
)
from validation import HandoffValidationResult, validate_worker_handoff


def reset_status_to_running(status: dict[str, Any]) -> None:
    """Discard worker-written terminal state and restore dispatch-owned running state."""

    history = status.get("state_history")
    if not isinstance(history, list):
        status["state_history"] = []
        status["state"] = "running"
        status["previous_state"] = None
        return
    running_index = None
    for idx in range(len(history) - 1, -1, -1):
        item = history[idx]
        if isinstance(item, dict) and item.get("to") == "running" and item.get("actor") == "dispatch":
            running_index = idx
            break
    if running_index is not None:
        del history[running_index + 1 :]
        item = history[running_index]
        status["previous_state"] = item.get("from")
    status["state"] = "running"


def apply_dispatch_terminal_transition(
    status_path: Path,
    *,
    target_state: str,
    actor: str = "dispatch",
    summary: str = "",
    needs_coordinator: bool = False,
    blocker_type: str = "",
    blocking_reason: str = "",
    commands_run: list[Any] | None = None,
) -> None:
    status = load_json(status_path)
    if not isinstance(status, dict):
        raise ValueError("STATUS.json must be a JSON object")
    reset_status_to_running(status)
    now = utc_now()
    status["previous_state"] = "running"
    status["state"] = target_state
    status["updated_at"] = now
    status["owner"] = "worker"
    status["summary"] = summary or status.get("summary", "")
    status["needs_coordinator"] = needs_coordinator
    status["blocker_type"] = blocker_type
    status["blocking_reason"] = blocking_reason
    evidence = status.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {"commands_run": [], "logs": [], "passed": None}
    if commands_run is not None:
        evidence["commands_run"] = [str(command) for command in commands_run]
    status["evidence"] = evidence
    status.setdefault("state_history", []).append({
        "from": "running",
        "to": target_state,
        "actor": actor,
        "at": now,
    })
    write_json(status_path, status)


def add_common_event_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)


def cmd_check_dispatch_transition(args: argparse.Namespace) -> int:
    status = load_json(Path(args.status_path))
    fsm = load_json(Path(args.fsm_path))
    state = status.get("state")
    allowed = fsm["transitions"].get(state, {}).get("running", [])
    if "dispatch" not in allowed:
        raise SystemExit(f"illegal dispatch transition: {state!r} -> 'running'")
    return 0


def cmd_append_event(args: argparse.Namespace) -> int:
    dispatch_events = {"task_dispatched", "worker_exit_without_valid_status", "worker_blocked", "worker_review_ready"}
    payload: dict[str, Any] = {
        "at": utc_now(),
        "actor": "dispatch" if args.event_name in dispatch_events else "coordinator",
        "event": args.event_name,
        "run_id": args.run_id,
        "task_id": args.task_id,
        "attempt_id": args.attempt_id,
    }
    if args.event_name == "task_dispatched":
        payload["worker"] = args.agent_name
        payload["worker_backend"] = getattr(args, "worker_backend", "")
    if args.event_name == "worker_blocked":
        status = load_json(Path(args.status_path))
        payload["blocker_type"] = status.get("blocker_type", "")
        payload["blocking_reason"] = status.get("blocking_reason", "")
    append_event_line(Path(args.run_dir), payload)
    return 0


def cmd_create_attempt(args: argparse.Namespace) -> int:
    command = args.command
    command_parts = shlex.split(command)
    runtime: dict[str, Any] = {
        "backend": args.runtime_backend,
        "runtime_backend": args.runtime_backend,
        "io_mode": args.io_mode,
        "model": os.environ.get("CLAUDE_MODEL"),
        "cli": command_parts[0] if command_parts else command,
        "command": command,
        "cwd": args.cwd,
    }
    if args.runtime_backend == "tmux":
        runtime["tmux_session"] = args.tmux_session
        runtime["attach_command"] = args.attach_command

    payload = {
        "attempt_id": args.attempt_id,
        "task_id": args.task_id,
        "role": "worker",
        "backend_id": args.worker_backend,
        "agent": args.worker_backend,
        "agent_name": args.agent_name,
        "backend_session_id": args.session_id,
        "session_id": args.session_id,
        "execution_mode": args.execution_mode,
        "permission_mode": args.permission_mode,
        "state": "created",
        "handoff_valid": None,
        "handoff_state": None,
        "started_at": utc_now(),
        "ended_at": None,
        "exit_code": None,
        "runtime": runtime,
    }
    write_json(Path(args.path), payload)
    return 0


def cmd_transition_running(args: argparse.Namespace) -> int:
    status_path = Path(args.status_path)
    status = load_json(status_path)
    fsm = load_json(Path(args.fsm_path))
    state = status.get("state")
    allowed = fsm["transitions"].get(state, {}).get("running", [])
    if "dispatch" not in allowed:
        raise SystemExit(f"illegal dispatch transition: {state!r} -> 'running'")
    now = utc_now()
    status["previous_state"] = state
    status["state"] = "running"
    status["owner"] = "worker"
    status["updated_at"] = now
    status["needs_coordinator"] = False
    status["blocking_reason"] = ""
    status["blocker_type"] = ""
    status["current_attempt_id"] = args.attempt_id
    status["assigned_worker"] = {
        "backend_id": args.worker_backend,
        "agent": args.worker_backend,
        "agent_name": args.agent_name,
        "backend_session_id": args.session_id,
        "session_id": args.session_id,
        "role": "worker",
    }
    status.setdefault("state_history", []).append({
        "from": state,
        "to": "running",
        "actor": "dispatch",
        "at": now,
    })
    write_json(status_path, status)
    return 0


def cmd_set_attempt_running(args: argparse.Namespace) -> int:
    path = Path(args.attempt_path)
    attempt = load_json(path)
    attempt["state"] = "running"
    write_json(path, attempt)
    return 0


def cmd_validate_handoff(args: argparse.Namespace) -> int:
    attempt_path = Path(args.attempt_path)
    task_dir = Path(args.task_dir)
    status_error = None
    try:
        status = load_json(Path(args.status_path))
    except json.JSONDecodeError as exc:
        status = None
        status_error = f"invalid STATUS.json: {exc}"
    result = validate_worker_handoff(status, args.attempt_id, task_dir, args.exit_code_raw)
    if status_error:
        result = HandoffValidationResult(
            valid=False,
            handoff_state=None,
            exit_code=result.exit_code,
            reasons=[status_error, *result.reasons],
        )

    try:
        attempt = load_json(attempt_path)
    except json.JSONDecodeError as exc:
        print("worker_exit_without_valid_status", file=sys.stderr)
        print(f"- invalid ATTEMPT.json: {exc}", file=sys.stderr)
        for reason in result.reasons:
            print(f"- {reason}", file=sys.stderr)
        return 4
    if not isinstance(attempt, dict):
        print("worker_exit_without_valid_status", file=sys.stderr)
        print("- ATTEMPT.json must be a JSON object", file=sys.stderr)
        for reason in result.reasons:
            print(f"- {reason}", file=sys.stderr)
        return 4
    attempt["ended_at"] = utc_now()
    attempt["exit_code"] = result.exit_code

    if not result.valid:
        attempt["state"] = "invalid_handoff"
        attempt["handoff_valid"] = False
        attempt["handoff_state"] = None
        write_json(attempt_path, attempt)
        try:
            apply_dispatch_terminal_transition(
                Path(args.status_path),
                target_state="blocked",
                summary="Invalid worker handoff",
                needs_coordinator=True,
                blocker_type="needs_coordinator",
                blocking_reason="invalid worker handoff: " + "; ".join(result.reasons[:3]),
            )
        except Exception as exc:
            print(f"- failed to mark task blocked after invalid handoff: {exc}", file=sys.stderr)
        print("worker_exit_without_valid_status", file=sys.stderr)
        for reason in result.reasons:
            print(f"- {reason}", file=sys.stderr)
        return 4

    attempt["state"] = "completed"
    attempt["handoff_valid"] = True
    attempt["handoff_state"] = result.handoff_state
    write_json(attempt_path, attempt)
    request = result.request or {}
    if result.handoff_state == "blocked":
        blocker_type = str(request.get("blocker_type") or "")
        blocking_reason = str(request.get("blocking_reason") or "")
        needs_coordinator = bool(request.get("needs_coordinator", True))
    else:
        blocker_type = ""
        blocking_reason = ""
        needs_coordinator = bool(request.get("needs_coordinator", False))
    commands_run = request.get("commands_run") if isinstance(request.get("commands_run"), list) else None
    apply_dispatch_terminal_transition(
        Path(args.status_path),
        target_state=str(result.handoff_state),
        summary=str(request.get("summary") or ""),
        needs_coordinator=needs_coordinator,
        blocker_type=blocker_type,
        blocking_reason=blocking_reason,
        commands_run=commands_run,
    )
    return 0


def cmd_write_dispatch_diagnostics(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    diagnostics_dir = run_dir / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = diagnostics_dir / f"dispatch-failure-{args.task_id}-{stamp}.md"
    path.write_text(
        "\n".join(
            [
                "# Dispatch Failure",
                "",
                f"- run_id: {args.run_id}",
                f"- task_id: {args.task_id}",
                f"- exit_code: {args.exit_code}",
                f"- status_updated: {args.status_updated}",
                f"- attempt_id: {args.attempt_id or ''}",
                f"- lock_path: {args.lock_path}",
                f"- dispatch_lock_dir: {args.dispatch_lock_dir}",
                f"- time: {utc_now()}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return 0


def cmd_write_tmux_timeout_diagnostics(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    diagnostics_dir = run_dir / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = diagnostics_dir / f"tmux-wait-timeout-{args.task_id}-{stamp}.json"
    md_path = diagnostics_dir / f"tmux-wait-timeout-{args.task_id}-{stamp}.md"
    payload = {
        "at": utc_now(),
        "reason": "tmux_wait_timeout",
        "run_id": args.run_id,
        "task_id": args.task_id,
        "attempt_id": args.attempt_id,
        "tmux_session": args.tmux_session,
        "attach_command": args.attach_command,
        "timeout_seconds": int(args.timeout_seconds),
        "dispatch_exit_code": 5,
        "worker_exit_code": None,
        "dispatch_lock_retained": True,
        "dispatch_lock_dir": args.dispatch_lock_dir,
        "attempt_dir": args.attempt_dir,
    }
    write_json(json_path, payload, sort_keys=True)
    md_path.write_text(
        "\n".join(
            [
                "# Tmux Wait Timeout",
                "",
                f"- run_id: {args.run_id}",
                f"- task_id: {args.task_id}",
                f"- attempt_id: {args.attempt_id}",
                "- reason: tmux_wait_timeout",
                "- dispatch_exit_code: 5",
                "- worker_exit_code: null",
                "- dispatch_lock_retained: true",
                f"- tmux_session: {args.tmux_session}",
                f"- attach_command: {args.attach_command}",
                f"- timeout_seconds: {args.timeout_seconds}",
                f"- time: {utc_now()}",
                "",
                "Dispatch lost supervision before the attempt-local exit_code file appeared.",
                "Do not assume the worker stopped. Use Lock Recovery Review before removing .dispatch-lock.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal protocol operations for dispatch.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-dispatch-transition")
    check.add_argument("--status-path", required=True)
    check.add_argument("--fsm-path", required=True)
    check.set_defaults(func=cmd_check_dispatch_transition)

    event = sub.add_parser("append-event")
    add_common_event_args(event)
    event.add_argument("--event-name", required=True)
    event.add_argument("--agent-name", required=True)
    event.add_argument("--worker-backend", default="")
    event.add_argument("--status-path", required=True)
    event.set_defaults(func=cmd_append_event)

    attempt = sub.add_parser("create-attempt")
    attempt.add_argument("--path", required=True)
    attempt.add_argument("--attempt-id", required=True)
    attempt.add_argument("--task-id", required=True)
    attempt.add_argument("--agent-name", required=True)
    attempt.add_argument("--session-id", default="")
    attempt.add_argument("--worker-backend", default="claude-code")
    attempt.add_argument("--execution-mode", default="start")
    attempt.add_argument("--permission-mode", default="auto")
    attempt.add_argument("--io-mode", default="machine")
    attempt.add_argument("--command", required=True)
    attempt.add_argument("--cwd", required=True)
    attempt.add_argument("--backend", dest="runtime_backend", required=True)
    attempt.add_argument("--runtime-backend", dest="runtime_backend", default=None)
    attempt.add_argument("--tmux-session", default="")
    attempt.add_argument("--attach-command", default="")
    attempt.set_defaults(func=cmd_create_attempt)

    transition = sub.add_parser("transition-running")
    transition.add_argument("--status-path", required=True)
    transition.add_argument("--fsm-path", required=True)
    transition.add_argument("--attempt-id", required=True)
    transition.add_argument("--agent-name", required=True)
    transition.add_argument("--session-id", default="")
    transition.add_argument("--worker-backend", default="claude-code")
    transition.set_defaults(func=cmd_transition_running)

    set_running = sub.add_parser("set-attempt-running")
    set_running.add_argument("--attempt-path", required=True)
    set_running.set_defaults(func=cmd_set_attempt_running)

    validate = sub.add_parser("validate-handoff")
    validate.add_argument("--status-path", required=True)
    validate.add_argument("--attempt-id", required=True)
    validate.add_argument("--task-dir", required=True)
    validate.add_argument("--attempt-path", required=True)
    validate.add_argument("--exit-code-raw", required=True)
    validate.set_defaults(func=cmd_validate_handoff)

    diagnostics = sub.add_parser("write-dispatch-diagnostics")
    diagnostics.add_argument("--run-dir", required=True)
    diagnostics.add_argument("--run-id", required=True)
    diagnostics.add_argument("--task-id", required=True)
    diagnostics.add_argument("--attempt-id", default="")
    diagnostics.add_argument("--exit-code", required=True)
    diagnostics.add_argument("--status-updated", required=True)
    diagnostics.add_argument("--lock-path", required=True)
    diagnostics.add_argument("--dispatch-lock-dir", required=True)
    diagnostics.set_defaults(func=cmd_write_dispatch_diagnostics)

    tmux_timeout = sub.add_parser("write-tmux-timeout-diagnostics")
    tmux_timeout.add_argument("--run-dir", required=True)
    tmux_timeout.add_argument("--run-id", required=True)
    tmux_timeout.add_argument("--task-id", required=True)
    tmux_timeout.add_argument("--attempt-id", required=True)
    tmux_timeout.add_argument("--tmux-session", required=True)
    tmux_timeout.add_argument("--attach-command", required=True)
    tmux_timeout.add_argument("--timeout-seconds", required=True)
    tmux_timeout.add_argument("--dispatch-lock-dir", required=True)
    tmux_timeout.add_argument("--attempt-dir", required=True)
    tmux_timeout.set_defaults(func=cmd_write_tmux_timeout_diagnostics)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
