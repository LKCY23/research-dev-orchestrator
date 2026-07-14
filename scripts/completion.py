#!/usr/bin/env python3
"""Attempt-bound completion signals for interactive workers."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protocol import load_json, parse_iso, utc_now


@dataclass(frozen=True)
class CompletionValidationResult:
    valid: bool
    reasons: tuple[str, ...]
    payload: dict[str, Any] | None = None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def completion_path(task_dir: Path, attempt_id: str) -> Path:
    return task_dir / "attempts" / attempt_id / "COMPLETION.json"


def write_completion(
    task_dir: Path,
    *,
    attempt_id: str,
    phase: str,
    requested_state: str,
    strategy_sha256: str | None = None,
) -> Path:
    """Commit completion last, after all handoff artifacts are durable."""

    handoff_path = task_dir / "HANDOFF.json"
    payload = {
        "schema_version": 1,
        "task_id": load_json(task_dir / "STATUS.json").get("task_id"),
        "attempt_id": attempt_id,
        "phase": phase,
        "requested_state": requested_state,
        "handoff_sha256": file_sha256(handoff_path),
        "strategy_sha256": strategy_sha256 or None,
        "completed_at": utc_now(),
    }
    path = completion_path(task_dir, attempt_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def validate_completion(path: Path, *, task_dir: Path, attempt_id: str) -> CompletionValidationResult:
    reasons: list[str] = []
    try:
        payload = load_json(path)
    except Exception as exc:
        return CompletionValidationResult(False, (f"completion artifact is unreadable: {exc}",))
    if not isinstance(payload, dict):
        return CompletionValidationResult(False, ("completion artifact must be a JSON object",))

    try:
        status = load_json(task_dir / "STATUS.json")
        attempt = load_json(task_dir / "attempts" / attempt_id / "ATTEMPT.json")
        handoff = load_json(task_dir / "HANDOFF.json")
    except Exception as exc:
        return CompletionValidationResult(False, (f"completion dependencies are unreadable: {exc}",), payload)

    if payload.get("schema_version") != 1:
        reasons.append("completion schema_version must be 1")
    if payload.get("task_id") != status.get("task_id"):
        reasons.append("completion task_id does not match STATUS.json")
    if payload.get("attempt_id") != attempt_id:
        reasons.append("completion attempt_id does not match the supervised attempt")
    if status.get("current_attempt_id") != attempt_id:
        reasons.append("completion attempt is not the current task attempt")
    if attempt.get("attempt_id") != attempt_id or attempt.get("state") not in {"created", "running"}:
        reasons.append("completion does not reference an active ATTEMPT.json")

    phase = payload.get("phase")
    if phase != attempt.get("phase") or phase not in {"planning", "execution"}:
        reasons.append("completion phase does not match ATTEMPT.json")
    requested_state = payload.get("requested_state")
    allowed_states = (
        {"strategy_review", "blocked"}
        if phase == "planning"
        else {"strategy_review", "verified", "review", "blocked"}
    )
    if requested_state not in allowed_states:
        reasons.append(f"completion requested_state is invalid for {phase!r} phase")
    if handoff.get("requested_state") != requested_state:
        reasons.append("completion requested_state does not match HANDOFF.json")

    handoff_path = task_dir / "HANDOFF.json"
    try:
        current_handoff_digest = file_sha256(handoff_path)
    except OSError as exc:
        reasons.append(f"HANDOFF.json cannot be hashed: {exc}")
    else:
        if payload.get("handoff_sha256") != current_handoff_digest:
            reasons.append("completion handoff_sha256 does not match HANDOFF.json")

    if requested_state == "strategy_review":
        strategy_digest = payload.get("strategy_sha256")
        if not isinstance(strategy_digest, str) or not strategy_digest:
            reasons.append("strategy completion requires strategy_sha256")
        elif strategy_digest != handoff.get("strategy_sha256"):
            reasons.append("completion strategy_sha256 does not match HANDOFF.json")
    if parse_iso(payload.get("completed_at")) is None:
        reasons.append("completion completed_at must be an ISO timestamp")

    return CompletionValidationResult(not reasons, tuple(reasons), payload)
