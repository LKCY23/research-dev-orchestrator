#!/usr/bin/env python3
"""Explicit read-only routing for v2 and recognized legacy task artifacts.

Artifact Protocol v2 is resolved only from the current attempt and only through
the validated HANDOFF_READY digest closure.  The legacy decoder is deliberately
separate: an absent historical version is normalized to ``legacy-v0.5`` for
audit compatibility, but legacy files can never satisfy a v2 lookup.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from artifact_bundle import (
    ARTIFACT_PROTOCOL_VERSION,
    EVIDENCE_REF,
    HANDOFF_REF,
    READY_REF,
    TASK_INPUTS_REF,
    ArtifactBundle,
    ArtifactBundleError,
    artifact_binding,
    load_bundle,
    validate_artifact_binding,
    validate_task_inputs_binding,
)


_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_PUBLICATION_REQUIRED_STATES = {
    "strategy_review",
    "verified",
    "review",
    "blocked",
    "changes_requested",
    "approved",
    "merged",
}


class ArtifactResolutionError(ValueError):
    """The status-selected artifact protocol cannot be resolved safely."""


class UnsupportedArtifactProtocolError(ArtifactResolutionError):
    """STATUS.json declares an artifact protocol this installation cannot read."""


@dataclass(frozen=True)
class CommitCheck:
    declared: str | None
    resolved: str | None
    worktree_head: str | None
    valid: bool | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "declared": self.declared,
            "resolved": self.resolved,
            "worktree_head": self.worktree_head,
            "valid": self.valid,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ResolvedTaskArtifacts:
    protocol: str
    artifact_protocol_version: int
    task_dir: Path
    attempt_dir: Path | None
    publication_state: str
    bundle: ArtifactBundle | None
    handoff_index: dict[str, Any] | None
    artifact_refs: dict[str, str]
    commit_check: CommitCheck
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "artifact_protocol_version": self.artifact_protocol_version,
            "attempt_dir": str(self.attempt_dir) if self.attempt_dir is not None else None,
            "publication_state": self.publication_state,
            "artifact_refs": dict(self.artifact_refs),
            "commit_check": self.commit_check.as_dict(),
            "warnings": list(self.warnings),
        }


def protocol_route(status: Mapping[str, Any]) -> tuple[str, int]:
    """Return the explicit decoder route selected by STATUS.json."""

    raw = status.get("artifact_protocol_version")
    if isinstance(raw, bool):
        raise UnsupportedArtifactProtocolError(
            f"unsupported STATUS.artifact_protocol_version: {raw!r}"
        )
    if isinstance(raw, int) and raw == ARTIFACT_PROTOCOL_VERSION:
        return "v2", ARTIFACT_PROTOCOL_VERSION
    if isinstance(raw, int) and raw == 1:
        return "legacy-v1", 1
    if raw is None or (
        isinstance(raw, (float, str)) and raw in {0.5, "0.5", "v0.5"}
    ):
        # RDO v0.5 predated the field.  Absence is the explicit historical
        # discriminator; it is never interpreted as v2.
        return "legacy-v0.5", 1
    raise UnsupportedArtifactProtocolError(
        f"unsupported STATUS.artifact_protocol_version: {raw!r}"
    )


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ArtifactResolutionError(f"{label} is missing: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactResolutionError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArtifactResolutionError(f"{label} must be a JSON object")
    return payload


def _legacy_handoff_index(
    task_dir: Path,
    *,
    required: bool,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    path = task_dir / "HANDOFF.json"
    if not path.exists():
        if required:
            raise ArtifactResolutionError("legacy task has no task-root HANDOFF.json")
        return None, ()
    try:
        payload = _load_json_object(path, "legacy HANDOFF.json")
    except ArtifactResolutionError:
        if required:
            raise
        return None, ("legacy HANDOFF.json is unreadable",)
    if payload.get("_template") is True:
        if required:
            raise ArtifactResolutionError("legacy task-root HANDOFF.json is still a template")
        return {"template": True, "protocol": "legacy"}, ()
    warnings: list[str] = []
    requested_state = payload.get("requested_state")
    if requested_state not in {None, "", "strategy_review", "verified", "review", "blocked"}:
        warnings.append(
            "legacy HANDOFF.json requested_state should be strategy_review, verified, review, or blocked"
        )
    for field in ("commands_run", "files_changed", "known_limitations"):
        if field in payload and not isinstance(payload.get(field), list):
            warnings.append(f"legacy HANDOFF.json {field} must be an array")
    if "needs_coordinator" in payload and not isinstance(payload.get("needs_coordinator"), bool):
        warnings.append("legacy HANDOFF.json needs_coordinator must be boolean")
    blocker = payload.get("blocking_reason", "")
    return {
        "template": False,
        "protocol": "legacy",
        "requested_state": requested_state,
        "summary": payload.get("summary", ""),
        "commands_run": payload.get("commands_run", []),
        "files_changed": payload.get("files_changed", []),
        "known_limitations": payload.get("known_limitations", []),
        "needs_coordinator": payload.get("needs_coordinator", False),
        "blocker_type": payload.get("blocker_type", ""),
        "blocking_reason": blocker,
        "source_commit": payload.get("source_commit"),
    }, tuple(warnings)


def _v2_handoff_index(bundle: ArtifactBundle) -> dict[str, Any]:
    handoff = bundle.handoff
    blocker = handoff.get("conditional_blocker")
    if not isinstance(blocker, dict):
        blocker = {}
    return {
        "template": False,
        "protocol": "v2",
        "requested_state": handoff.get("requested_state"),
        "summary": handoff.get("summary", ""),
        "known_limitations": handoff.get("known_limitations", []),
        "needs_coordinator": handoff.get("requested_state") == "blocked",
        "blocker_type": blocker.get("blocker_type", ""),
        "blocking_reason": blocker.get("reason", ""),
        "source_commit": handoff.get("source_commit"),
        "direct_self_review": handoff.get("direct_self_review"),
        "command_record_count": len(bundle.evidence.get("command_records", [])),
        "changed_path_count": len(bundle.evidence.get("changed_paths", [])),
    }


def _git_output(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _repo_root(task_dir: Path) -> Path | None:
    root = _git_output(task_dir, "rev-parse", "--show-toplevel")
    return Path(root).resolve() if root else None


def _resolve_worktree(root: Path, raw: Any) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def verify_source_commit(
    task_dir: Path,
    status: Mapping[str, Any],
    source_commit: str | None,
) -> CommitCheck:
    """Recompute commit-object and live source-worktree validity."""

    if source_commit is None:
        return CommitCheck(None, None, None, None, ())
    reasons: list[str] = []
    if not isinstance(source_commit, str) or not _FULL_COMMIT.fullmatch(source_commit):
        return CommitCheck(
            source_commit if isinstance(source_commit, str) else None,
            None,
            None,
            False,
            ("source_commit must be an exact 40- or 64-character lowercase Git object ID",),
        )
    root = _repo_root(task_dir)
    if root is None:
        return CommitCheck(source_commit, None, None, False, ("task is not inside a Git repository",))
    resolved = _git_output(root, "rev-parse", "--verify", f"{source_commit}^{{commit}}")
    if resolved != source_commit:
        reasons.append("source_commit does not resolve to the exact declared commit object")

    worktree_head: str | None = None
    worktree = _resolve_worktree(root, status.get("worktree"))
    if worktree is not None and worktree.exists():
        worktree_head = _git_output(worktree, "rev-parse", "HEAD")
        if worktree_head is None:
            reasons.append("configured task worktree is not a readable Git worktree")
        elif worktree_head != source_commit:
            reasons.append("configured task worktree HEAD differs from frozen source_commit")
    elif status.get("state") != "merged":
        reasons.append("configured task worktree is missing before merge")
    return CommitCheck(source_commit, resolved, worktree_head, not reasons, tuple(reasons))


def _publication_required(status: Mapping[str, Any]) -> bool:
    return status.get("state") in _PUBLICATION_REQUIRED_STATES


def _expected_handoff_state(
    status: Mapping[str, Any],
    attempt: Mapping[str, Any] | None = None,
) -> str | None:
    state = status.get("state")
    if state in {"strategy_review", "verified", "review", "blocked"}:
        return str(state)
    if state == "changes_requested" and isinstance(attempt, Mapping) and (
        attempt.get("phase") == "planning"
        or attempt.get("handoff_state") == "strategy_review"
    ):
        return "strategy_review"
    if state in {"changes_requested", "approved", "merged"}:
        return "verified" if status.get("profile", "full") == "direct" else "review"
    return None


def resolve_task_artifacts(
    task_dir: Path,
    status: Mapping[str, Any],
    *,
    require_publication: bool | None = None,
    verify_commit: bool = True,
) -> ResolvedTaskArtifacts:
    """Resolve artifacts using only the decoder explicitly selected by status."""

    task_dir = task_dir.resolve()
    protocol, version = protocol_route(status)
    monitoring_default = require_publication is None
    required = _publication_required(status) if monitoring_default else require_publication
    task_id_raw = status.get("task_id")
    task_id = task_id_raw if isinstance(task_id_raw, str) and task_id_raw else task_dir.name
    attempt_id = status.get("current_attempt_id")

    if protocol != "v2":
        handoff, legacy_warnings = _legacy_handoff_index(task_dir, required=required)
        refs: dict[str, str] = {}
        for key, filename in (
            ("handoff_json", "HANDOFF.json"),
            ("handoff_markdown", "HANDOFF.md"),
            ("evidence_markdown", "EVIDENCE.md"),
        ):
            path = task_dir / filename
            if path.exists():
                refs[key] = str(path)
        source_commit = handoff.get("source_commit") if isinstance(handoff, dict) else None
        commit = verify_source_commit(task_dir, status, source_commit) if verify_commit else CommitCheck(
            source_commit, None, None, None, ()
        )
        return ResolvedTaskArtifacts(
            protocol=protocol,
            artifact_protocol_version=version,
            task_dir=task_dir,
            attempt_dir=None,
            publication_state="published" if handoff and not handoff.get("template") else "unpublished",
            bundle=None,
            handoff_index=handoff,
            artifact_refs=refs,
            commit_check=commit,
            warnings=legacy_warnings,
        )

    if attempt_id is None:
        if required:
            raise ArtifactResolutionError("v2 task requires a current attempt publication")
        return ResolvedTaskArtifacts(
            protocol=protocol,
            artifact_protocol_version=version,
            task_dir=task_dir,
            attempt_dir=None,
            publication_state="unpublished",
            bundle=None,
            handoff_index=None,
            artifact_refs={},
            commit_check=CommitCheck(None, None, None, None, ()),
        )
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ArtifactResolutionError("v2 STATUS.current_attempt_id must be a non-empty string or null")
    if attempt_id in {".", ".."} or Path(attempt_id).name != attempt_id or "/" in attempt_id or "\\" in attempt_id:
        raise ArtifactResolutionError("v2 STATUS.current_attempt_id must be one safe path component")
    attempt_dir = task_dir / "attempts" / attempt_id
    if not attempt_dir.is_dir():
        raise ArtifactResolutionError(f"v2 current attempt directory is missing: {attempt_id}")

    attempt_payload = _load_json_object(attempt_dir / "ATTEMPT.json", "v2 ATTEMPT.json")

    # A dispatcher-rejected handoff deliberately has no publication. Treat the
    # coordinator-visible blocked recovery state as auditable/unpublished when
    # callers use the monitoring default; strict consumers still pass
    # require_publication=True and cannot bypass a bundle gate.
    if (
        monitoring_default
        and status.get("state") == "blocked"
        and attempt_payload.get("state") == "invalid_handoff"
    ):
        try:
            validate_task_inputs_binding(
                attempt_dir,
                expected_task_id=task_id,
                expected_attempt_id=attempt_id,
            )
        except ArtifactBundleError as exc:
            raise ArtifactResolutionError(f"v2 task input binding is invalid: {exc}") from exc
        refs = {
            "task_inputs": str(attempt_dir / TASK_INPUTS_REF),
            "attempt": str(attempt_dir / "ATTEMPT.json"),
        }
        for key, ref in (
            ("evidence", EVIDENCE_REF),
            ("handoff", HANDOFF_REF),
            ("handoff_ready", READY_REF),
        ):
            if (attempt_dir / ref).exists():
                refs[key] = str(attempt_dir / ref)
        return ResolvedTaskArtifacts(
            protocol=protocol,
            artifact_protocol_version=version,
            task_dir=task_dir,
            attempt_dir=attempt_dir,
            publication_state="rejected",
            bundle=None,
            handoff_index=None,
            artifact_refs=refs,
            commit_check=CommitCheck(None, None, None, None, ()),
            warnings=("dispatcher rejected the current attempt handoff",),
        )

    ready_path = attempt_dir / READY_REF
    partial = any((attempt_dir / ref).exists() for ref in (EVIDENCE_REF, HANDOFF_REF))
    if not ready_path.exists():
        try:
            validate_task_inputs_binding(
                attempt_dir,
                expected_task_id=task_id,
                expected_attempt_id=attempt_id,
            )
        except ArtifactBundleError as exc:
            raise ArtifactResolutionError(f"v2 task input binding is invalid: {exc}") from exc
        if required:
            detail = " (partial unpublished handoff exists)" if partial else ""
            raise ArtifactResolutionError(
                f"v2 current attempt has no HANDOFF_READY publication{detail}"
            )
        refs = {
            "task_inputs": str(attempt_dir / TASK_INPUTS_REF),
            "attempt": str(attempt_dir / "ATTEMPT.json"),
        }
        if partial:
            if (attempt_dir / EVIDENCE_REF).exists():
                refs["evidence"] = str(attempt_dir / EVIDENCE_REF)
            if (attempt_dir / HANDOFF_REF).exists():
                refs["handoff"] = str(attempt_dir / HANDOFF_REF)
        return ResolvedTaskArtifacts(
            protocol=protocol,
            artifact_protocol_version=version,
            task_dir=task_dir,
            attempt_dir=attempt_dir,
            publication_state="partial" if partial else "unpublished",
            bundle=None,
            handoff_index=None,
            artifact_refs=refs,
            commit_check=CommitCheck(None, None, None, None, ()),
            warnings=("v2 attempt has a partial unpublished handoff",) if partial else (),
        )

    try:
        bundle = load_bundle(
            attempt_dir,
            expected_task_id=task_id,
            expected_attempt_id=attempt_id,
        )
    except ArtifactBundleError as exc:
        raise ArtifactResolutionError(f"v2 HANDOFF_READY bundle is invalid: {exc}") from exc
    expected_state = _expected_handoff_state(status, attempt_payload)
    if expected_state is not None and bundle.handoff.get("requested_state") != expected_state:
        raise ArtifactResolutionError(
            "v2 HANDOFF_READY requested_state does not match STATUS/profile: "
            f"{bundle.handoff.get('requested_state')!r} != {expected_state!r}"
        )
    source_commit = bundle.handoff.get("source_commit")
    commit = verify_source_commit(task_dir, status, source_commit) if verify_commit else CommitCheck(
        source_commit, None, None, None, ()
    )
    if commit.valid is False:
        raise ArtifactResolutionError(
            "v2 source commit binding is invalid: " + "; ".join(commit.reasons)
        )
    refs = {
        "attempt": str(attempt_dir / "ATTEMPT.json"),
        "task_inputs": str(attempt_dir / TASK_INPUTS_REF),
        "handoff": str(attempt_dir / HANDOFF_REF),
        "evidence": str(attempt_dir / EVIDENCE_REF),
        "handoff_ready": str(attempt_dir / READY_REF),
    }
    commands = attempt_dir / "runtime" / "COMMANDS.ndjson"
    if commands.exists():
        refs["commands"] = str(commands)
    return ResolvedTaskArtifacts(
        protocol=protocol,
        artifact_protocol_version=version,
        task_dir=task_dir,
        attempt_dir=attempt_dir,
        publication_state="published",
        bundle=bundle,
        handoff_index=_v2_handoff_index(bundle),
        artifact_refs=refs,
        commit_check=commit,
    )


def require_current_bundle(
    task_dir: Path,
    status: Mapping[str, Any],
    *,
    expected_requested_state: str | None = None,
    expected_source_commit: str | None = None,
) -> ArtifactBundle:
    """Strict v2 consumer API for review, approval, and merge gates."""

    protocol, _ = protocol_route(status)
    if protocol != "v2":
        raise ArtifactResolutionError(
            f"require_current_bundle is v2-only; route {protocol!r} through the legacy decoder"
        )
    resolved = resolve_task_artifacts(task_dir, status, require_publication=True)
    bundle = resolved.bundle
    if bundle is None:
        raise ArtifactResolutionError("v2 current attempt has no validated bundle")
    attempt = bundle.task_inputs_binding.attempt
    if (
        attempt.get("state") != "completed"
        or attempt.get("handoff_valid") is not True
        or attempt.get("handoff_state") != bundle.handoff.get("requested_state")
    ):
        raise ArtifactResolutionError(
            "v2 strict consumer requires a completed, valid dispatcher handoff"
        )
    if (
        expected_requested_state is not None
        and bundle.handoff.get("requested_state") != expected_requested_state
    ):
        raise ArtifactResolutionError(
            "v2 handoff requested_state does not match the consumer's expected transition"
        )
    if expected_source_commit is not None and bundle.handoff.get("source_commit") != expected_source_commit:
        raise ArtifactResolutionError(
            "v2 handoff source_commit does not match the consumer's expected commit"
        )
    return bundle


def artifact_binding_for_task(
    task_dir: Path,
    status: Mapping[str, Any],
    *,
    expected_requested_state: str | None = None,
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    """Return a review/approval/merge binding after strict current-attempt resolution."""

    return artifact_binding(
        require_current_bundle(
            task_dir,
            status,
            expected_requested_state=expected_requested_state,
            expected_source_commit=expected_source_commit,
        )
    )


def validate_artifact_binding_for_task(
    task_dir: Path,
    status: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    expected_requested_state: str | None = None,
    expected_source_commit: str | None = None,
) -> ArtifactBundle:
    """Revalidate a stored review/approval binding against the current v2 attempt."""

    bundle = require_current_bundle(
        task_dir,
        status,
        expected_requested_state=expected_requested_state,
        expected_source_commit=expected_source_commit,
    )
    try:
        return validate_artifact_binding(
            bundle.attempt_dir,
            expected,
            expected_task_id=bundle.task_inputs_binding.task_id,
            expected_attempt_id=bundle.task_inputs_binding.attempt_id,
            expected_source_commit=expected_source_commit,
        )
    except ArtifactBundleError as exc:
        raise ArtifactResolutionError(f"stored artifact binding is invalid: {exc}") from exc


__all__ = [
    "ArtifactResolutionError",
    "CommitCheck",
    "ResolvedTaskArtifacts",
    "UnsupportedArtifactProtocolError",
    "artifact_binding_for_task",
    "protocol_route",
    "require_current_bundle",
    "resolve_task_artifacts",
    "validate_artifact_binding_for_task",
    "verify_source_commit",
]
