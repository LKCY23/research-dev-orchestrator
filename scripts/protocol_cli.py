#!/usr/bin/env python3
"""Narrow CLI for dispatch_claude.sh protocol operations."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protocol import (
    BLOCKER_TYPES,
    TEMPLATE_MARKERS,
    append_event as append_event_line,
    load_json,
    parse_iso,
    utc_now,
    write_json,
)


def add_common_event_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)


def substantive(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return False
    marker = TEMPLATE_MARKERS.get(path.name)
    return not (marker and marker in text)


def cmd_check_dispatch_transition(args: argparse.Namespace) -> int:
    status = load_json(Path(args.status_path))
    fsm = load_json(Path(args.fsm_path))
    state = status.get("state")
    allowed = fsm["transitions"].get(state, {}).get("running", [])
    if "dispatch" not in allowed:
        raise SystemExit(f"illegal dispatch transition: {state!r} -> 'running'")
    return 0


def cmd_append_event(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "at": utc_now(),
        "actor": "dispatch" if args.event_name in {"task_dispatched", "worker_exit_without_valid_status"} else "claude-code",
        "event": args.event_name,
        "run_id": args.run_id,
        "task_id": args.task_id,
        "attempt_id": args.attempt_id,
    }
    if args.event_name == "task_dispatched":
        payload["worker"] = args.agent_name
    if args.event_name == "worker_blocked":
        status = load_json(Path(args.status_path))
        payload["blocker_type"] = status.get("blocker_type", "")
        payload["blocking_reason"] = status.get("blocking_reason", "")
    append_event_line(Path(args.run_dir), payload)
    return 0


def cmd_create_attempt(args: argparse.Namespace) -> int:
    command = args.command
    runtime: dict[str, Any] = {
        "backend": args.backend,
        "model": os.environ.get("CLAUDE_MODEL"),
        "cli": command.split()[0] if command.split() else command,
        "command": command,
        "cwd": args.cwd,
    }
    if args.backend == "tmux":
        runtime["tmux_session"] = args.tmux_session
        runtime["attach_command"] = args.attach_command

    payload = {
        "attempt_id": args.attempt_id,
        "task_id": args.task_id,
        "agent": "claude-code",
        "agent_name": args.agent_name,
        "session_id": args.session_id,
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
    status["owner"] = "claude-code"
    status["updated_at"] = now
    status["needs_coordinator"] = False
    status["blocking_reason"] = ""
    status["blocker_type"] = ""
    status["current_attempt_id"] = args.attempt_id
    status["assigned_worker"] = {
        "agent": "claude-code",
        "agent_name": args.agent_name,
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
    status_path = Path(args.status_path)
    attempt_path = Path(args.attempt_path)
    task_dir = Path(args.task_dir)
    exit_code_raw = args.exit_code_raw
    try:
        exit_code = int(exit_code_raw)
        exit_code_valid = True
    except (TypeError, ValueError):
        exit_code = None
        exit_code_valid = False

    status = load_json(status_path)
    state = status.get("state")
    valid_state = state in {"review", "blocked"}
    valid_attempt = status.get("current_attempt_id") == args.attempt_id
    valid_previous = status.get("previous_state") == "running"
    history = status.get("state_history") if isinstance(status.get("state_history"), list) else []
    last_transition = history[-1] if history else {}
    valid_history = (
        isinstance(last_transition, dict)
        and last_transition.get("from") == "running"
        and last_transition.get("to") == state
        and last_transition.get("actor") == "claude-code"
        and parse_iso(last_transition.get("at")) is not None
    )
    handoff_ok = substantive(task_dir / "HANDOFF.md")
    evidence_ok = substantive(task_dir / "EVIDENCE.md")
    if state == "review":
        artifacts_ok = handoff_ok and evidence_ok
        exit_ok = exit_code_valid and exit_code == 0
    elif state == "blocked":
        artifacts_ok = handoff_ok and status.get("blocker_type") in BLOCKER_TYPES and bool(status.get("blocking_reason"))
        exit_ok = exit_code_valid
    else:
        artifacts_ok = False
        exit_ok = False

    attempt = load_json(attempt_path)
    attempt["ended_at"] = utc_now()
    attempt["exit_code"] = exit_code

    if not (exit_code_valid and valid_state and valid_attempt and valid_previous and valid_history and artifacts_ok and exit_ok):
        attempt["state"] = "invalid_handoff"
        attempt["handoff_valid"] = False
        attempt["handoff_state"] = None
        write_json(attempt_path, attempt)
        print("worker_exit_without_valid_status", file=sys.stderr)
        print(f"state={state!r} current_attempt_id={status.get('current_attempt_id')!r}", file=sys.stderr)
        print(
            f"exit_code_raw={exit_code_raw!r} exit_code={exit_code!r} "
            f"exit_code_valid={exit_code_valid!r} exit_ok={exit_ok!r}",
            file=sys.stderr,
        )
        print(f"valid_previous={valid_previous!r}", file=sys.stderr)
        print(f"valid_history={valid_history!r}", file=sys.stderr)
        return 4

    attempt["state"] = "completed"
    attempt["handoff_valid"] = True
    attempt["handoff_state"] = state
    write_json(attempt_path, attempt)
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
    parser = argparse.ArgumentParser(description="Internal protocol operations for dispatch_claude.sh.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-dispatch-transition")
    check.add_argument("--status-path", required=True)
    check.add_argument("--fsm-path", required=True)
    check.set_defaults(func=cmd_check_dispatch_transition)

    event = sub.add_parser("append-event")
    add_common_event_args(event)
    event.add_argument("--event-name", required=True)
    event.add_argument("--agent-name", required=True)
    event.add_argument("--status-path", required=True)
    event.set_defaults(func=cmd_append_event)

    attempt = sub.add_parser("create-attempt")
    attempt.add_argument("--path", required=True)
    attempt.add_argument("--attempt-id", required=True)
    attempt.add_argument("--task-id", required=True)
    attempt.add_argument("--agent-name", required=True)
    attempt.add_argument("--session-id", default="")
    attempt.add_argument("--command", required=True)
    attempt.add_argument("--cwd", required=True)
    attempt.add_argument("--backend", required=True)
    attempt.add_argument("--tmux-session", default="")
    attempt.add_argument("--attach-command", default="")
    attempt.set_defaults(func=cmd_create_attempt)

    transition = sub.add_parser("transition-running")
    transition.add_argument("--status-path", required=True)
    transition.add_argument("--fsm-path", required=True)
    transition.add_argument("--attempt-id", required=True)
    transition.add_argument("--agent-name", required=True)
    transition.add_argument("--session-id", default="")
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
