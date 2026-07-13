#!/usr/bin/env python3
"""Shared protocol validation rules."""

from __future__ import annotations

import json
import hashlib
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
    load_json,
    parse_iso,
)


@dataclass(frozen=True)
class HandoffValidationResult:
    valid: bool
    handoff_state: str | None
    exit_code: int | None
    reasons: list[str]
    request: dict[str, Any] | None = None


def parse_exit_code(exit_code_raw: str) -> tuple[int | None, str | None]:
    try:
        return int(exit_code_raw), None
    except (TypeError, ValueError):
        return None, f"exit_code must be an integer, got {exit_code_raw!r}"


def load_handoff_request(task_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Load and validate the worker's HANDOFF.json transition request."""

    path = task_dir / "HANDOFF.json"
    if not path.exists():
        return None, ["HANDOFF.json is required for terminal worker handoff"]
    try:
        request = load_json(path)
    except Exception as exc:
        return None, [f"HANDOFF.json is not valid JSON: {exc}"]
    if not isinstance(request, dict):
        return None, ["HANDOFF.json must be a JSON object"]
    if request.get("_template") is True:
        return None, ["HANDOFF.json still has _template=true"]

    reasons: list[str] = []
    requested_state = request.get("requested_state")
    if requested_state not in {"strategy_review", "review", "blocked"}:
        reasons.append(f"HANDOFF.json requested_state must be strategy_review, review, or blocked, got {requested_state!r}")
    for field in ("commands_run", "files_changed", "known_limitations"):
        if field in request and not isinstance(request.get(field), list):
            reasons.append(f"HANDOFF.json {field} must be a list")
    if "needs_coordinator" in request and not isinstance(request.get("needs_coordinator"), bool):
        reasons.append("HANDOFF.json needs_coordinator must be boolean")
    return request, reasons


def validate_status_schema(status: Any, fsm: dict[str, Any], task_name: str) -> list[str]:
    """Validate STATUS.json fields that do not require filesystem access."""

    violations: list[str] = []
    if not isinstance(status, dict):
        return [f"{task_name}: STATUS.json must be a JSON object"]

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
    if state in {"planning", "strategy_review", "running", "blocked", "review", "approved", "merged"} and not attempt_id:
        violations.append(f"{task_name}: {state} task is missing current_attempt_id")

    return violations


def validate_state_history(status: Any, fsm: dict[str, Any], task_name: str) -> list[str]:
    """Validate FSM state_history legality and continuity."""

    violations: list[str] = []
    if not isinstance(status, dict):
        return []

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
        "phase",
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
    if attempt.get("phase") not in {"planning", "execution"}:
        violations.append(f"{task_name}: ATTEMPT.phase must be planning or execution")

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
        if attempt.get("handoff_state") not in {"strategy_review", "review", "blocked"}:
            violations.append(f"{task_name}: completed ATTEMPT requires a terminal handoff_state")
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
    if not isinstance(event_name, str) or not event_name.strip():
        violations.append(f"EVENTS.ndjson line {line_no}: event must be a non-empty string")
        event_name = None
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
    status: Any,
    attempt_id: str,
    task_dir: Path,
    exit_code_raw: str,
) -> HandoffValidationResult:
    """Validate the worker's requested running -> review|blocked handoff.

    This is the shared rule set for dispatch-time handoff gating and
    terminal audit checks. It does not mutate protocol files. Workers request
    terminal state through HANDOFF.json; dispatch applies STATUS transitions.
    """

    reasons: list[str] = []
    attempt_path = task_dir / "attempts" / attempt_id / "ATTEMPT.json"
    try:
        attempt = load_json(attempt_path)
    except Exception as exc:
        attempt = None
        reasons.append(f"ATTEMPT.json is unreadable during governance validation: {exc}")
    profile = None
    if isinstance(attempt, dict) and attempt.get("backend_profile_sha256"):
        profile_path = attempt_path.parent / "runtime" / "BACKEND_PROFILE.json"
        try:
            profile = load_json(profile_path)
            unsigned = dict(profile)
            actual = unsigned.pop("profile_sha256", None)
            from strategy import canonical_digest
            recomputed = canonical_digest(unsigned)
        except Exception as exc:
            reasons.append(f"backend profile is unreadable during handoff: {exc}")
        else:
            if actual != recomputed or actual != attempt.get("backend_profile_sha256"):
                reasons.append("backend profile digest changed during the attempt")
    if isinstance(attempt, dict) and attempt.get("backend_settings_sha256"):
        generated = profile.get("generated_files", []) if isinstance(profile, dict) else []
        settings_files = [item for item in generated if isinstance(item, str) and item]
        settings_path = attempt_path.parent / "runtime" / (
            settings_files[0] if len(settings_files) == 1 else "claude-settings.json"
        )
        try:
            settings_digest = hashlib.sha256(settings_path.read_bytes()).hexdigest()
        except OSError as exc:
            reasons.append(f"backend settings are unreadable during handoff: {exc}")
        else:
            if settings_digest != attempt.get("backend_settings_sha256"):
                reasons.append("backend settings changed during the attempt")
    violations_path = task_dir / "attempts" / attempt_id / "runtime" / "VIOLATIONS.ndjson"
    if violations_path.exists():
        try:
            violations = [json.loads(line) for line in violations_path.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, json.JSONDecodeError) as exc:
            reasons.append(f"backend governance violations are unreadable: {exc}")
        else:
            hard = [item for item in violations if isinstance(item, dict) and item.get("hard") is True]
            if hard:
                reasons.append(f"attempt has {len(hard)} hard backend governance violation(s)")
    exit_code, exit_code_error = parse_exit_code(exit_code_raw)
    if exit_code_error:
        reasons.append(exit_code_error)
    request, request_reasons = load_handoff_request(task_dir)
    reasons.extend(request_reasons)
    requested_state = request.get("requested_state") if isinstance(request, dict) else None

    if not isinstance(status, dict):
        return HandoffValidationResult(
            valid=False,
            handoff_state=None,
            exit_code=exit_code,
            reasons=[*reasons, "STATUS.json must be a JSON object"],
            request=request,
        )

    state = status.get("state")
    if requested_state == "review":
        allowed_active_states = {"running"}
    else:
        allowed_active_states = {"planning", "running"}
    expected_state = state if state in allowed_active_states else "running"
    if state not in allowed_active_states:
        allowed = ", ".join(sorted(allowed_active_states))
        reasons.append(f"STATUS.state must remain one of [{allowed}] until dispatch applies handoff, got {state!r}")

    if status.get("current_attempt_id") != attempt_id:
        reasons.append(
            f"STATUS.current_attempt_id {status.get('current_attempt_id')!r} does not match attempt_id {attempt_id!r}"
        )

    history = status.get("state_history") if isinstance(status.get("state_history"), list) else []
    last_transition = history[-1] if history else {}
    valid_history = (
        isinstance(last_transition, dict)
        and last_transition.get("to") == expected_state
        and last_transition.get("actor") == "dispatch"
        and parse_iso(last_transition.get("at")) is not None
    )
    if not valid_history:
        reasons.append(f"state_history must show dispatch moved the task into {expected_state} before worker handoff")

    handoff_ok = has_substantive_content(task_dir / "HANDOFF.md")
    evidence_ok = has_substantive_content(task_dir / "EVIDENCE.md")

    if requested_state == "strategy_review":
        if exit_code != 0:
            reasons.append(f"strategy review handoff requires exit_code 0, got {exit_code!r}")
        if not handoff_ok or not evidence_ok:
            reasons.append("strategy review handoff requires substantive HANDOFF.md and EVIDENCE.md")
        revision = request.get("strategy_revision") if isinstance(request, dict) else None
        digest = request.get("strategy_sha256") if isinstance(request, dict) else None
        if not is_int_not_bool(revision) or revision <= 0 or not is_non_empty_string(digest):
            reasons.append("strategy review handoff requires strategy_revision and strategy_sha256")
        else:
            try:
                from strategy import canonical_digest, strategy_path

                submitted = load_json(strategy_path(task_dir, revision))
                if canonical_digest(submitted) != digest:
                    reasons.append("strategy review handoff digest does not match submitted strategy")
            except Exception as exc:
                reasons.append(f"strategy review handoff cannot load submitted strategy: {exc}")
    elif requested_state == "review":
        if exit_code != 0:
            reasons.append(f"review handoff requires exit_code 0, got {exit_code!r}")
        if not handoff_ok:
            reasons.append("review handoff requires substantive HANDOFF.md")
        if not evidence_ok:
            reasons.append("review handoff requires substantive EVIDENCE.md")
        try:
            attempt = load_json(task_dir / "attempts" / attempt_id / "ATTEMPT.json")
            if attempt.get("phase") != "execution":
                reasons.append("review handoff requires an execution attempt")
            from strategy import load_approved_strategy

            approved, _ = load_approved_strategy(task_dir)
            attempt_runtime = task_dir / "attempts" / attempt_id / "runtime"
            records_path = attempt_runtime / "WORKFLOWS.ndjson"
            records = []
            if records_path.exists():
                records = [json.loads(line) for line in records_path.read_text().splitlines() if line.strip()]
            if approved["completion_gate"]["required_workflows_complete"]:
                completed = {
                    record.get("workflow_id")
                    for record in records
                    if record.get("event") == "workflow_completed"
                }
                missing = sorted(
                    workflow["workflow_id"]
                    for workflow in approved["workflows"]
                    if workflow["required"] and workflow["workflow_id"] not in completed
                )
                if missing:
                    reasons.append(f"required workflows are incomplete: {missing}")
            commands_path = attempt_runtime / "COMMANDS.ndjson"
            commands = []
            if commands_path.exists():
                commands = [json.loads(line) for line in commands_path.read_text().splitlines() if line.strip()]
            acceptance_commands = [item for item in commands if item.get("acceptance") is True]
            if approved["completion_gate"]["acceptance_commands_pass"] and (
                not acceptance_commands
                or any(item.get("exit_code") != 0 or item.get("timed_out") for item in acceptance_commands)
            ):
                reasons.append("acceptance command completion gate failed")
            if not approved["completion_gate"]["optional_workflows_may_timeout"] and any(
                record.get("event") == "workflow_timed_out" for record in records
            ):
                reasons.append("workflow timeout is forbidden by the completion gate")
        except Exception as exc:
            reasons.append(f"review handoff cannot validate approved strategy completion: {exc}")
    elif requested_state == "blocked":
        if not handoff_ok:
            reasons.append("blocked handoff requires substantive HANDOFF.md")
        blocker_type = request.get("blocker_type") if isinstance(request, dict) else None
        if blocker_type not in BLOCKER_TYPES:
            reasons.append(f"blocked handoff requires blocker_type in {sorted(BLOCKER_TYPES)}, got {blocker_type!r}")
        blocking_reason = request.get("blocking_reason") if isinstance(request, dict) else None
        if not blocking_reason:
            reasons.append("blocked handoff requires non-empty blocking_reason")

    return HandoffValidationResult(
        valid=not reasons,
        handoff_state=requested_state if requested_state in {"strategy_review", "review", "blocked"} else None,
        exit_code=exit_code,
        reasons=reasons,
        request=request,
    )
