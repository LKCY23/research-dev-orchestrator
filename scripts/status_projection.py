#!/usr/bin/env python3
"""Read-only task status projection with explicit artifact provenance."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from artifact_resolver import (
    ArtifactResolutionError,
    ResolvedTaskArtifacts,
    protocol_route,
    resolve_task_artifacts,
)


@dataclass(frozen=True)
class StatusProjectionResult:
    projection: dict[str, Any]
    artifacts: ResolvedTaskArtifacts | None
    error: str | None


def _load_attempt(task_dir: Path, attempt_id: Any) -> dict[str, Any] | None:
    if (
        not isinstance(attempt_id, str)
        or not attempt_id
        or attempt_id in {".", ".."}
        or Path(attempt_id).name != attempt_id
        or "/" in attempt_id
        or "\\" in attempt_id
    ):
        return None
    try:
        payload = json.loads(
            (task_dir / "attempts" / attempt_id / "ATTEMPT.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _attempt_projection(
    attempt_id: Any,
    attempt: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(attempt_id, str) or not attempt_id:
        return None
    payload = attempt if isinstance(attempt, Mapping) else {}
    return {
        "source": f"attempts/{attempt_id}/ATTEMPT.json",
        "attempt_id": attempt_id,
        "identity_valid": payload.get("attempt_id") == attempt_id,
        "state": payload.get("state"),
        "outcome": payload.get("outcome"),
        "phase": payload.get("phase"),
        "started_at": payload.get("started_at"),
        "ended_at": payload.get("ended_at"),
        "exit_code": payload.get("exit_code"),
    }


def _publication_projection(
    resolved: ResolvedTaskArtifacts,
    *,
    relation: str,
) -> dict[str, Any]:
    attempt_id = resolved.attempt_dir.name if resolved.attempt_dir is not None else None
    handoff = resolved.handoff_index if isinstance(resolved.handoff_index, dict) else {}
    published = resolved.publication_state == "published"
    evidence_ref = resolved.artifact_refs.get("evidence") or resolved.artifact_refs.get(
        "evidence_markdown"
    )
    return {
        "relation": relation,
        "attempt_id": attempt_id,
        "state": resolved.publication_state,
        "valid": published,
        "source": (
            "attempt_bundle" if resolved.protocol == "v2" else "legacy_task_root"
        ),
        "requested_state": handoff.get("requested_state") if published else None,
        "source_commit": handoff.get("source_commit") if published else None,
        "commit_check": resolved.commit_check.as_dict(),
        "summary": str(handoff.get("summary") or "") if published else "",
        "evidence": {
            "available": bool(published and evidence_ref),
            "attempt_id": attempt_id,
            "ref": evidence_ref if published else None,
            "command_record_count": (
                handoff.get("command_record_count") if published else None
            ),
            "changed_path_count": (
                handoff.get("changed_path_count") if published else None
            ),
        },
    }


def _invalid_publication(attempt_id: Any, error: str) -> dict[str, Any]:
    return {
        "relation": "current",
        "attempt_id": attempt_id if isinstance(attempt_id, str) else None,
        "state": "invalid",
        "valid": False,
        "source": "attempt_bundle",
        "requested_state": None,
        "source_commit": None,
        "commit_check": None,
        "summary": "",
        "evidence": {
            "available": False,
            "attempt_id": attempt_id if isinstance(attempt_id, str) else None,
            "ref": None,
            "command_record_count": None,
            "changed_path_count": None,
        },
        "error": error,
    }


def _attempt_order(path: Path) -> tuple[int, str]:
    match = re.match(r"^A(\d+)", path.name)
    return (int(match.group(1)) if match else -1, path.name)


def _previous_v2_publication(
    task_dir: Path,
    status: Mapping[str, Any],
    current_attempt_id: Any,
) -> dict[str, Any] | None:
    if not isinstance(current_attempt_id, str) or not current_attempt_id:
        return None
    attempts_dir = task_dir / "attempts"
    if not attempts_dir.is_dir():
        return None
    current_sequence = _attempt_order(Path(current_attempt_id))[0]
    if current_sequence < 0:
        return None
    candidates = sorted(
        (
            path
            for path in attempts_dir.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and path.name != current_attempt_id
            and (
                current_sequence < 0
                or _attempt_order(path)[0] < current_sequence
            )
            and (path / "runtime" / "HANDOFF_READY.json").is_file()
            and not (path / "runtime" / "HANDOFF_READY.json").is_symlink()
        ),
        key=_attempt_order,
        reverse=True,
    )
    for candidate in candidates:
        historical_attempt = _load_attempt(task_dir, candidate.name)
        if not (
            isinstance(historical_attempt, dict)
            and historical_attempt.get("state") == "completed"
            and historical_attempt.get("handoff_valid") is True
        ):
            continue
        historical_status = dict(status)
        historical_status["state"] = "running"
        historical_status["current_attempt_id"] = candidate.name
        try:
            resolved = resolve_task_artifacts(
                task_dir,
                historical_status,
                require_publication=False,
                verify_commit=False,
            )
        except ArtifactResolutionError:
            continue
        if (
            resolved.publication_state == "published"
            and isinstance(resolved.handoff_index, dict)
            and historical_attempt.get("handoff_state")
            == resolved.handoff_index.get("requested_state")
        ):
            return _publication_projection(resolved, relation="previous")
    return None


def resolve_status_projection(
    task_dir: Path,
    status: Mapping[str, Any],
    *,
    attempt: Mapping[str, Any] | None = None,
) -> StatusProjectionResult:
    """Resolve one task's observable status without mutating protocol state."""

    task_dir = task_dir.resolve()
    attempt_id = status.get("current_attempt_id")
    if attempt is None:
        attempt = _load_attempt(task_dir, attempt_id)

    artifacts: ResolvedTaskArtifacts | None = None
    error: str | None = None
    try:
        protocol, version = protocol_route(status)
    except ArtifactResolutionError as exc:
        protocol = "unknown"
        raw_version = status.get("artifact_protocol_version")
        version = raw_version if isinstance(raw_version, int) else None
        error = str(exc)
    else:
        try:
            artifacts = resolve_task_artifacts(task_dir, status)
        except ArtifactResolutionError as exc:
            error = str(exc)

    if artifacts is not None:
        publication = _publication_projection(artifacts, relation="current")
    else:
        publication = _invalid_publication(attempt_id, error or "artifact resolution failed")

    previous_publication = None
    if protocol == "v2" and publication["state"] != "published":
        previous_publication = _previous_v2_publication(
            task_dir,
            status,
            attempt_id,
        )

    if protocol == "v2":
        if publication["state"] == "published":
            summary_publication = publication
        else:
            summary_publication = previous_publication
        summary = (
            str(summary_publication.get("summary") or "")
            if isinstance(summary_publication, dict)
            else ""
        )
        summary_relation = (
            summary_publication.get("relation")
            if isinstance(summary_publication, dict) and summary
            else "none"
        )
        summary_attempt_id = (
            summary_publication.get("attempt_id")
            if isinstance(summary_publication, dict) and summary
            else None
        )
        compatibility = {
            "status_summary_authoritative": False,
            "status_evidence_authoritative": False,
            "ignored_status_fields": ["summary", "evidence"],
        }
    else:
        handoff_summary = publication.get("summary") or ""
        summary = str(status.get("summary") or handoff_summary or "")
        summary_relation = "legacy_status_or_handoff" if summary else "none"
        summary_attempt_id = None
        compatibility = {
            "status_summary_authoritative": True,
            "status_evidence_authoritative": True,
            "ignored_status_fields": [],
        }

    projection = {
        "schema_version": 1,
        "protocol": protocol,
        "artifact_protocol_version": version,
        "task": {
            "source": "STATUS.json",
            "task_id": status.get("task_id", task_dir.name),
            "profile": status.get("profile"),
            "state": status.get("state"),
            "owner": status.get("owner"),
            "current_attempt_id": attempt_id,
            "needs_coordinator": status.get("needs_coordinator"),
            "blocker_type": status.get("blocker_type"),
            "blocking_reason": status.get("blocking_reason"),
        },
        "attempt": _attempt_projection(attempt_id, attempt),
        "publication": publication,
        "previous_publication": previous_publication,
        "display": {
            "summary": summary,
            "summary_relation": summary_relation,
            "summary_attempt_id": summary_attempt_id,
        },
        "compatibility": compatibility,
    }
    return StatusProjectionResult(projection, artifacts, error)
