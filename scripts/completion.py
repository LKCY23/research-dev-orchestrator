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
import signal
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protocol import ARTIFACT_PROTOCOL_VERSION, load_json, parse_iso, utc_now


LEGACY_ARTIFACT_PROTOCOL_VERSION = 1
MAX_PUBLICATION_MARKER_BYTES = 64 * 1024
MAX_GIT_CONTROL_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class CompletionValidationResult:
    valid: bool
    reasons: tuple[str, ...]
    payload: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None


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


def inspect_publication_candidate(
    path: Path,
    *,
    artifact_protocol_version: int,
    task_dir: Path,
    attempt_id: str,
) -> CompletionValidationResult:
    """Perform a bounded marker-only check before stopping the worker.

    Complete bundle validation can hash large evidence and output artifacts, so
    supervisors run it only after the process tree is quiescent.
    """

    try:
        expected_path = publication_path(
            task_dir,
            attempt_id,
            artifact_protocol_version,
        )
    except ValueError as exc:
        return CompletionValidationResult(False, (str(exc),))
    if not _same_path(path, expected_path):
        return CompletionValidationResult(
            False,
            ("publication candidate path does not belong to the attempt",),
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        return CompletionValidationResult(
            False,
            (f"publication marker is missing or unsafe: {exc}",),
        )
    try:
        metadata_before = os.fstat(descriptor)
        if not stat.S_ISREG(metadata_before.st_mode):
            return CompletionValidationResult(
                False,
                ("publication marker must be a regular non-symlink file",),
            )
        if metadata_before.st_size > MAX_PUBLICATION_MARKER_BYTES:
            return CompletionValidationResult(
                False,
                ("publication marker exceeds the bounded marker size",),
            )
        chunks: list[bytes] = []
        remaining = MAX_PUBLICATION_MARKER_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        metadata = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_ctime_ns",
            "st_mtime_ns",
        )
        if any(
            getattr(metadata_before, field) != getattr(metadata, field)
            for field in stable_fields
        ):
            return CompletionValidationResult(
                False,
                ("publication marker changed while it was being inspected",),
            )
    finally:
        os.close(descriptor)
    if len(raw) > MAX_PUBLICATION_MARKER_BYTES:
        return CompletionValidationResult(
            False,
            ("publication marker exceeds the bounded marker size",),
        )
    try:
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return CompletionValidationResult(
            False,
            (f"publication marker is unreadable: {exc}",),
        )
    if not isinstance(payload, dict):
        return CompletionValidationResult(
            False,
            ("publication marker must be a JSON object",),
        )
    reasons: list[str] = []
    if payload.get("attempt_id") != attempt_id:
        reasons.append("publication marker attempt_id does not match")
    if artifact_protocol_version == ARTIFACT_PROTOCOL_VERSION:
        if payload.get("schema_version") != ARTIFACT_PROTOCOL_VERSION:
            reasons.append("HANDOFF_READY schema_version must be 2")
        if payload.get("artifact_protocol_version") != ARTIFACT_PROTOCOL_VERSION:
            reasons.append("HANDOFF_READY artifact_protocol_version must be 2")
        if payload.get("publication") != "handoff_ready":
            reasons.append("HANDOFF_READY publication must be handoff_ready")
        status_raw = _stable_bounded_path(
            task_dir / "STATUS.json",
            MAX_PUBLICATION_MARKER_BYTES,
        )
        if status_raw is None:
            reasons.append("task status is missing or unsafe")
        else:
            try:
                status_payload = json.loads(status_raw)
            except (UnicodeError, json.JSONDecodeError):
                status_payload = None
            if not isinstance(status_payload, dict):
                reasons.append("task status must be a JSON object")
            else:
                if status_payload.get("artifact_protocol_version") != 2:
                    reasons.append("task is not using artifact protocol v2")
                if status_payload.get("current_attempt_id") != attempt_id:
                    reasons.append("publication is not for the current task attempt")
                if status_payload.get("task_id") != payload.get("task_id"):
                    reasons.append("publication task_id does not match task status")
    elif artifact_protocol_version == LEGACY_ARTIFACT_PROTOCOL_VERSION:
        if payload.get("schema_version") != LEGACY_ARTIFACT_PROTOCOL_VERSION:
            reasons.append("COMPLETION schema_version must be 1")
    else:
        reasons.append("unsupported artifact protocol version")
    receipt = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "ctime": float(metadata.st_ctime),
        "ctime_ns": int(metadata.st_ctime_ns),
        "mtime_ns": int(metadata.st_mtime_ns),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "size": int(metadata.st_size),
    }
    return CompletionValidationResult(
        not reasons,
        tuple(reasons),
        payload,
        receipt,
    )


def inspect_candidate_source_state(
    worktree: Path,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = 1.0,
) -> CompletionValidationResult:
    """Validate the final clean Git state after the worker tree is quiescent."""

    requested_state = payload.get("requested_state")
    source_commit = payload.get("source_commit")
    if source_commit is None and requested_state not in {"verified", "review"}:
        return CompletionValidationResult(True, (), payload, {})
    if not isinstance(source_commit, str) or _full_commit(source_commit) is None:
        return CompletionValidationResult(
            False,
            ("final-state publication has no full source_commit",),
            payload,
        )

    head_result = inspect_candidate_source_head(worktree, payload)
    if not head_result.valid:
        return head_result

    def run_git(*arguments: str) -> tuple[int, bytes]:
        process = subprocess.Popen(
            ["git", "-C", str(worktree), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, _stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return 124, b""
        return int(process.returncode), stdout

    status_code, status_output = run_git(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    reasons: list[str] = []
    if status_code != 0:
        reasons.append("supervisor could not inspect publication worktree state")
    elif status_output and requested_state != "blocked":
        reasons.append("publication worktree is not clean")
    return CompletionValidationResult(
        not reasons,
        tuple(reasons),
        payload,
        {
            **(head_result.receipt or {}),
            "worktree_clean": status_code == 0 and not status_output,
        },
    )


def _stable_bounded_path(path: Path, max_bytes: int) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) > max_bytes
            or any(
                getattr(before, field) != getattr(after, field)
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_size",
                    "st_ctime_ns",
                    "st_mtime_ns",
                )
            )
        ):
            return None
        return raw
    finally:
        os.close(descriptor)


def _git_control_dirs(worktree: Path) -> tuple[Path, Path] | None:
    marker = worktree / ".git"
    if marker.is_dir() and not marker.is_symlink():
        git_dir = marker.resolve()
    else:
        raw = _stable_bounded_path(marker, 4096)
        if raw is None:
            return None
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeError:
            return None
        if not line.startswith("gitdir: "):
            return None
        candidate = Path(line[8:])
        git_dir = (
            candidate if candidate.is_absolute() else marker.parent / candidate
        ).resolve()
    common_dir = git_dir
    common_ref = git_dir / "commondir"
    if common_ref.is_file() and not common_ref.is_symlink():
        raw = _stable_bounded_path(common_ref, 4096)
        if raw is None:
            return None
        try:
            candidate = Path(raw.decode("utf-8").strip())
        except UnicodeError:
            return None
        common_dir = (
            candidate if candidate.is_absolute() else git_dir / candidate
        ).resolve()
    return git_dir, common_dir


def _full_commit(value: str) -> str | None:
    value = value.strip()
    return (
        value
        if len(value) in {40, 64}
        and all(char in "0123456789abcdef" for char in value)
        else None
    )


def inspect_candidate_source_head(
    worktree: Path,
    payload: dict[str, Any],
) -> CompletionValidationResult:
    """Read the supervised Git HEAD without spawning a process."""

    requested_state = payload.get("requested_state")
    source_commit = payload.get("source_commit")
    if source_commit is None and requested_state not in {"verified", "review"}:
        return CompletionValidationResult(True, (), payload, {})
    if not isinstance(source_commit, str) or _full_commit(source_commit) is None:
        return CompletionValidationResult(
            False,
            ("final-state publication has no full source_commit",),
            payload,
        )
    directories = _git_control_dirs(worktree)
    if directories is None:
        return CompletionValidationResult(
            False,
            ("supervisor could not resolve the publication Git directory",),
            payload,
        )
    git_dir, common_dir = directories
    raw_head = _stable_bounded_path(git_dir / "HEAD", 4096)
    if raw_head is None:
        return CompletionValidationResult(
            False,
            ("supervisor could not read publication Git HEAD",),
            payload,
        )
    try:
        head_value = raw_head.decode("ascii").strip()
    except UnicodeError:
        head_value = ""
    commit = _full_commit(head_value)
    if commit is None and head_value.startswith("ref: "):
        ref = head_value[5:]
        ref_path = Path(ref)
        if ref_path.is_absolute() or ".." in ref_path.parts:
            ref = ""
        for root in (common_dir, git_dir):
            raw_ref = _stable_bounded_path(root / ref, 4096) if ref else None
            if raw_ref is not None:
                try:
                    commit = _full_commit(raw_ref.decode("ascii"))
                except UnicodeError:
                    commit = None
                if commit is not None:
                    break
        if commit is None and ref:
            packed = _stable_bounded_path(
                common_dir / "packed-refs",
                MAX_GIT_CONTROL_BYTES,
            )
            if packed is not None:
                try:
                    lines = packed.decode("ascii").splitlines()
                except UnicodeError:
                    lines = []
                suffix = f" {ref}"
                for line in lines:
                    if not line.startswith(("#", "^")) and line.endswith(suffix):
                        commit = _full_commit(line.split(" ", 1)[0])
                        if commit is not None:
                            break
    if commit != source_commit:
        return CompletionValidationResult(
            False,
            ("publication source_commit does not match supervised Git HEAD",),
            payload,
            {"source_commit": commit},
        )
    return CompletionValidationResult(
        True,
        (),
        payload,
        {"source_commit": commit},
    )


def v2_publication_dependency_latest_ctime(
    task_dir: Path,
    attempt_id: str,
) -> float:
    """Return the latest trusted ctime among files bound by HANDOFF_READY."""

    from artifact_bundle import (
        ATTEMPT_REF,
        COMMANDS_REF,
        EVIDENCE_REF,
        HANDOFF_REF,
        READY_REF,
        safe_ref,
    )

    attempt_dir = task_dir / "attempts" / attempt_id
    refs: set[str] = {
        ATTEMPT_REF,
        EVIDENCE_REF,
        HANDOFF_REF,
    }

    def collect_refs(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    isinstance(item, str)
                    and (key == "ref" or key.endswith("_ref"))
                ):
                    refs.add(item)
                collect_refs(item)
        elif isinstance(value, list):
            for item in value:
                collect_refs(item)

    for ref in (EVIDENCE_REF, HANDOFF_REF, READY_REF):
        collect_refs(load_json(safe_ref(attempt_dir, ref)))
    ready = load_json(safe_ref(attempt_dir, READY_REF))
    external_ctimes: list[float] = []
    if ready.get("requested_state") == "strategy_review":
        submission_ref = "runtime/STRATEGY_SUBMISSION.json"
        submission_path = safe_ref(attempt_dir, submission_ref)
        submission = load_json(submission_path)
        refs.add(submission_ref)
        revision = submission.get("strategy_revision")
        expected_name = (
            f"STRATEGY-v{revision:03d}.json"
            if type(revision) is int and revision > 0
            else ""
        )
        expected_ref = f"../../strategy/{expected_name}" if expected_name else ""
        if submission.get("strategy_ref") != expected_ref:
            raise ValueError("strategy submission ref is not canonical")
        strategy_dir = task_dir / "strategy"
        strategy_path = strategy_dir / expected_name
        if (
            strategy_dir.is_symlink()
            or strategy_path.is_symlink()
            or not strategy_path.is_file()
        ):
            raise ValueError("submitted strategy is unsafe or missing")
        from strategy import canonical_digest

        strategy_payload = load_json(strategy_path)
        if canonical_digest(strategy_payload) != submission.get("strategy_sha256"):
            raise ValueError("submitted strategy digest does not match")
        external_ctimes.append(
            float(strategy_path.stat(follow_symlinks=False).st_ctime)
        )
    commands_path = attempt_dir / COMMANDS_REF
    if commands_path.is_file() and not commands_path.is_symlink():
        for line in commands_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                collect_refs(json.loads(line))

    latest = 0.0
    for ref in refs:
        candidate = safe_ref(attempt_dir, ref)
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"publication dependency is unsafe or missing: {ref}")
        latest = max(
            latest,
            float(candidate.stat(follow_symlinks=False).st_ctime),
        )
    for external_ctime in external_ctimes:
        latest = max(latest, external_ctime)
    return latest


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
