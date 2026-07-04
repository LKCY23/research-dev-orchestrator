#!/usr/bin/env python3
"""Shared protocol validation rules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protocol import BLOCKER_TYPES, has_substantive_content, parse_iso


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
