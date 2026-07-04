#!/usr/bin/env python3
"""Shared protocol validation rules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protocol import (
    ATTEMPT_STATES,
    ATTEMPT_EVENTS,
    BLOCKER_TYPES,
    CORE_EVENTS,
    HANDOFF_STATES,
    REQUIRED_STATUS_FIELDS,
    RUNTIME_BACKENDS,
    TASK_EVENTS,
    has_substantive_content,
    is_int_not_bool,
    is_non_empty_string,
    parse_iso,
)


@dataclass(frozen=True)
class HandoffValidationResult:
    valid: bool
    handoff_state: str | None
    exit_code: int | None
    reasons: list[str]


def parse_exit_code(exit_code_raw: str) -> tuple[int | None, str | None]:
    try:
        return int(exit_code_raw), None
    except (TypeError, ValueError):
        return None, f"exit_code must be an integer, got {exit_code_raw!r}"


def validate_status_schema(status: dict[str, Any], fsm: dict[str, Any], task_name: str) -> list[str]:
    """Validate STATUS.json fields that do not require filesystem access."""

    violations: list[str] = []
    missing = sorted(REQUIRED_STATUS_FIELDS - set(status))
    if missing:
        violations.append(f"{task_name}: missing STATUS fields: {', '.join(missing)}")

    state = status.get("state")
    states = set(fsm.get("states", []))
    if state not in states:
        violations.append(f"{task_name}: invalid state {state!r}")

    if state == "blocked":
        blocker_type = status.get("blocker_type")
        if blocker_type not in BLOCKER_TYPES:
            violations.append(f"{task_name}: blocked task has invalid blocker_type {blocker_type!r}")
        if not status.get("blocking_reason"):
            violations.append(f"{task_name}: blocked task has empty blocking_reason")

    evidence = status.get("evidence")
    if not isinstance(evidence, dict):
        violations.append(f"{task_name}: evidence must be an object")
    else:
        logs = evidence.get("logs", [])
        if not isinstance(logs, list):
            violations.append(f"{task_name}: evidence.logs must be a list")

    attempt_id = status.get("current_attempt_id")
    if state in {"running", "blocked", "review", "approved", "merged"} and not attempt_id:
        violations.append(f"{task_name}: {state} task is missing current_attempt_id")

    return violations


def validate_state_history(status: dict[str, Any], fsm: dict[str, Any], task_name: str) -> list[str]:
    """Validate FSM state_history legality and continuity."""

    violations: list[str] = []
    state = status.get("state")
    history = status.get("state_history")
    if not isinstance(history, list):
        violations.append(f"{task_name}: state_history must be a list")
        history = []

    transitions = fsm.get("transitions", {})
    for idx, item in enumerate(history):
        if not isinstance(item, dict):
            violations.append(f"{task_name}: state_history[{idx}] is not an object")
            continue
        from_state = item.get("from")
        to_state = item.get("to")
        actor = item.get("actor")
        allowed = transitions.get(from_state, {}).get(to_state)
        if allowed is None:
            violations.append(f"{task_name}: illegal transition {from_state!r} -> {to_state!r}")
        elif actor not in allowed:
            violations.append(f"{task_name}: illegal actor {actor!r} for {from_state!r} -> {to_state!r}")
        if idx == 0 and from_state != "pending":
            violations.append(f"{task_name}: first state_history transition must start from 'pending'")
        if idx > 0 and isinstance(history[idx - 1], dict):
            previous_to = history[idx - 1].get("to")
            if previous_to != from_state:
                violations.append(
                    f"{task_name}: non-continuous state_history at index {idx}: "
                    f"previous to={previous_to!r}, current from={from_state!r}"
                )

    if history:
        last = history[-1]
        if isinstance(last, dict):
            if state != last.get("to"):
                violations.append(f"{task_name}: state {state!r} does not match last history target {last.get('to')!r}")
            if status.get("previous_state") != last.get("from"):
                violations.append(f"{task_name}: previous_state does not match last history source")
    elif state != "pending":
        violations.append(f"{task_name}: non-pending task has empty state_history")

    return violations


def validate_runtime_backend(runtime: Any, task_name: str) -> tuple[list[str], dict[str, Any]]:
    """Validate ATTEMPT.runtime and return a usable runtime object."""

    violations: list[str] = []
    if not isinstance(runtime, dict):
        violations.append(f"{task_name}: ATTEMPT.runtime must be an object")
        runtime = {}

    for field in ("cli", "command", "cwd"):
        if field in runtime and not is_non_empty_string(runtime.get(field)):
            violations.append(f"{task_name}: ATTEMPT.runtime.{field} must be a non-empty string")
        elif field not in runtime:
            violations.append(f"{task_name}: ATTEMPT.runtime missing field: {field}")

    backend = runtime.get("backend")
    if backend not in RUNTIME_BACKENDS:
        violations.append(f"{task_name}: ATTEMPT.runtime.backend must be plain or tmux")
    if backend == "tmux":
        for field in ("tmux_session", "attach_command"):
            if not is_non_empty_string(runtime.get(field)):
                violations.append(f"{task_name}: ATTEMPT.runtime.{field} is required for tmux backend")

    return violations, runtime


def validate_attempt_schema(
    attempt: dict[str, Any],
    status: dict[str, Any],
    attempt_id: str,
    task_name: str,
) -> list[str]:
    """Validate ATTEMPT.json schema and pure lifecycle invariants."""

    violations: list[str] = []
    required = {
        "attempt_id",
        "task_id",
        "agent",
        "agent_name",
        "session_id",
        "state",
        "handoff_valid",
        "handoff_state",
        "started_at",
        "ended_at",
        "exit_code",
        "runtime",
    }
    missing = sorted(required - set(attempt))
    if missing:
        violations.append(f"{task_name}: ATTEMPT.json missing fields: {', '.join(missing)}")
    for field in ("attempt_id", "task_id", "agent", "agent_name"):
        if field in attempt and not is_non_empty_string(attempt.get(field)):
            violations.append(f"{task_name}: ATTEMPT.{field} must be a non-empty string")
    if "session_id" in attempt and not isinstance(attempt.get("session_id"), str):
        violations.append(f"{task_name}: ATTEMPT.session_id must be a string")
    if attempt.get("attempt_id") != attempt_id:
        violations.append(f"{task_name}: ATTEMPT.json attempt_id does not match current_attempt_id")
    if attempt.get("task_id") != status.get("task_id"):
        violations.append(f"{task_name}: ATTEMPT.json task_id does not match STATUS task_id")
    if attempt.get("state") not in ATTEMPT_STATES:
        violations.append(f"{task_name}: invalid ATTEMPT.state {attempt.get('state')!r}")
    if attempt.get("handoff_state") not in HANDOFF_STATES:
        violations.append(f"{task_name}: invalid ATTEMPT.handoff_state {attempt.get('handoff_state')!r}")
    if parse_iso(attempt.get("started_at")) is None:
        violations.append(f"{task_name}: invalid ATTEMPT.started_at {attempt.get('started_at')!r}")
    if attempt.get("ended_at") and parse_iso(attempt.get("ended_at")) is None:
        violations.append(f"{task_name}: invalid ATTEMPT.ended_at {attempt.get('ended_at')!r}")

    runtime_violations, _ = validate_runtime_backend(attempt.get("runtime"), task_name)
    violations.extend(runtime_violations)

    attempt_state = attempt.get("state")
    if attempt_state in {"completed", "invalid_handoff"}:
        if not attempt.get("ended_at"):
            violations.append(f"{task_name}: ATTEMPT.state {attempt_state} requires ended_at")
    if attempt_state == "completed":
        if not is_int_not_bool(attempt.get("exit_code")):
            violations.append(f"{task_name}: completed ATTEMPT requires integer exit_code")
    if attempt_state == "invalid_handoff":
        if attempt.get("exit_code") is not None and not is_int_not_bool(attempt.get("exit_code")):
            violations.append(f"{task_name}: invalid_handoff ATTEMPT requires exit_code integer or null")
    if attempt_state in {"created", "running"}:
        if attempt.get("ended_at") is not None:
            violations.append(f"{task_name}: ATTEMPT.state {attempt_state} requires ended_at=null")
        if attempt.get("exit_code") is not None:
            violations.append(f"{task_name}: ATTEMPT.state {attempt_state} requires exit_code=null")
    if attempt_state == "completed":
        if attempt.get("handoff_valid") is not True:
            violations.append(f"{task_name}: completed ATTEMPT requires handoff_valid=true")
        if attempt.get("handoff_state") not in {"review", "blocked"}:
            violations.append(f"{task_name}: completed ATTEMPT requires handoff_state review or blocked")
    if attempt_state == "invalid_handoff":
        if attempt.get("handoff_valid") is not False:
            violations.append(f"{task_name}: invalid_handoff ATTEMPT requires handoff_valid=false")

    return violations


def validate_event(event: dict[str, Any], run_id: str, line_no: int) -> tuple[list[str], list[str]]:
    """Validate one EVENTS.ndjson event object."""

    violations: list[str] = []
    warnings: list[str] = []

    missing = [field for field in ("at", "actor", "event", "run_id") if not event.get(field)]
    if missing:
        violations.append(f"EVENTS.ndjson line {line_no}: missing required fields: {', '.join(missing)}")
    if event.get("run_id") and event.get("run_id") != run_id:
        violations.append(f"EVENTS.ndjson line {line_no}: run_id {event.get('run_id')!r} does not match {run_id!r}")

    event_name = event.get("event")
    if event_name and event_name not in CORE_EVENTS:
        warnings.append(f"EVENTS.ndjson line {line_no}: unknown event type {event_name!r}")
    if event_name in TASK_EVENTS and not event.get("task_id"):
        violations.append(f"EVENTS.ndjson line {line_no}: event {event_name!r} requires task_id")
    if event_name in ATTEMPT_EVENTS and not event.get("attempt_id"):
        violations.append(f"EVENTS.ndjson line {line_no}: event {event_name!r} requires attempt_id")
    if event.get("at") and parse_iso(event.get("at")) is None:
        violations.append(f"EVENTS.ndjson line {line_no}: invalid at timestamp {event.get('at')!r}")

    return violations, warnings


def validate_worker_handoff(
    status: dict[str, Any],
    attempt_id: str,
    task_dir: Path,
    exit_code_raw: str,
) -> HandoffValidationResult:
    """Validate the worker's running -> review|blocked handoff.

    This is the shared rule set for dispatch-time handoff gating and
    collect_status review/blocked audit checks. It does not mutate protocol
    files.
    """

    reasons: list[str] = []
    exit_code, exit_code_error = parse_exit_code(exit_code_raw)
    if exit_code_error:
        reasons.append(exit_code_error)

    state = status.get("state")
    if state not in {"review", "blocked"}:
        reasons.append(f"STATUS.state must be review or blocked, got {state!r}")

    if status.get("current_attempt_id") != attempt_id:
        reasons.append(
            f"STATUS.current_attempt_id {status.get('current_attempt_id')!r} does not match attempt_id {attempt_id!r}"
        )

    if status.get("previous_state") != "running":
        reasons.append(f"STATUS.previous_state must be running, got {status.get('previous_state')!r}")

    history = status.get("state_history") if isinstance(status.get("state_history"), list) else []
    last_transition = history[-1] if history else {}
    valid_history = (
        isinstance(last_transition, dict)
        and last_transition.get("from") == "running"
        and last_transition.get("to") == state
        and last_transition.get("actor") == "claude-code"
        and parse_iso(last_transition.get("at")) is not None
    )
    if not valid_history:
        reasons.append("state_history must end with running -> review|blocked by actor claude-code with valid timestamp")

    handoff_ok = has_substantive_content(task_dir / "HANDOFF.md")
    evidence_ok = has_substantive_content(task_dir / "EVIDENCE.md")

    if state == "review":
        if exit_code != 0:
            reasons.append(f"review handoff requires exit_code 0, got {exit_code!r}")
        if not handoff_ok:
            reasons.append("review handoff requires substantive HANDOFF.md")
        if not evidence_ok:
            reasons.append("review handoff requires substantive EVIDENCE.md")
    elif state == "blocked":
        if not handoff_ok:
            reasons.append("blocked handoff requires substantive HANDOFF.md")
        blocker_type = status.get("blocker_type")
        if blocker_type not in BLOCKER_TYPES:
            reasons.append(f"blocked handoff requires blocker_type in {sorted(BLOCKER_TYPES)}, got {blocker_type!r}")
        if not status.get("blocking_reason"):
            reasons.append("blocked handoff requires non-empty blocking_reason")

    return HandoffValidationResult(
        valid=not reasons,
        handoff_state=state if state in {"review", "blocked"} else None,
        exit_code=exit_code,
        reasons=reasons,
    )
