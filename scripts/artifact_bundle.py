#!/usr/bin/env python3
"""Artifact Protocol v2 attempt-local publication and integrity helpers.

This module deliberately contains no FSM transitions.  It publishes an immutable
attempt-local review bundle and verifies the complete reference/digest closure.
Consumers must opt into v2 explicitly; missing v2 artifacts are errors and are
never resolved through task-root legacy files.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from task_contract import TaskContractError, validate_task_inputs_payload


ARTIFACT_PROTOCOL_VERSION = 2
ATTEMPT_REF = "ATTEMPT.json"
TASK_INPUTS_REF = "TASK_INPUTS.json"
HANDOFF_REF = "HANDOFF.json"
EVIDENCE_REF = "EVIDENCE.json"
COMMANDS_REF = "runtime/COMMANDS.ndjson"
READY_REF = "runtime/HANDOFF_READY.json"

_REQUESTED_STATES = {"strategy_review", "verified", "review", "blocked"}
_FULL_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_HANDOFF_FORBIDDEN_FIELDS = {
    "commands",
    "commands_run",
    "files_changed",
    "changed_paths",
    "command_records",
    "required_outputs",
}


class ArtifactBundleError(ValueError):
    """Base class for malformed or inconsistent v2 artifacts."""


class MissingArtifactError(ArtifactBundleError):
    """A required v2 artifact does not exist."""


class IntegrityError(ArtifactBundleError):
    """A v2 reference or digest closure is invalid."""


class PublicationConflictError(ArtifactBundleError):
    """A create-once artifact already exists with different content."""


class UnsafeArtifactReferenceError(ArtifactBundleError):
    """An artifact reference escapes or ambiguously addresses an attempt."""


@dataclass(frozen=True)
class CommandRecord:
    """One validated raw command record and its canonical record digest."""

    record_id: str
    payload: dict[str, Any]
    sha256: str
    line_number: int


@dataclass(frozen=True)
class TaskInputsBinding:
    """The immutable ATTEMPT.json -> TASK_INPUTS.json identity projection."""

    attempt: dict[str, Any]
    task_inputs: dict[str, Any]
    task_id: str
    attempt_id: str
    task_inputs_ref: str
    task_inputs_sha256: str
    attempt_binding_sha256: str


@dataclass(frozen=True)
class ArtifactBundle:
    """A fully validated v2 handoff publication."""

    attempt_dir: Path
    task_inputs_binding: TaskInputsBinding
    handoff: dict[str, Any]
    evidence: dict[str, Any]
    ready: dict[str, Any]
    handoff_sha256: str
    evidence_sha256: str
    ready_sha256: str


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def file_sha256(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except FileNotFoundError as exc:
        raise MissingArtifactError(f"required v2 artifact is missing: {path}") from exc


def _require_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise IntegrityError(f"{label} must be a JSON object")
    return payload


def _require_non_empty_string(payload: Mapping[str, Any], field: str, label: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise IntegrityError(f"{label}.{field} must be a non-empty string")
    return value


def _require_v2(payload: Mapping[str, Any], label: str) -> None:
    if payload.get("artifact_protocol_version") != ARTIFACT_PROTOCOL_VERSION:
        raise IntegrityError(
            f"{label}.artifact_protocol_version must be {ARTIFACT_PROTOCOL_VERSION}"
        )
    if payload.get("schema_version") != ARTIFACT_PROTOCOL_VERSION:
        raise IntegrityError(f"{label}.schema_version must be {ARTIFACT_PROTOCOL_VERSION}")


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise IntegrityError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_full_commit(value: Any, label: str) -> str:
    if not isinstance(value, str) or _FULL_COMMIT_RE.fullmatch(value) is None:
        raise IntegrityError(f"{label} must be an exact full Git object id")
    return value


def safe_ref(
    attempt_dir: Path,
    ref: str,
    *,
    must_exist: bool = True,
    require_file: bool = True,
) -> Path:
    """Resolve a POSIX attempt-local ref without permitting traversal or symlinks."""

    if not isinstance(ref, str) or not ref:
        raise UnsafeArtifactReferenceError("artifact ref must be a non-empty string")
    if "\x00" in ref or "\\" in ref:
        raise UnsafeArtifactReferenceError(f"artifact ref is not a safe POSIX path: {ref!r}")
    if ref.startswith("/") or ref.endswith("/"):
        raise UnsafeArtifactReferenceError(f"artifact ref must be a relative file path: {ref!r}")
    raw_parts = ref.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise UnsafeArtifactReferenceError(f"artifact ref contains an unsafe path segment: {ref!r}")
    pure = PurePosixPath(ref)
    if pure.is_absolute() or str(pure) != ref:
        raise UnsafeArtifactReferenceError(f"artifact ref is not normalized: {ref!r}")

    root = attempt_dir.resolve()
    candidate = root.joinpath(*pure.parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise UnsafeArtifactReferenceError(f"artifact ref escapes the attempt: {ref!r}") from exc

    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise UnsafeArtifactReferenceError(f"artifact ref traverses a symlink: {ref!r}")

    if must_exist and not candidate.exists():
        raise MissingArtifactError(f"required v2 artifact is missing: {ref}")
    if must_exist and require_file and not candidate.is_file():
        raise IntegrityError(f"artifact ref does not name a regular file: {ref}")
    return candidate


def local_ref(attempt_dir: Path, path: Path) -> str:
    """Return a normalized ref for a non-symlink file contained by an attempt."""

    root = attempt_dir.resolve()
    resolved = path.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise UnsafeArtifactReferenceError(f"path is outside the attempt: {path}") from exc
    ref = relative.as_posix()
    safe_ref(attempt_dir, ref, must_exist=path.exists())
    return ref


def _load_json_ref(attempt_dir: Path, ref: str, label: str) -> dict[str, Any]:
    path = safe_ref(attempt_dir, ref)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable JSON: {exc}") from exc
    return _require_object(payload, label)


def publish_json_once(path: Path, payload: Mapping[str, Any]) -> str:
    """Atomically create JSON once; equal JSON is idempotent, different JSON conflicts.

    The target is linked from a durable same-directory temporary file.  This keeps
    a crash from exposing a partial target and gives the create operation O_EXCL
    semantics without ever replacing an existing publication.
    """

    expected = dict(payload)
    content = _json_bytes(expected)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise UnsafeArtifactReferenceError(f"refusing to publish through a symlink: {path}")
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PublicationConflictError(f"published artifact is unreadable: {path}: {exc}") from exc
        if current != expected:
            raise PublicationConflictError(f"published artifact is immutable: {path}")
        return file_sha256(path)

    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        try:
            os.link(temporary, path)
        except FileExistsError:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise PublicationConflictError(
                    f"published artifact is unreadable: {path}: {exc}"
                ) from exc
            if current != expected:
                raise PublicationConflictError(f"published artifact is immutable: {path}")
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return file_sha256(path)


def _attempt_identity_payload(
    *,
    task_id: str,
    attempt_id: str,
    task_inputs_ref: str,
    task_inputs_sha256: str,
) -> dict[str, Any]:
    return {
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "task_inputs_ref": task_inputs_ref,
        "task_inputs_sha256": task_inputs_sha256,
    }


def validate_task_inputs_binding(
    attempt_dir: Path,
    *,
    expected_task_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> TaskInputsBinding:
    """Validate the exact ATTEMPT.json -> TASK_INPUTS.json v2 binding."""

    attempt_dir = attempt_dir.resolve()
    attempt = _load_json_ref(attempt_dir, ATTEMPT_REF, "ATTEMPT.json")
    _require_v2(attempt, "ATTEMPT.json")
    task_id = _require_non_empty_string(attempt, "task_id", "ATTEMPT.json")
    attempt_id = _require_non_empty_string(attempt, "attempt_id", "ATTEMPT.json")
    if expected_task_id is not None and task_id != expected_task_id:
        raise IntegrityError(
            f"ATTEMPT.json task_id {task_id!r} does not match expected task {expected_task_id!r}"
        )
    if expected_attempt_id is not None and attempt_id != expected_attempt_id:
        raise IntegrityError(
            "ATTEMPT.json does not describe the supervised/current attempt: "
            f"{attempt_id!r} != {expected_attempt_id!r}"
        )

    task_inputs_ref = _require_non_empty_string(attempt, "task_inputs_ref", "ATTEMPT.json")
    if task_inputs_ref != TASK_INPUTS_REF:
        raise IntegrityError(f"ATTEMPT.json task_inputs_ref must be {TASK_INPUTS_REF!r}")
    expected_digest = _require_non_empty_string(
        attempt, "task_inputs_sha256", "ATTEMPT.json"
    )
    inputs_path = safe_ref(attempt_dir, task_inputs_ref)
    actual_digest = file_sha256(inputs_path)
    if expected_digest != actual_digest:
        raise IntegrityError(
            "ATTEMPT.json task_inputs_sha256 does not match TASK_INPUTS.json"
        )
    task_inputs = _load_json_ref(attempt_dir, task_inputs_ref, "TASK_INPUTS.json")
    try:
        validate_task_inputs_payload(task_inputs)
    except TaskContractError as exc:
        raise IntegrityError(f"TASK_INPUTS.json contract is invalid: {exc}") from exc
    if task_inputs.get("task_id") != task_id:
        raise IntegrityError("TASK_INPUTS.json task_id does not match ATTEMPT.json")
    if task_inputs.get("attempt_id") != attempt_id:
        raise IntegrityError("TASK_INPUTS.json attempt_id does not match ATTEMPT.json")

    identity = _attempt_identity_payload(
        task_id=task_id,
        attempt_id=attempt_id,
        task_inputs_ref=task_inputs_ref,
        task_inputs_sha256=actual_digest,
    )
    return TaskInputsBinding(
        attempt=attempt,
        task_inputs=task_inputs,
        task_id=task_id,
        attempt_id=attempt_id,
        task_inputs_ref=task_inputs_ref,
        task_inputs_sha256=actual_digest,
        attempt_binding_sha256=sha256_bytes(_canonical_bytes(identity)),
    )


def command_record_sha256(payload: Mapping[str, Any]) -> str:
    """Digest one command record, excluding its optional self-declared digest."""

    canonical = {key: value for key, value in payload.items() if key != "record_sha256"}
    return sha256_bytes(_canonical_bytes(canonical))


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_command_record(
    payload: Mapping[str, Any],
    *,
    line_number: int = 0,
    attempt_dir: Path | None = None,
) -> CommandRecord:
    label = f"COMMANDS.ndjson line {line_number}" if line_number else "command record"
    record = _require_object(dict(payload), label)
    _require_v2(record, label)
    record_id = _require_non_empty_string(record, "record_id", label)
    _require_non_empty_string(record, "task_id", label)
    _require_non_empty_string(record, "attempt_id", label)
    _require_sha256(record.get("task_inputs_sha256"), f"{label}.task_inputs_sha256")
    _require_non_empty_string(record, "check_id", label)
    _require_sha256(
        record.get("acceptance_contract_sha256"),
        f"{label}.acceptance_contract_sha256",
    )
    if record.get("category") != "required_commands":
        raise IntegrityError(f"{label}.category must be 'required_commands'")
    argv = record.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise IntegrityError(f"{label}.argv must be a non-empty string array")
    _require_non_empty_string(record, "cwd", label)
    timeout = record.get("timeout_seconds")
    if not _number(timeout) or timeout <= 0:
        raise IntegrityError(f"{label}.timeout_seconds must be positive")
    timed_out = record.get("timed_out")
    if not isinstance(timed_out, bool):
        raise IntegrityError(f"{label}.timed_out must be boolean")
    exit_code = record.get("exit_code")
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        raise IntegrityError(f"{label}.exit_code must be an integer or null")
    if exit_code is None and not timed_out:
        raise IntegrityError(f"{label}.exit_code may be null only when timed_out=true")
    elapsed = record.get("elapsed_seconds")
    if not _number(elapsed) or elapsed < 0:
        raise IntegrityError(f"{label}.elapsed_seconds must be non-negative")
    _require_non_empty_string(record, "started_at", label)
    _require_non_empty_string(record, "finished_at", label)
    for field in (
        "started_at_epoch",
        "finished_at_epoch",
        "finalization_started_at_epoch",
    ):
        if field in record and (
            not _number(record[field]) or not math.isfinite(float(record[field]))
        ):
            raise IntegrityError(f"{label}.{field} must be finite")
    for field in (
        "source_before_entries_sha256",
        "source_after_entries_sha256",
        "source_snapshot_entries_sha256",
    ):
        if field in record:
            _require_sha256(record[field], f"{label}.{field}")
    if "source_unchanged" in record and not isinstance(
        record["source_unchanged"],
        bool,
    ):
        raise IntegrityError(f"{label}.source_unchanged must be boolean")

    survivors = record.get("surviving_processes")
    if not isinstance(survivors, list):
        raise IntegrityError(f"{label}.surviving_processes must be an array")
    for survivor in survivors:
        if isinstance(survivor, int) and not isinstance(survivor, bool) and survivor > 0:
            continue
        if isinstance(survivor, dict):
            pid = survivor.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                continue
        raise IntegrityError(
            f"{label}.surviving_processes entries must be positive pids or objects with pid"
        )
    if "cleanup_verified" in record and not isinstance(
        record["cleanup_verified"],
        bool,
    ):
        raise IntegrityError(f"{label}.cleanup_verified must be boolean")
    if (
        "cleanup_failure_reason" in record
        and record["cleanup_failure_reason"] is not None
        and not isinstance(record["cleanup_failure_reason"], str)
    ):
        raise IntegrityError(
            f"{label}.cleanup_failure_reason must be a string or null"
        )

    for prefix in ("stdout", "stderr"):
        ref = _require_non_empty_string(record, f"{prefix}_ref", label)
        digest = _require_sha256(record.get(f"{prefix}_sha256"), f"{label}.{prefix}_sha256")
        if attempt_dir is not None:
            path = safe_ref(attempt_dir, ref)
            if file_sha256(path) != digest:
                raise IntegrityError(f"{label}.{prefix}_sha256 does not match {ref}")

    digest = command_record_sha256(record)
    declared = record.get("record_sha256")
    _require_sha256(declared, f"{label}.record_sha256")
    if declared != digest:
        raise IntegrityError(f"{label}.record_sha256 does not match the record")
    return CommandRecord(record_id, record, digest, line_number)


def load_command_records(
    attempt_dir: Path,
    *,
    ref: str = COMMANDS_REF,
    required: bool = True,
) -> list[CommandRecord]:
    """Read and validate structured command facts without inventing evidence."""

    try:
        path = safe_ref(attempt_dir, ref)
    except MissingArtifactError:
        if required:
            raise
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise IntegrityError(f"{ref} is unreadable: {exc}") from exc
    records: list[CommandRecord] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IntegrityError(f"{ref} line {line_number} is invalid JSON: {exc}") from exc
        record = validate_command_record(
            _require_object(raw, f"{ref} line {line_number}"),
            line_number=line_number,
            attempt_dir=attempt_dir,
        )
        if record.record_id in seen:
            raise IntegrityError(f"{ref} contains duplicate record_id {record.record_id!r}")
        seen.add(record.record_id)
        records.append(record)
    return records


def _safe_changed_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise IntegrityError(f"changed path must be relative POSIX path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise IntegrityError(f"changed path is not normalized: {value!r}")
    return value


def build_required_output_bindings(
    worktree: Path,
    source_commit: str,
    paths: Sequence[str],
    *,
    require_live_match: bool = True,
) -> list[dict[str, str]]:
    """Bind required outputs to exact regular-file blobs in one frozen commit."""

    source_commit = _require_full_commit(source_commit, "source_commit")
    root = worktree.resolve()
    normalized = [_safe_changed_path(path) for path in paths]
    if len(normalized) != len(set(normalized)):
        raise IntegrityError("required output paths must not contain duplicates")
    result: list[dict[str, str]] = []
    for relative in normalized:
        candidate = root / relative
        if require_live_match:
            if not candidate.exists():
                raise IntegrityError(f"required outputs are missing: [{relative!r}]")
            if candidate.is_symlink() or not candidate.is_file():
                raise IntegrityError(
                    f"required output {relative!r} must be a regular non-symlink file"
                )
        tree = subprocess.run(
            ["git", "ls-tree", "-z", source_commit, "--", relative],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if tree.returncode != 0:
            detail = tree.stderr.decode("utf-8", errors="replace").strip()
            raise IntegrityError(f"cannot inspect required output {relative!r}: {detail}")
        entries = [item for item in tree.stdout.split(b"\0") if item]
        if len(entries) != 1:
            raise IntegrityError(
                f"required output {relative!r} is not tracked by source_commit"
            )
        metadata, separator, raw_path = entries[0].partition(b"\t")
        fields = metadata.split()
        decoded_path = raw_path.decode("utf-8", errors="surrogateescape")
        if separator != b"\t" or len(fields) != 3 or decoded_path != relative:
            raise IntegrityError(f"unexpected Git tree entry for required output {relative!r}")
        mode = fields[0].decode("ascii", errors="strict")
        object_type = fields[1].decode("ascii", errors="strict")
        object_id = fields[2].decode("ascii", errors="strict")
        if mode not in {"100644", "100755"} or object_type != "blob":
            raise IntegrityError(
                f"required output {relative!r} must be a regular Git blob"
            )
        _require_full_commit(object_id, f"required output {relative!r} git_oid")
        blob = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if blob.returncode != 0:
            detail = blob.stderr.decode("utf-8", errors="replace").strip()
            raise IntegrityError(f"cannot read required output {relative!r}: {detail}")
        digest = sha256_bytes(blob.stdout)
        if require_live_match:
            if sha256_bytes(candidate.read_bytes()) != digest:
                raise IntegrityError(
                    f"required output {relative!r} does not match source_commit"
                )
            executable = bool(candidate.stat().st_mode & 0o111)
            if executable != (mode == "100755"):
                raise IntegrityError(
                    f"required output {relative!r} mode does not match source_commit"
                )
        result.append(
            {
                "path": relative,
                "git_mode": mode,
                "git_oid": object_id,
                "sha256": digest,
            }
        )
    return result


def validate_required_output_bindings(
    worktree: Path,
    source_commit: str,
    descriptors: Any,
    *,
    expected_paths: Sequence[str],
    require_live_match: bool = True,
) -> list[dict[str, str]]:
    """Recompute and compare output bindings at review/dispatch/merge boundaries."""

    if not isinstance(descriptors, list):
        raise IntegrityError("EVIDENCE.json required_outputs must be an array")
    expected = build_required_output_bindings(
        worktree,
        source_commit,
        expected_paths,
        require_live_match=require_live_match,
    )
    if descriptors != expected:
        raise IntegrityError("EVIDENCE.json required_outputs do not match source_commit")
    return expected


def _artifact_descriptor(attempt_dir: Path, ref: str) -> dict[str, str]:
    path = safe_ref(attempt_dir, ref)
    return {"ref": ref, "sha256": file_sha256(path)}


def _descriptor_sequence(attempt_dir: Path, refs: Iterable[str]) -> list[dict[str, str]]:
    values = list(refs)
    if any(not isinstance(ref, str) for ref in values):
        raise IntegrityError("artifact refs must be strings")
    if len(values) != len(set(values)):
        raise IntegrityError("artifact refs must not contain duplicates")
    return [_artifact_descriptor(attempt_dir, ref) for ref in values]


def build_evidence(
    attempt_dir: Path,
    *,
    binding: TaskInputsBinding,
    source_commit: str | None,
    command_record_ids: Sequence[str] = (),
    changed_paths: Sequence[str] = (),
    worktree: Mapping[str, str] | None = None,
    log_refs: Sequence[str] = (),
    artifact_refs: Sequence[str] = (),
    reviewer_evidence: Sequence[Mapping[str, str]] = (),
    required_outputs: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """Build the frozen evidence index from existing attempt-local raw facts."""

    if source_commit is not None:
        _require_full_commit(source_commit, "source_commit")

    commands_path = attempt_dir / COMMANDS_REF
    commands_exist = commands_path.exists()
    records = load_command_records(attempt_dir, required=bool(command_record_ids) or commands_exist)
    by_id = {record.record_id: record for record in records}
    if len(command_record_ids) != len(set(command_record_ids)):
        raise IntegrityError("command_record_ids must not contain duplicates")
    missing = [record_id for record_id in command_record_ids if record_id not in by_id]
    if missing:
        raise IntegrityError(f"evidence references unknown command records: {missing}")

    indexed_commands: list[dict[str, Any]] = []
    for record_id in command_record_ids:
        record = by_id[record_id]
        raw = record.payload
        index: dict[str, Any] = {
            "record_id": record.record_id,
            "record_sha256": record.sha256,
            "record_ref": COMMANDS_REF,
            "category": raw["category"],
            "check_id": raw["check_id"],
            "acceptance_contract_sha256": raw["acceptance_contract_sha256"],
            "argv": raw["argv"],
            "cwd": raw["cwd"],
            "timeout_seconds": raw["timeout_seconds"],
            "exit_code": raw["exit_code"],
            "timed_out": raw["timed_out"],
            "elapsed_seconds": raw["elapsed_seconds"],
            "surviving_processes": raw["surviving_processes"],
            "stdout_ref": raw["stdout_ref"],
            "stdout_sha256": raw["stdout_sha256"],
            "stderr_ref": raw["stderr_ref"],
            "stderr_sha256": raw["stderr_sha256"],
        }
        for optional in (
            "workflow_id",
            "instance_id",
            "finished_at",
            "started_at_epoch",
            "finished_at_epoch",
            "finalization_started_at_epoch",
            "source_before_entries_sha256",
            "source_after_entries_sha256",
            "source_snapshot_entries_sha256",
            "source_unchanged",
            "cleanup_verified",
            "cleanup_failure_reason",
        ):
            if optional in raw:
                index[optional] = raw[optional]
        indexed_commands.append(index)

    normalized_paths = [_safe_changed_path(path) for path in changed_paths]
    if len(normalized_paths) != len(set(normalized_paths)):
        raise IntegrityError("changed_paths must not contain duplicates")

    snapshots: dict[str, dict[str, str]] = {}
    for key, ref in (worktree or {}).items():
        if not isinstance(key, str) or not key:
            raise IntegrityError("worktree keys must be non-empty strings")
        if not isinstance(ref, str):
            raise IntegrityError("worktree values must be artifact refs")
        snapshots[key] = _artifact_descriptor(attempt_dir, ref)

    normalized_reviewers: list[dict[str, str]] = []
    reviewer_keys: set[tuple[str, str]] = set()
    for item in reviewer_evidence:
        reviewer_id = item.get("reviewer_id")
        ref = item.get("ref")
        if not isinstance(reviewer_id, str) or not reviewer_id:
            raise IntegrityError("reviewer evidence requires reviewer_id")
        if not isinstance(ref, str) or not ref:
            raise IntegrityError("reviewer evidence requires an attempt-local ref")
        key = (reviewer_id, ref)
        if key in reviewer_keys:
            raise IntegrityError("reviewer evidence must not contain duplicates")
        reviewer_keys.add(key)
        normalized_reviewers.append({"reviewer_id": reviewer_id, **_artifact_descriptor(attempt_dir, ref)})

    return {
        "schema_version": ARTIFACT_PROTOCOL_VERSION,
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "task_id": binding.task_id,
        "attempt_id": binding.attempt_id,
        "frozen": True,
        "source_commit": source_commit,
        "command_records": indexed_commands,
        "changed_paths": normalized_paths,
        "worktree": snapshots,
        "logs": _descriptor_sequence(attempt_dir, log_refs),
        "artifacts": _descriptor_sequence(attempt_dir, artifact_refs),
        "reviewer_evidence": normalized_reviewers,
        "required_outputs": [dict(item) for item in required_outputs],
    }


def build_handoff(
    *,
    binding: TaskInputsBinding,
    requested_state: str,
    summary: str,
    known_limitations: Sequence[str],
    conditional_blocker: Mapping[str, Any] | None,
    direct_self_review: Mapping[str, Any],
    source_commit: str | None,
    evidence_sha256: str,
) -> dict[str, Any]:
    """Build the deliberately minimal worker transition request."""

    if requested_state not in _REQUESTED_STATES:
        raise IntegrityError(f"unsupported requested_state: {requested_state!r}")
    if not isinstance(summary, str) or not summary.strip():
        raise IntegrityError("handoff summary must be non-empty")
    limitations = list(known_limitations)
    if any(not isinstance(item, str) or not item.strip() for item in limitations):
        raise IntegrityError("known_limitations must be an array of non-empty strings")
    if conditional_blocker is not None and not isinstance(conditional_blocker, Mapping):
        raise IntegrityError("conditional_blocker must be an object or null")
    blocker = dict(conditional_blocker) if conditional_blocker is not None else None
    if requested_state == "blocked":
        if blocker is None:
            raise IntegrityError("blocked handoff requires conditional_blocker")
        _require_non_empty_string(blocker, "blocker_type", "conditional_blocker")
        _require_non_empty_string(blocker, "reason", "conditional_blocker")
    if not isinstance(direct_self_review, Mapping):
        raise IntegrityError("direct_self_review must be an object")
    review = dict(direct_self_review)
    if not isinstance(review.get("performed"), bool):
        raise IntegrityError("direct_self_review.performed must be boolean")
    if not isinstance(review.get("passed"), bool):
        raise IntegrityError("direct_self_review.passed must be boolean")
    if not isinstance(review.get("summary"), str):
        raise IntegrityError("direct_self_review.summary must be a string")
    findings = review.get("findings")
    if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
        raise IntegrityError("direct_self_review.findings must be a string array")
    if requested_state == "verified" and (
        review["performed"] is not True
        or review["passed"] is not True
        or not review["summary"].strip()
    ):
        raise IntegrityError(
            "verified handoff requires a performed, passing Direct self-review with summary"
        )
    if requested_state in {"verified", "review"} and (
        not isinstance(source_commit, str) or not source_commit.strip()
    ):
        raise IntegrityError(f"{requested_state} handoff requires source_commit")
    if source_commit is not None:
        _require_full_commit(source_commit, "source_commit")
    if not isinstance(evidence_sha256, str) or not evidence_sha256:
        raise IntegrityError("evidence_sha256 must be non-empty")

    return {
        "schema_version": ARTIFACT_PROTOCOL_VERSION,
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "task_id": binding.task_id,
        "attempt_id": binding.attempt_id,
        "requested_state": requested_state,
        "summary": summary,
        "known_limitations": limitations,
        "conditional_blocker": blocker,
        "direct_self_review": review,
        "source_commit": source_commit,
        "evidence_ref": EVIDENCE_REF,
        "evidence_sha256": evidence_sha256,
    }


def build_ready(
    *,
    binding: TaskInputsBinding,
    requested_state: str,
    source_commit: str | None,
    handoff_sha256: str,
    evidence_sha256: str,
    published_at_epoch: float | None = None,
) -> dict[str, Any]:
    """Build the final internal publication marker."""

    if published_at_epoch is None:
        published_at_epoch = time.time()
    if not math.isfinite(published_at_epoch) or published_at_epoch <= 0:
        raise IntegrityError("published_at_epoch must be finite and positive")
    published_at = (
        datetime.fromtimestamp(published_at_epoch, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return {
        "schema_version": ARTIFACT_PROTOCOL_VERSION,
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "publication": "handoff_ready",
        "published_at": published_at,
        "published_at_epoch": published_at_epoch,
        "task_id": binding.task_id,
        "attempt_id": binding.attempt_id,
        "attempt_ref": ATTEMPT_REF,
        "attempt_binding_sha256": binding.attempt_binding_sha256,
        "task_inputs_ref": binding.task_inputs_ref,
        "task_inputs_sha256": binding.task_inputs_sha256,
        "handoff_ref": HANDOFF_REF,
        "handoff_sha256": handoff_sha256,
        "evidence_ref": EVIDENCE_REF,
        "evidence_sha256": evidence_sha256,
        "requested_state": requested_state,
        "source_commit": source_commit,
        "source_commit_sha256": sha256_text(source_commit) if source_commit is not None else None,
    }


def publish_bundle(
    attempt_dir: Path,
    *,
    requested_state: str,
    summary: str,
    known_limitations: Sequence[str] = (),
    conditional_blocker: Mapping[str, Any] | None = None,
    direct_self_review: Mapping[str, Any],
    source_commit: str | None = None,
    command_record_ids: Sequence[str] = (),
    changed_paths: Sequence[str] = (),
    worktree: Mapping[str, str] | None = None,
    log_refs: Sequence[str] = (),
    artifact_refs: Sequence[str] = (),
    reviewer_evidence: Sequence[Mapping[str, str]] = (),
    required_outputs: Sequence[Mapping[str, str]] = (),
    expected_task_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> ArtifactBundle:
    """Freeze EVIDENCE, then HANDOFF, and publish HANDOFF_READY strictly last.

    A retry after an interruption succeeds only when every already-published file
    has the same semantic content.  No existing publication is ever replaced.
    """

    attempt_dir = attempt_dir.resolve()
    binding = validate_task_inputs_binding(
        attempt_dir,
        expected_task_id=expected_task_id,
        expected_attempt_id=expected_attempt_id,
    )
    evidence = build_evidence(
        attempt_dir,
        binding=binding,
        source_commit=source_commit,
        command_record_ids=command_record_ids,
        changed_paths=changed_paths,
        worktree=worktree,
        log_refs=log_refs,
        artifact_refs=artifact_refs,
        reviewer_evidence=reviewer_evidence,
        required_outputs=required_outputs,
    )
    evidence_sha256 = publish_json_once(attempt_dir / EVIDENCE_REF, evidence)

    handoff = build_handoff(
        binding=binding,
        requested_state=requested_state,
        summary=summary,
        known_limitations=known_limitations,
        conditional_blocker=conditional_blocker,
        direct_self_review=direct_self_review,
        source_commit=source_commit,
        evidence_sha256=evidence_sha256,
    )
    handoff_sha256 = publish_json_once(attempt_dir / HANDOFF_REF, handoff)

    ready_path = attempt_dir / READY_REF
    published_at_epoch: float | None = None
    if ready_path.is_file() and not ready_path.is_symlink():
        try:
            existing_ready = json.loads(ready_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing_ready = None
        if isinstance(existing_ready, dict) and isinstance(
            existing_ready.get("published_at_epoch"),
            (int, float),
        ):
            published_at_epoch = float(existing_ready["published_at_epoch"])
    ready = build_ready(
        binding=binding,
        requested_state=requested_state,
        source_commit=source_commit,
        handoff_sha256=handoff_sha256,
        evidence_sha256=evidence_sha256,
        published_at_epoch=published_at_epoch,
    )
    publish_json_once(attempt_dir / READY_REF, ready)
    return load_bundle(
        attempt_dir,
        expected_task_id=expected_task_id,
        expected_attempt_id=expected_attempt_id,
        expected_requested_state=requested_state,
        expected_source_commit=source_commit,
    )


def _validate_descriptor(attempt_dir: Path, descriptor: Any, label: str) -> None:
    item = _require_object(descriptor, label)
    ref = _require_non_empty_string(item, "ref", label)
    digest = _require_non_empty_string(item, "sha256", label)
    if file_sha256(safe_ref(attempt_dir, ref)) != digest:
        raise IntegrityError(f"{label}.sha256 does not match {ref}")


def _validate_evidence(
    attempt_dir: Path,
    evidence: dict[str, Any],
    binding: TaskInputsBinding,
) -> None:
    _require_v2(evidence, "EVIDENCE.json")
    if evidence.get("frozen") is not True:
        raise IntegrityError("EVIDENCE.json frozen must be true")
    if evidence.get("task_id") != binding.task_id or evidence.get("attempt_id") != binding.attempt_id:
        raise IntegrityError("EVIDENCE.json identity does not match ATTEMPT.json")
    source_commit = evidence.get("source_commit")
    if source_commit is not None:
        _require_full_commit(source_commit, "EVIDENCE.json source_commit")

    records = load_command_records(attempt_dir, required=False)
    by_id = {record.record_id: record for record in records}
    indexes = evidence.get("command_records")
    if not isinstance(indexes, list):
        raise IntegrityError("EVIDENCE.json command_records must be an array")
    seen: set[str] = set()
    for position, raw_index in enumerate(indexes):
        label = f"EVIDENCE.json.command_records[{position}]"
        index = _require_object(raw_index, label)
        record_id = _require_non_empty_string(index, "record_id", label)
        if record_id in seen:
            raise IntegrityError("EVIDENCE.json indexes a command record more than once")
        seen.add(record_id)
        record = by_id.get(record_id)
        if record is None:
            raise IntegrityError(f"EVIDENCE.json references missing command record {record_id!r}")
        if index.get("record_sha256") != record.sha256:
            raise IntegrityError(f"EVIDENCE.json record digest mismatch for {record_id!r}")
        record_ref = index.get("record_ref")
        if record_ref != COMMANDS_REF:
            raise IntegrityError(f"EVIDENCE.json record_ref mismatch for {record_id!r}")
        if (
            record.payload.get("task_id") != binding.task_id
            or record.payload.get("attempt_id") != binding.attempt_id
            or record.payload.get("task_inputs_sha256") != binding.task_inputs_sha256
        ):
            raise IntegrityError(
                f"EVIDENCE.json command record {record_id!r} has a foreign task binding"
            )
        expected_fields = (
            "category",
            "check_id",
            "acceptance_contract_sha256",
            "argv",
            "cwd",
            "timeout_seconds",
            "exit_code",
            "timed_out",
            "elapsed_seconds",
            "surviving_processes",
            "stdout_ref",
            "stdout_sha256",
            "stderr_ref",
            "stderr_sha256",
        )
        for field in expected_fields:
            if index.get(field) != record.payload.get(field):
                raise IntegrityError(
                    f"EVIDENCE.json command index {record_id!r} disagrees on {field}"
                )

    changed_paths = evidence.get("changed_paths")
    if not isinstance(changed_paths, list):
        raise IntegrityError("EVIDENCE.json changed_paths must be an array")
    normalized = [_safe_changed_path(path) for path in changed_paths]
    if len(normalized) != len(set(normalized)):
        raise IntegrityError("EVIDENCE.json changed_paths contains duplicates")

    worktrees = evidence.get("worktree")
    if not isinstance(worktrees, dict):
        raise IntegrityError("EVIDENCE.json worktree must be an object")
    for key, descriptor in worktrees.items():
        if not isinstance(key, str) or not key:
            raise IntegrityError("EVIDENCE.json worktree keys must be non-empty")
        _validate_descriptor(attempt_dir, descriptor, f"EVIDENCE.json.worktree.{key}")

    for field in ("logs", "artifacts", "reviewer_evidence"):
        values = evidence.get(field)
        if not isinstance(values, list):
            raise IntegrityError(f"EVIDENCE.json {field} must be an array")
        for position, descriptor in enumerate(values):
            label = f"EVIDENCE.json.{field}[{position}]"
            _validate_descriptor(attempt_dir, descriptor, label)
            if field == "reviewer_evidence":
                _require_non_empty_string(descriptor, "reviewer_id", label)

    log_descriptors = {
        item.get("ref"): item.get("sha256")
        for item in evidence["logs"]
        if isinstance(item, dict)
    }
    for record_id in seen:
        record = by_id[record_id].payload
        for prefix in ("stdout", "stderr"):
            if log_descriptors.get(record[f"{prefix}_ref"]) != record[f"{prefix}_sha256"]:
                raise IntegrityError(
                    f"EVIDENCE.json logs omit or disagree with {record_id!r} {prefix}"
                )

    required_outputs = evidence.get("required_outputs")
    if not isinstance(required_outputs, list):
        raise IntegrityError("EVIDENCE.json required_outputs must be an array")
    required_paths: set[str] = set()
    for position, raw in enumerate(required_outputs):
        label = f"EVIDENCE.json.required_outputs[{position}]"
        item = _require_object(raw, label)
        path = _safe_changed_path(_require_non_empty_string(item, "path", label))
        if path in required_paths:
            raise IntegrityError("EVIDENCE.json required_outputs contains duplicate paths")
        required_paths.add(path)
        if item.get("git_mode") not in {"100644", "100755"}:
            raise IntegrityError(f"{label}.git_mode must be 100644 or 100755")
        _require_full_commit(item.get("git_oid"), f"{label}.git_oid")
        _require_sha256(item.get("sha256"), f"{label}.sha256")


def _validate_handoff(handoff: dict[str, Any], binding: TaskInputsBinding) -> None:
    _require_v2(handoff, "HANDOFF.json")
    if handoff.get("task_id") != binding.task_id or handoff.get("attempt_id") != binding.attempt_id:
        raise IntegrityError("HANDOFF.json identity does not match ATTEMPT.json")
    requested_state = handoff.get("requested_state")
    if requested_state not in _REQUESTED_STATES:
        raise IntegrityError("HANDOFF.json requested_state is invalid")
    _require_non_empty_string(handoff, "summary", "HANDOFF.json")
    limitations = handoff.get("known_limitations")
    if not isinstance(limitations, list) or any(
        not isinstance(item, str) or not item.strip() for item in limitations
    ):
        raise IntegrityError("HANDOFF.json known_limitations must be a string array")
    blocker = handoff.get("conditional_blocker")
    if blocker is not None and not isinstance(blocker, dict):
        raise IntegrityError("HANDOFF.json conditional_blocker must be an object or null")
    if requested_state == "blocked":
        blocker = _require_object(blocker, "HANDOFF.json.conditional_blocker")
        _require_non_empty_string(blocker, "blocker_type", "HANDOFF.json.conditional_blocker")
        _require_non_empty_string(blocker, "reason", "HANDOFF.json.conditional_blocker")
    review = handoff.get("direct_self_review")
    if not isinstance(review, dict):
        raise IntegrityError("HANDOFF.json direct_self_review must be an object")
    if not isinstance(review.get("performed"), bool):
        raise IntegrityError("HANDOFF.json direct_self_review.performed must be boolean")
    if not isinstance(review.get("passed"), bool):
        raise IntegrityError("HANDOFF.json direct_self_review.passed must be boolean")
    if not isinstance(review.get("summary"), str):
        raise IntegrityError("HANDOFF.json direct_self_review.summary must be a string")
    findings = review.get("findings")
    if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
        raise IntegrityError("HANDOFF.json direct_self_review.findings must be a string array")
    if requested_state == "verified" and (
        review.get("performed") is not True
        or review.get("passed") is not True
        or not review.get("summary", "").strip()
    ):
        raise IntegrityError(
            "HANDOFF.json verified request requires a passing Direct self-review"
        )
    source_commit = handoff.get("source_commit")
    if requested_state in {"verified", "review"} and (
        not isinstance(source_commit, str) or not source_commit
    ):
        raise IntegrityError(f"HANDOFF.json {requested_state} requires source_commit")
    if source_commit is not None:
        _require_full_commit(source_commit, "HANDOFF.json source_commit")
    if handoff.get("evidence_ref") != EVIDENCE_REF:
        raise IntegrityError(f"HANDOFF.json evidence_ref must be {EVIDENCE_REF!r}")
    _require_non_empty_string(handoff, "evidence_sha256", "HANDOFF.json")
    overlap = sorted(_HANDOFF_FORBIDDEN_FIELDS.intersection(handoff))
    if overlap:
        raise IntegrityError(f"HANDOFF.json duplicates evidence fields: {overlap}")


def load_bundle(
    attempt_dir: Path,
    *,
    expected_task_id: str | None = None,
    expected_attempt_id: str | None = None,
    expected_requested_state: str | None = None,
    expected_source_commit: str | None = None,
) -> ArtifactBundle:
    """Load a v2 bundle and verify every reference and digest in its closure."""

    attempt_dir = attempt_dir.resolve()
    binding = validate_task_inputs_binding(
        attempt_dir,
        expected_task_id=expected_task_id,
        expected_attempt_id=expected_attempt_id,
    )
    evidence = _load_json_ref(attempt_dir, EVIDENCE_REF, "EVIDENCE.json")
    handoff = _load_json_ref(attempt_dir, HANDOFF_REF, "HANDOFF.json")
    ready_path = safe_ref(attempt_dir, READY_REF)
    ready = _load_json_ref(attempt_dir, READY_REF, "HANDOFF_READY.json")
    _validate_evidence(attempt_dir, evidence, binding)
    _validate_handoff(handoff, binding)

    evidence_digest = file_sha256(safe_ref(attempt_dir, EVIDENCE_REF))
    handoff_digest = file_sha256(safe_ref(attempt_dir, HANDOFF_REF))
    ready_digest = file_sha256(safe_ref(attempt_dir, READY_REF))
    if handoff.get("evidence_sha256") != evidence_digest:
        raise IntegrityError("HANDOFF.json evidence_sha256 does not match EVIDENCE.json")
    if handoff.get("source_commit") != evidence.get("source_commit"):
        raise IntegrityError("HANDOFF.json source_commit does not match EVIDENCE.json")

    _require_v2(ready, "HANDOFF_READY.json")
    if ready.get("publication") != "handoff_ready":
        raise IntegrityError("HANDOFF_READY.json publication must be 'handoff_ready'")
    published_at_epoch = ready.get("published_at_epoch")
    published_at = ready.get("published_at")
    if (
        not _number(published_at_epoch)
        or not math.isfinite(float(published_at_epoch))
        or float(published_at_epoch) <= 0
        or not isinstance(published_at, str)
    ):
        raise IntegrityError("HANDOFF_READY.json publication timestamp is invalid")
    try:
        parsed_published_at = datetime.fromisoformat(
            published_at.replace("Z", "+00:00")
        ).timestamp()
    except ValueError as exc:
        raise IntegrityError(
            "HANDOFF_READY.json published_at is invalid"
        ) from exc
    if not math.isclose(
        parsed_published_at,
        float(published_at_epoch),
        rel_tol=0,
        abs_tol=0.0015,
    ):
        raise IntegrityError(
            "HANDOFF_READY.json published_at does not match published_at_epoch"
        )
    if ready.get("task_id") != binding.task_id or ready.get("attempt_id") != binding.attempt_id:
        raise IntegrityError("HANDOFF_READY.json identity does not match ATTEMPT.json")
    expected_refs = {
        "attempt_ref": ATTEMPT_REF,
        "task_inputs_ref": binding.task_inputs_ref,
        "handoff_ref": HANDOFF_REF,
        "evidence_ref": EVIDENCE_REF,
    }
    for field, expected in expected_refs.items():
        if ready.get(field) != expected:
            raise IntegrityError(f"HANDOFF_READY.json {field} must be {expected!r}")
    expected_digests = {
        "attempt_binding_sha256": binding.attempt_binding_sha256,
        "task_inputs_sha256": binding.task_inputs_sha256,
        "handoff_sha256": handoff_digest,
        "evidence_sha256": evidence_digest,
    }
    for field, expected in expected_digests.items():
        if ready.get(field) != expected:
            raise IntegrityError(f"HANDOFF_READY.json {field} does not match its artifact")
    requested_state = handoff["requested_state"]
    source_commit = handoff.get("source_commit")
    if ready.get("requested_state") != requested_state:
        raise IntegrityError("HANDOFF_READY.json requested_state does not match HANDOFF.json")
    if ready.get("source_commit") != source_commit:
        raise IntegrityError("HANDOFF_READY.json source_commit does not match HANDOFF.json")
    expected_source_digest = sha256_text(source_commit) if source_commit is not None else None
    if ready.get("source_commit_sha256") != expected_source_digest:
        raise IntegrityError("HANDOFF_READY.json source_commit_sha256 is invalid")
    if expected_requested_state is not None and requested_state != expected_requested_state:
        raise IntegrityError(
            f"handoff requested_state {requested_state!r} does not match expected state "
            f"{expected_requested_state!r}"
        )
    if expected_source_commit is not None and source_commit != expected_source_commit:
        raise IntegrityError("handoff source_commit does not match the expected/approved commit")

    return ArtifactBundle(
        attempt_dir=attempt_dir,
        task_inputs_binding=binding,
        handoff=handoff,
        evidence=evidence,
        ready=ready,
        handoff_sha256=handoff_digest,
        evidence_sha256=evidence_digest,
        ready_sha256=ready_digest,
    )


def artifact_binding(bundle_or_attempt: ArtifactBundle | Path) -> dict[str, Any]:
    """Create the exact review/approval/merge binding for a validated bundle."""

    bundle = (
        bundle_or_attempt
        if isinstance(bundle_or_attempt, ArtifactBundle)
        else load_bundle(Path(bundle_or_attempt))
    )
    binding = bundle.task_inputs_binding
    payload: dict[str, Any] = {
        "schema_version": ARTIFACT_PROTOCOL_VERSION,
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "task_id": binding.task_id,
        "attempt_id": binding.attempt_id,
        "attempt_binding_sha256": binding.attempt_binding_sha256,
        "task_inputs_ref": binding.task_inputs_ref,
        "task_inputs_sha256": binding.task_inputs_sha256,
        "handoff_ref": HANDOFF_REF,
        "handoff_sha256": bundle.handoff_sha256,
        "evidence_ref": EVIDENCE_REF,
        "evidence_sha256": bundle.evidence_sha256,
        "ready_ref": READY_REF,
        "ready_sha256": bundle.ready_sha256,
        "requested_state": bundle.handoff["requested_state"],
        "source_commit": bundle.handoff.get("source_commit"),
        "source_commit_sha256": bundle.ready.get("source_commit_sha256"),
    }
    payload["binding_sha256"] = sha256_bytes(_canonical_bytes(payload))
    return payload


def validate_artifact_binding(
    attempt_dir: Path,
    expected: Mapping[str, Any],
    *,
    expected_task_id: str | None = None,
    expected_attempt_id: str | None = None,
    expected_source_commit: str | None = None,
) -> ArtifactBundle:
    """Revalidate a stored review/approval binding against live immutable files."""

    bundle = load_bundle(
        attempt_dir,
        expected_task_id=expected_task_id,
        expected_attempt_id=expected_attempt_id,
        expected_source_commit=expected_source_commit,
    )
    current = artifact_binding(bundle)
    if dict(expected) != current:
        raise IntegrityError("stored artifact binding does not match the current v2 bundle")
    return bundle


__all__ = [
    "ARTIFACT_PROTOCOL_VERSION",
    "ArtifactBundle",
    "ArtifactBundleError",
    "CommandRecord",
    "IntegrityError",
    "MissingArtifactError",
    "PublicationConflictError",
    "TaskInputsBinding",
    "UnsafeArtifactReferenceError",
    "artifact_binding",
    "build_required_output_bindings",
    "build_evidence",
    "build_handoff",
    "build_ready",
    "command_record_sha256",
    "file_sha256",
    "load_bundle",
    "load_command_records",
    "local_ref",
    "publish_bundle",
    "publish_json_once",
    "safe_ref",
    "sha256_bytes",
    "sha256_text",
    "validate_artifact_binding",
    "validate_required_output_bindings",
    "validate_command_record",
    "validate_task_inputs_binding",
]
