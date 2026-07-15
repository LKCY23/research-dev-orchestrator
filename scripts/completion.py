#!/usr/bin/env python3
"""Attempt-bound publication signals for worker supervisors.

Artifact Protocol v1 uses ``COMPLETION.json``.  Protocol v2 deliberately does
not: its only supervisor signal is the validated, attempt-local
``runtime/HANDOFF_READY.json`` publication marker.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protocol import ARTIFACT_PROTOCOL_VERSION, load_json, parse_iso, utc_now


LEGACY_ARTIFACT_PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class CompletionValidationResult:
    valid: bool
    reasons: tuple[str, ...]
    payload: dict[str, Any] | None = None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def completion_path(task_dir: Path, attempt_id: str) -> Path:
    """Return the legacy-v1 completion path."""

    return task_dir / "attempts" / attempt_id / "COMPLETION.json"


def handoff_ready_path(task_dir: Path, attempt_id: str) -> Path:
    """Return the v2 publication-marker path for exactly one attempt."""

    return task_dir / "attempts" / attempt_id / "runtime" / "HANDOFF_READY.json"


def publication_path(task_dir: Path, attempt_id: str, artifact_protocol_version: int) -> Path:
    """Resolve the signal path without inferring a protocol from file presence."""

    if artifact_protocol_version == LEGACY_ARTIFACT_PROTOCOL_VERSION:
        return completion_path(task_dir, attempt_id)
    if artifact_protocol_version == ARTIFACT_PROTOCOL_VERSION:
        return handoff_ready_path(task_dir, attempt_id)
    raise ValueError(f"unsupported artifact protocol version: {artifact_protocol_version!r}")


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def validate_publication(
    path: Path,
    *,
    artifact_protocol_version: int,
    task_dir: Path,
    attempt_id: str,
) -> CompletionValidationResult:
    """Validate one protocol-explicit supervisor publication signal.

    V2 validates the complete HANDOFF/EVIDENCE/TASK_INPUTS digest closure via
    :func:`artifact_bundle.load_bundle`.  Merely creating a file named
    ``HANDOFF_READY.json`` can therefore never stop a worker.
    """

    try:
        expected_path = publication_path(task_dir, attempt_id, artifact_protocol_version)
    except ValueError as exc:
        return CompletionValidationResult(False, (str(exc),))
    if not _same_path(path, expected_path):
        return CompletionValidationResult(
            False,
            (
                "publication path does not belong to the supervised attempt: "
                f"{path} != {expected_path}",
            ),
        )

    if artifact_protocol_version == LEGACY_ARTIFACT_PROTOCOL_VERSION:
        try:
            status = load_json(task_dir / "STATUS.json")
        except Exception as exc:
            return CompletionValidationResult(False, (f"task status is unreadable: {exc}",))
        declared = status.get("artifact_protocol_version") if isinstance(status, dict) else None
        if declared is not None and (
            not isinstance(declared, int)
            or isinstance(declared, bool)
            or declared != LEGACY_ARTIFACT_PROTOCOL_VERSION
        ):
            return CompletionValidationResult(
                False,
                ("legacy COMPLETION.json cannot finish an artifact-protocol-v2 task",),
            )
        return validate_completion(path, task_dir=task_dir, attempt_id=attempt_id)

    try:
        status = load_json(task_dir / "STATUS.json")
    except Exception as exc:
        return CompletionValidationResult(False, (f"task status is unreadable: {exc}",))
    if not isinstance(status, dict):
        return CompletionValidationResult(False, ("STATUS.json must be a JSON object",))
    if status.get("artifact_protocol_version") != ARTIFACT_PROTOCOL_VERSION:
        return CompletionValidationResult(
            False,
            ("v2 handoff publication requires STATUS.json artifact_protocol_version 2",),
        )
    if status.get("current_attempt_id") != attempt_id:
        return CompletionValidationResult(
            False,
            ("handoff publication attempt is not the current task attempt",),
        )
    task_id = status.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return CompletionValidationResult(False, ("STATUS.json task_id must be non-empty",))

    attempt_dir = task_dir / "attempts" / attempt_id
    try:
        from artifact_bundle import ArtifactBundleError, load_bundle

        bundle = load_bundle(
            attempt_dir,
            expected_task_id=task_id,
            expected_attempt_id=attempt_id,
        )
    except (ArtifactBundleError, OSError, ValueError) as exc:
        return CompletionValidationResult(
            False,
            (f"v2 handoff publication is invalid: {exc}",),
        )
    if bundle.task_inputs_binding.attempt.get("state") not in {"created", "running"}:
        return CompletionValidationResult(
            False,
            ("handoff publication does not reference an active ATTEMPT.json",),
            bundle.ready,
        )
    return CompletionValidationResult(True, (), bundle.ready)


def write_completion(
    task_dir: Path,
    *,
    attempt_id: str,
    phase: str,
    requested_state: str,
    strategy_sha256: str | None = None,
    source_commit: str | None = None,
) -> Path:
    """Write the legacy-v1 completion signal after root handoff artifacts."""

    handoff_path = task_dir / "HANDOFF.json"
    payload = {
        "schema_version": 1,
        "task_id": load_json(task_dir / "STATUS.json").get("task_id"),
        "attempt_id": attempt_id,
        "phase": phase,
        "requested_state": requested_state,
        "handoff_sha256": file_sha256(handoff_path),
        "strategy_sha256": strategy_sha256 or None,
        "source_commit": source_commit or None,
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
    if requested_state == "verified":
        source_commit = payload.get("source_commit")
        if not isinstance(source_commit, str) or not source_commit:
            reasons.append("verified completion requires source_commit")
        elif source_commit != handoff.get("source_commit"):
            reasons.append("completion source_commit does not match HANDOFF.json")
    if parse_iso(payload.get("completed_at")) is None:
        reasons.append("completion completed_at must be an ISO timestamp")

    return CompletionValidationResult(not reasons, tuple(reasons), payload)
