#!/usr/bin/env python3
"""Role-safe local command surface for coordinator and worker actions."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import hashlib
import math
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from artifact_bundle import (
    ArtifactBundleError,
    READY_REF,
    artifact_binding,
    build_required_output_bindings,
    command_record_sha256,
    file_sha256,
    load_bundle,
    load_command_records,
    publish_bundle,
    publish_json_once,
    safe_ref,
    validate_artifact_binding,
    validate_required_output_bindings,
    validate_task_inputs_binding,
)
from artifact_resolver import ArtifactResolutionError, require_current_bundle
from check_broker import broker_directory_for_attempt, run_brokered
from completion import write_completion
from config import load_config
from dispatch_assets import render_worker_prompt
from dependency_context import (
    DependencyContextError,
    load_bound_dependency_context,
)
from protocol import (
    ARTIFACT_PROTOCOL_VERSION,
    EventJournalError,
    SKILL_ROOT,
    append_event,
    artifact_protocol_version,
    load_json,
    parse_iso,
    read_event_journal,
    repo_root,
    utc_now,
    write_json,
)
from strategy import (
    StrategyValidationError,
    build_strategy_scaffold,
    canonical_digest,
    load_approved_strategy,
    load_bound_approved_strategy,
    preflight_strategy,
    review_strategy,
    submit_strategy,
)
from status_projection import resolve_status_projection
from supervisor import (
    audit_supervision_token,
    run_supervised,
    terminate_current_supervision,
    validate_attempt_deadline_payload,
)
from task_contract import (
    TASK_INPUT_FILENAMES,
    TaskContractError,
    parse_acceptance_markdown,
    parse_execution_policy,
    validate_task_inputs_payload,
)
from task_budget import TaskBudgetError, assess_task_budget
from tmux_lifecycle import (
    TmuxLifecycleError,
    build_tmux_inventory,
    kill_live_tmux_session,
    list_live_tmux_sessions,
    load_attempt_tmux_identity,
    revalidate_live_tmux_identity,
)
from worktree_fingerprint import fingerprint


FINALIZATION_REF = "runtime/FINALIZATION.json"
FINALIZATION_SNAPSHOT_REF = "runtime/finalization-worktree.json"
ATTEMPT_DEADLINE_REF = "runtime/DEADLINE.json"
DEFAULT_FINALIZATION_GRACE_SECONDS = 90.0


@contextmanager
def attempt_artifact_lock(attempt_value: str | Path, *, exclusive: bool):
    """Coordinate live v2 command writers with immutable finalization.

    Checks and workflow commands hold a shared lock for their whole supervised
    process lifetime. Finalization takes the exclusive lock, so READY cannot be
    published while a command can still append raw facts afterward.
    """

    attempt = Path(attempt_value).resolve()
    if attempt.parent.name != "attempts" or attempt.name in {"", ".", ".."}:
        raise SystemExit("attempt directory must be task-local under attempts/")
    runtime = attempt / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    lock_path = runtime / "ARTIFACT_LOCK"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield attempt
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def task_dir(value: str) -> Path:
    path = Path(value).resolve()
    if not (path / "STATUS.json").exists():
        raise SystemExit(f"invalid task directory: {path}")
    return path


def task_protocol(path: Path, status: dict[str, Any] | None = None) -> int:
    """Resolve one task's explicit artifact protocol, failing closed on unknown versions."""

    version = artifact_protocol_version(path, status)
    if version not in {1, ARTIFACT_PROTOCOL_VERSION}:
        raise SystemExit("task uses an unknown artifact protocol version")
    return version


def _strategy_profile_is_full(protocol_version: int, profile: Any) -> bool:
    """Preserve the legacy missing-profile Full default while v2 fails closed."""

    return profile == "full" or (protocol_version == 1 and profile is None)


def _read_text_file(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"cannot read {label}: {exc}") from exc


def _require_attempt_ownership(
    attempt_value: str | Path,
    *,
    allow_ready: bool,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any], Any]:
    """Bind a worker mutation to the exact active v2 attempt and dispatch locks."""

    attempt = Path(attempt_value).resolve()
    if attempt.parent.name != "attempts":
        raise SystemExit("attempt directory must be task-local under attempts/")
    task = attempt.parent.parent.resolve()
    if attempt.parent.resolve() != (task / "attempts").resolve() or attempt.name in {"", ".", ".."}:
        raise SystemExit("attempt directory is not owned by the task")
    status = load_json(task / "STATUS.json")
    if not isinstance(status, dict):
        raise SystemExit("STATUS.json must contain an object")
    if task_protocol(task, status) != ARTIFACT_PROTOCOL_VERSION:
        raise SystemExit("this command requires Artifact Protocol v2")
    if status.get("current_attempt_id") != attempt.name:
        raise SystemExit("command requires the task's current attempt")
    if status.get("state") not in {"planning", "running"}:
        raise SystemExit("command requires an active planning or execution task")
    metadata = load_json(attempt / "ATTEMPT.json")
    if not isinstance(metadata, dict):
        raise SystemExit("ATTEMPT.json must contain an object")
    if metadata.get("attempt_id") != attempt.name or metadata.get("task_id") != status.get("task_id"):
        raise SystemExit("ATTEMPT.json identity does not match the active task/attempt")
    if metadata.get("state") not in {"created", "running"}:
        raise SystemExit("attempt is not active")

    dispatch_lock = task / ".dispatch-lock" / "attempt_id"
    if not dispatch_lock.is_file() or _read_text_file(dispatch_lock, label="dispatch lock").strip() != attempt.name:
        raise SystemExit("active attempt does not own the dispatch lock")
    protocol_lock = task / "LOCK"
    if not protocol_lock.is_file():
        raise SystemExit("active attempt does not own the task LOCK")
    lock_lines = _read_text_file(protocol_lock, label="task LOCK").splitlines()
    if f"attempt_id: {attempt.name}" not in lock_lines:
        raise SystemExit("task LOCK belongs to a different attempt")
    if not allow_ready and (attempt / READY_REF).exists():
        raise SystemExit("handoff is already published; command evidence is frozen")
    try:
        binding = validate_task_inputs_binding(
            attempt,
            expected_task_id=str(status.get("task_id")),
            expected_attempt_id=attempt.name,
        )
    except ArtifactBundleError as exc:
        raise SystemExit(f"invalid attempt input binding: {exc}") from exc
    if status.get("profile") == "full" and metadata.get("phase") == "execution":
        _bound_strategy_for_attempt(task, attempt, metadata)
    return attempt, task, status, metadata, binding


def _bound_strategy_for_attempt(
    task: Path,
    attempt: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the exact Full strategy bound before this worker was launched."""

    try:
        profile = load_json(attempt / "runtime" / "BACKEND_PROFILE.json")
    except Exception as exc:
        raise SystemExit(f"attempt backend profile cannot bind its strategy: {exc}") from exc
    if not isinstance(profile, dict):
        raise SystemExit("attempt backend profile must be an object")
    for field in ("strategy_id", "strategy_revision", "strategy_sha256"):
        if profile.get(field) != metadata.get(field):
            raise SystemExit(
                f"ATTEMPT.{field} does not match the launch-time backend profile"
            )
    try:
        strategy, _review = load_bound_approved_strategy(
            task,
            strategy_id=metadata.get("strategy_id"),
            strategy_sha256=metadata.get("strategy_sha256"),
            revision=metadata.get("strategy_revision"),
        )
    except (OSError, StrategyValidationError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"attempt-frozen strategy binding is invalid: {exc}") from exc
    return strategy


def _validate_frozen_sources(attempt: Path, task: Path, binding: Any) -> dict[str, Any]:
    """Verify task-root canonical inputs still match this attempt's immutable snapshot."""

    try:
        inputs = validate_task_inputs_payload(binding.task_inputs)
    except TaskContractError as exc:
        raise SystemExit(f"invalid TASK_INPUTS.json: {exc}") from exc
    task_root = task.resolve()
    descriptors = inputs["inputs"]
    input_names = {
        "task": "TASK.md",
        "context": "CONTEXT.md",
        "acceptance": "ACCEPTANCE.md",
        "execution_policy": "EXECUTION_POLICY.json",
    }
    for name, filename in input_names.items():
        descriptor = descriptors[name]
        source = (task_root / descriptor["ref"]).resolve()
        raw_expected = task_root / filename
        expected = raw_expected.resolve()
        if source != expected or expected.parent != task_root:
            raise SystemExit(f"TASK_INPUTS.json {filename} ref does not resolve to the canonical task input")
        if raw_expected.is_symlink() or not expected.is_file():
            raise SystemExit(f"canonical task input is missing or unsafe: {filename}")
        if file_sha256(expected) != descriptor["sha256"]:
            raise SystemExit(
                f"task input contract drifted after dispatch: {filename}; create a revision task"
            )
    try:
        acceptance = parse_acceptance_markdown(
            (task_root / "ACCEPTANCE.md").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, TaskContractError) as exc:
        raise SystemExit(f"frozen ACCEPTANCE.md is invalid: {exc}") from exc
    return acceptance["contract"]


def _worktree_for_attempt(metadata: dict[str, Any]) -> Path:
    runtime = metadata.get("runtime")
    cwd = runtime.get("cwd") if isinstance(runtime, dict) else None
    if not isinstance(cwd, str) or not cwd:
        raise SystemExit("ATTEMPT.json runtime.cwd is missing")
    worktree = Path(cwd).resolve()
    if not worktree.is_dir():
        raise SystemExit(f"attempt worktree does not exist: {worktree}")
    return worktree


def _command_cwd(worktree: Path, relative: str) -> Path:
    candidate = (worktree / relative).resolve()
    try:
        candidate.relative_to(worktree.resolve())
    except ValueError as exc:
        raise SystemExit(f"acceptance command cwd escapes the task worktree: {relative!r}") from exc
    if not candidate.is_dir():
        raise SystemExit(f"acceptance command cwd does not exist: {relative!r}")
    return candidate


def _append_command_record(path: Path, record: dict[str, Any]) -> None:
    """Append one complete NDJSON record with a single O_APPEND write."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise OSError(f"short append: wrote {written} of {len(encoded)} bytes")
        os.fsync(descriptor)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _check_command_locked(args: argparse.Namespace) -> int:
    """Execute one exact required acceptance command under shared supervision."""

    attempt, task, status, metadata, binding = _require_attempt_ownership(
        args.attempt_dir,
        allow_ready=False,
    )
    _emit_deadline_notice(attempt)
    if metadata.get("phase") != "execution" or status.get("state") != "running":
        raise SystemExit("rdo check requires the current running execution attempt")
    if bool(getattr(args, "workflow_id", "")) != bool(getattr(args, "instance_id", "")):
        raise SystemExit("--workflow-id and --instance-id must be supplied together")
    if getattr(args, "workflow_id", ""):
        active: set[tuple[str, str]] = set()
        for item in workflow_events(attempt):
            key = (str(item.get("workflow_id")), str(item.get("instance_id")))
            if item.get("event") == "workflow_started":
                active.add(key)
            elif item.get("event") in {
                "workflow_completed",
                "workflow_timed_out",
                "workflow_cancelled",
            }:
                active.discard(key)
        if (args.workflow_id, args.instance_id) not in active:
            raise SystemExit("rdo check workflow binding is not an active workflow instance")
    contract = _validate_frozen_sources(attempt, task, binding)
    definitions = {
        item["id"]: item for item in contract.get("required_commands", [])
    }
    definition = definitions.get(args.check_id)
    if definition is None:
        raise SystemExit(f"unknown required acceptance check id: {args.check_id!r}")

    worktree = _worktree_for_attempt(metadata)
    finalization_marker: dict[str, Any] | None = None
    frozen_entries_sha256: str | None = None
    if (attempt / FINALIZATION_REF).exists():
        finalization_marker = _validate_finalization_marker(
            attempt,
            task_id=binding.task_id,
            attempt_id=binding.attempt_id,
            task_inputs_sha256=binding.task_inputs_sha256,
        )
        _validate_finalization_source_unchanged(
            attempt,
            worktree,
            finalization_marker,
        )
        snapshot = load_json(attempt / FINALIZATION_SNAPSHOT_REF)
        frozen_entries_sha256 = str(snapshot["entries_sha256"])
    source_before = _semantic_worktree_entries(worktree)
    source_before_sha256 = canonical_digest(source_before)
    cwd = _command_cwd(worktree, definition["cwd"])
    record_id = f"C-{uuid.uuid4().hex}"
    command_dir = attempt / "runtime" / "commands"
    command_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = command_dir / f"{record_id}.stdout.log"
    stderr_path = command_dir / f"{record_id}.stderr.log"
    started_at_epoch = time.time()
    started_at = utc_now()
    try:
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            try:
                broker = broker_directory_for_attempt(attempt)
            except RuntimeError as exc:
                raise SystemExit(str(exc)) from exc
            if broker is not None:
                result = run_brokered(
                    broker,
                    attempt_id=binding.attempt_id,
                    task_id=binding.task_id,
                    task_inputs_sha256=binding.task_inputs_sha256,
                    check_id=str(definition["id"]),
                    argv=definition["argv"],
                    timeout_seconds=float(definition["timeout_seconds"]),
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                )
            else:
                result = run_supervised(
                    definition["argv"],
                    timeout_seconds=float(definition["timeout_seconds"]),
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                )
        exit_code = result.exit_code
        timed_out = result.timed_out
        elapsed_seconds = result.elapsed_seconds
        survivors = list(result.surviving_pids)
        cleanup_verified = result.cleanup_verified
        cleanup_failure_reason = result.cleanup_failure_reason
    except OSError as exc:
        stderr_path.write_text(f"command could not start: {exc}\n", encoding="utf-8")
        stdout_path.touch(exist_ok=True)
        exit_code = 127
        timed_out = False
        elapsed_seconds = 0.0
        survivors = []
        cleanup_verified = True
        cleanup_failure_reason = None

    source_after = _semantic_worktree_entries(worktree)
    source_after_sha256 = canonical_digest(source_after)
    source_unchanged = source_before == source_after
    if finalization_marker is not None:
        source_unchanged = bool(
            source_unchanged
            and source_before_sha256 == frozen_entries_sha256
            and source_after_sha256 == frozen_entries_sha256
        )
        if not source_unchanged:
            with stderr_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    "source tree changed while a finalize-only acceptance check ran\n"
                )
            exit_code = 126

    finished_at_epoch = time.time()
    record: dict[str, Any] = {
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "schema_version": ARTIFACT_PROTOCOL_VERSION,
        "record_id": record_id,
        "task_id": binding.task_id,
        "attempt_id": binding.attempt_id,
        "task_inputs_sha256": binding.task_inputs_sha256,
        "acceptance_contract_sha256": binding.task_inputs["inputs"]["acceptance"]["sha256"],
        "category": "required_commands",
        "check_id": definition["id"],
        "argv": list(definition["argv"]),
        "cwd": definition["cwd"],
        "timeout_seconds": definition["timeout_seconds"],
        "started_at": started_at,
        "started_at_epoch": started_at_epoch,
        "finished_at": utc_now(),
        "finished_at_epoch": finished_at_epoch,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_seconds": elapsed_seconds,
        "surviving_processes": survivors,
        "cleanup_verified": cleanup_verified,
        "cleanup_failure_reason": cleanup_failure_reason,
        "stdout_ref": stdout_path.relative_to(attempt).as_posix(),
        "stdout_sha256": file_sha256(stdout_path),
        "stderr_ref": stderr_path.relative_to(attempt).as_posix(),
        "stderr_sha256": file_sha256(stderr_path),
        "source_before_entries_sha256": source_before_sha256,
        "source_after_entries_sha256": source_after_sha256,
        "source_unchanged": source_unchanged,
    }
    if finalization_marker is not None:
        record["finalization_started_at_epoch"] = finalization_marker[
            "started_at_epoch"
        ]
        record["source_snapshot_entries_sha256"] = frozen_entries_sha256
    if getattr(args, "workflow_id", ""):
        record["workflow_id"] = args.workflow_id
    if getattr(args, "instance_id", ""):
        record["instance_id"] = args.instance_id
    record["record_sha256"] = command_record_sha256(record)
    _append_command_record(attempt / "runtime" / "COMMANDS.ndjson", record)
    print(json.dumps(record, indent=2))
    return int(exit_code)


def check_command(args: argparse.Namespace) -> int:
    with attempt_artifact_lock(args.attempt_dir, exclusive=False):
        return _check_command_locked(args)


def run_dir(path: Path) -> Path:
    return path.parent.parent


def event(path: Path, name: str, actor: str, **extra: Any) -> None:
    status = load_json(path / "STATUS.json")
    append_event(
        run_dir(path),
        {"at": utc_now(), "actor": actor, "event": name, "run_id": run_dir(path).name, "task_id": status["task_id"], **extra},
    )


def transition(path: Path, target: str, actor: str) -> None:
    status_path = path / "STATUS.json"
    status = load_json(status_path)
    source = status.get("state")
    fsm = load_json(SKILL_ROOT / "references" / "state-machine.json")
    if actor not in fsm.get("transitions", {}).get(source, {}).get(target, []):
        raise SystemExit(f"illegal transition: {source!r} -> {target!r} by {actor}")
    now = utc_now()
    status.update(previous_state=source, state=target, updated_at=now, owner=actor)
    status.setdefault("state_history", []).append({"from": source, "to": target, "actor": actor, "at": now})
    write_json(status_path, status)


def validate_transition(path: Path, target: str, actor: str) -> str:
    status = load_json(path / "STATUS.json")
    source = status.get("state")
    fsm = load_json(SKILL_ROOT / "references" / "state-machine.json")
    if actor not in fsm.get("transitions", {}).get(source, {}).get(target, []):
        raise SystemExit(f"illegal transition: {source!r} -> {target!r} by {actor}")
    return str(source)


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def derive_task_changed_files(task: Path, attempt_path: Path, cwd: Path) -> list[str]:
    before_paths = sorted((attempt_path.parent).glob("*/runtime/worktree-before.json"))
    if not before_paths:
        raise SystemExit("cannot derive task changes: no worktree-before fingerprint exists")
    before_payload = load_json(before_paths[0])
    before = {item["path"]: item["sha256"] for item in before_payload.get("entries", [])}
    after_payload = fingerprint(cwd)
    after = {item["path"]: item["sha256"] for item in after_payload.get("entries", [])}
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def require_clean_task_worktree(cwd: Path, expected_branch: str) -> None:
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=cwd, text=True, capture_output=True, check=False
    )
    if branch.returncode != 0 or branch.stdout.strip() != expected_branch:
        raise SystemExit(
            f"task worktree must be on assigned branch {expected_branch!r}, got {branch.stdout.strip()!r}"
        )
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode != 0:
        raise SystemExit(f"cannot inspect task worktree status: {status.stderr.strip()}")
    if status.stdout.strip():
        raise SystemExit("task worktree must be committed and clean before final handoff")


def require_clean_target_worktree(cwd: Path, expected_branch: str) -> None:
    branch = git_output(cwd, "branch", "--show-current")
    if branch != expected_branch:
        raise SystemExit(
            f"target worktree must be on run target branch {expected_branch!r}, got {branch!r}"
        )
    raw = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if raw.returncode != 0:
        raise SystemExit("cannot inspect target worktree status")
    dirty: list[str] = []
    for entry in raw.stdout.split(b"\0"):
        if not entry:
            continue
        text = entry.decode("utf-8", errors="replace")
        path = text[3:] if len(text) >= 4 else text
        if path.startswith(".agent-collab/") or path.startswith(".agent-worktrees/"):
            continue
        dirty.append(path)
    if dirty:
        raise SystemExit(f"target worktree has non-RDO changes: {sorted(dirty)}")


def git_output(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SystemExit(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def resolve_worktree(root: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise SystemExit(f"{label} is missing")
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not resolved.is_dir():
        raise SystemExit(f"{label} does not exist: {resolved}")
    return resolved


def require_same_repository(root: Path, worktree: Path) -> None:
    root_common_raw = Path(git_output(root, "rev-parse", "--git-common-dir"))
    worktree_common_raw = Path(git_output(worktree, "rev-parse", "--git-common-dir"))
    root_common = (
        root_common_raw.resolve()
        if root_common_raw.is_absolute()
        else (root / root_common_raw).resolve()
    )
    worktree_common = (
        worktree_common_raw.resolve()
        if worktree_common_raw.is_absolute()
        else (worktree / worktree_common_raw).resolve()
    )
    if root_common != worktree_common:
        raise SystemExit("target and task worktrees must belong to the same Git repository")


def current_task_review(path: Path) -> dict[str, Any]:
    pointer_path = path / "reviews" / "CURRENT_TASK_REVIEW.json"
    if not pointer_path.exists():
        raise SystemExit("approved task is missing CURRENT_TASK_REVIEW.json")
    pointer = load_json(pointer_path)
    revision = pointer.get("revision") if isinstance(pointer, dict) else None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise SystemExit("CURRENT_TASK_REVIEW.json revision must be a positive integer")
    relative = pointer.get("decision_path") if isinstance(pointer, dict) else None
    if not isinstance(relative, str) or not relative:
        raise SystemExit("CURRENT_TASK_REVIEW.json has no decision_path")
    decision_path = (path / relative).resolve()
    try:
        decision_path.relative_to((path / "reviews").resolve())
    except ValueError as exc:
        raise SystemExit("task review decision must be inside the reviews directory") from exc
    decision = load_json(decision_path)
    declared_digest = pointer.get("decision_sha256") if isinstance(pointer, dict) else None
    status = load_json(path / "STATUS.json")
    if decision.get("revision") != revision:
        raise SystemExit("CURRENT_TASK_REVIEW.json revision does not match the decision")
    if relative != f"reviews/DECISION-v{revision:03d}.json":
        raise SystemExit("CURRENT_TASK_REVIEW.json decision_path does not match its revision")
    if task_protocol(path, status) == ARTIFACT_PROTOCOL_VERSION and (
        not isinstance(declared_digest, str)
        or len(declared_digest) != 64
        or any(character not in "0123456789abcdef" for character in declared_digest)
    ):
        raise SystemExit("CURRENT_TASK_REVIEW.json requires a lowercase decision_sha256")
    if task_protocol(path, status) == ARTIFACT_PROTOCOL_VERSION and (
        decision.get("schema_version") != ARTIFACT_PROTOCOL_VERSION
        or decision.get("artifact_protocol_version") != ARTIFACT_PROTOCOL_VERSION
    ):
        raise SystemExit("current task review decision is not Artifact Protocol v2")
    if declared_digest is not None and declared_digest != file_sha256(decision_path):
        raise SystemExit("CURRENT_TASK_REVIEW.json decision digest does not match the decision")
    if decision.get("decision") != "approved":
        raise SystemExit("current task review decision is not approved")
    if decision.get("task_id") != status.get("task_id"):
        raise SystemExit("task review decision task_id does not match STATUS.json")
    return decision


def approval_git_binding(path: Path, status: dict[str, Any]) -> dict[str, Any]:
    root = repo_root(path)
    task_worktree = resolve_worktree(root, status.get("worktree"), label="task worktree")
    require_same_repository(root, task_worktree)
    source_branch = str(status.get("branch") or "")
    require_clean_task_worktree(task_worktree, source_branch)
    run = load_json(run_dir(path) / "RUN.json")
    target_branch = run.get("target_branch")
    if not isinstance(target_branch, str) or not target_branch:
        raise SystemExit("RUN.json target_branch is missing")
    approved_commit = git_output(task_worktree, "rev-parse", "HEAD")
    target_commit = git_output(root, "rev-parse", target_branch)
    target_is_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", target_commit, approved_commit],
        cwd=root,
        check=False,
    ).returncode == 0
    source_is_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", approved_commit, target_commit],
        cwd=root,
        check=False,
    ).returncode == 0
    if not target_is_ancestor and not source_is_ancestor:
        raise SystemExit(
            "task commit is neither fast-forward mergeable nor already contained in the target branch"
        )
    if task_protocol(path, status) == ARTIFACT_PROTOCOL_VERSION:
        attempt_id = status.get("current_attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise SystemExit("v2 task review requires a current attempt")
        try:
            bundle = load_bundle(
                path / "attempts" / attempt_id,
                expected_task_id=str(status.get("task_id")),
                expected_attempt_id=attempt_id,
                expected_requested_state="review",
                expected_source_commit=approved_commit,
            )
        except ArtifactBundleError as exc:
            raise SystemExit(f"v2 review bundle is invalid: {exc}") from exc
        contract = _validate_frozen_sources(
            path / "attempts" / attempt_id,
            path,
            bundle.task_inputs_binding,
        )
        _validate_bundle_required_outputs(
            task_worktree,
            approved_commit,
            contract,
            bundle.evidence,
        )
        return {
            "approved_commit": approved_commit,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "target_commit_at_review": target_commit,
            "artifact_binding": artifact_binding(bundle),
        }
    return {
        "approved_commit": approved_commit,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "target_commit_at_review": target_commit,
        "evidence_sha256": hashlib.sha256((path / "EVIDENCE.md").read_bytes()).hexdigest(),
        "handoff_sha256": hashlib.sha256((path / "HANDOFF.json").read_bytes()).hexdigest(),
    }


STRATEGY_DRAFT_REF = "runtime/STRATEGY_DRAFT.json"


def _strategy_planning_context(
    attempt_dir: str | Path,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    attempt, task, status, metadata, _binding = _require_attempt_ownership(
        attempt_dir,
        allow_ready=False,
    )
    if status.get("profile") != "full":
        raise SystemExit("strategy authoring requires profile='full'")
    if status.get("state") != "planning" or metadata.get("phase") != "planning":
        raise SystemExit("strategy authoring requires the active planning attempt")
    backend_id = metadata.get("backend_id")
    if not isinstance(backend_id, str) or not backend_id:
        raise SystemExit("planning attempt backend_id is missing")
    return attempt, task, status, metadata


def _load_strategy_candidate(value: str, *, label: str) -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin) if value == "-" else load_json(Path(value))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise StrategyValidationError(f"cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StrategyValidationError(f"{label} must contain a JSON object")
    return payload


def _write_strategy_draft(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _strategy_draft_path(attempt: Path) -> Path:
    path = attempt / STRATEGY_DRAFT_REF
    if path.is_symlink():
        raise StrategyValidationError("attempt-local strategy draft must not be a symlink")
    return path


@contextmanager
def _strategy_draft_lock(attempt: Path, *, exclusive: bool):
    descriptor = os.open(attempt.parent.parent / "LOCK", os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _preflight_result(task: Path, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        return 0, preflight_strategy(task, payload)
    except StrategyValidationError as exc:
        return 2, {"valid": False, "errors": [str(exc)]}


def strategy_scaffold(args: argparse.Namespace) -> int:
    _attempt, task, _status, metadata = _strategy_planning_context(args.attempt_dir)
    scaffold = build_strategy_scaffold(task, str(metadata["backend_id"]))
    print(json.dumps(scaffold, ensure_ascii=False, indent=2))
    return 0


def strategy_preflight(args: argparse.Namespace) -> int:
    attempt, task, _status, _metadata = _strategy_planning_context(args.attempt_dir)
    source = STRATEGY_DRAFT_REF if args.draft else args.file
    candidate_path = _strategy_draft_path(attempt) if args.draft else None
    if candidate_path is not None:
        with _strategy_draft_lock(attempt, exclusive=False):
            payload = _load_strategy_candidate(
                str(candidate_path),
                label="strategy draft",
            )
            code, result = _preflight_result(task, payload)
    else:
        payload = _load_strategy_candidate(str(args.file), label="strategy candidate")
        code, result = _preflight_result(task, payload)
    result["source"] = source
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


def strategy_draft(args: argparse.Namespace) -> int:
    attempt, task, _status, _metadata = _strategy_planning_context(args.attempt_dir)
    payload = _load_strategy_candidate(args.file, label="strategy candidate")
    draft_path = _strategy_draft_path(attempt)
    with _strategy_draft_lock(attempt, exclusive=True):
        code, result = _preflight_result(task, payload)
        if code != 0:
            draft_path.unlink(missing_ok=True)
            result["published"] = False
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return code
        _write_strategy_draft(draft_path, payload)
        draft_file_sha256 = file_sha256(draft_path)
    result.update(
        published=True,
        draft_ref=STRATEGY_DRAFT_REF,
        draft_file_sha256=draft_file_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _strategy_submission_payload(
    task: Path,
    status: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    if not getattr(args, "draft", False):
        return _load_strategy_candidate(args.file, label="strategy candidate")
    attempt_id = status.get("current_attempt_id")
    if not isinstance(attempt_id, str) or Path(attempt_id).name != attempt_id:
        raise SystemExit("strategy draft requires a safe current attempt id")
    attempt, draft_task, _status, _metadata = _strategy_planning_context(
        task / "attempts" / attempt_id
    )
    if draft_task != task:
        raise SystemExit("strategy draft belongs to a different task")
    with _strategy_draft_lock(attempt, exclusive=False):
        return _load_strategy_candidate(
            str(_strategy_draft_path(attempt)),
            label="attempt-local strategy draft",
        )


def strategy_submit(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    protocol_version = task_protocol(path, status)
    profile = status.get("profile")
    if not _strategy_profile_is_full(protocol_version, profile):
        raise SystemExit("strategy submission requires profile='full'")
    if status.get("state") not in {"planning", "running"}:
        raise SystemExit("strategy submission requires planning or running state")
    attempt_id = status.get("current_attempt_id")
    attempt = load_json(path / "attempts" / str(attempt_id) / "ATTEMPT.json")
    expected_phase = "planning" if status["state"] == "planning" else "execution"
    if attempt.get("phase") != expected_phase or attempt.get("state") not in {"created", "running"}:
        raise SystemExit("strategy submission requires the current active attempt")
    payload = _strategy_submission_payload(path, status, args)
    if args.strategy_action == "submit" and payload.get("revision") != 1:
        raise SystemExit("strategy submit is only for revision 1; use strategy revise")
    if args.strategy_action == "revise" and (
        not isinstance(payload.get("revision"), int) or payload["revision"] <= 1
    ):
        raise SystemExit("strategy revise requires revision > 1")
    existing_output = path / "strategy" / f"STRATEGY-v{int(payload.get('revision', 0)):03d}.json"
    if protocol_version == ARTIFACT_PROTOCOL_VERSION and existing_output.is_file():
        existing_payload = load_json(existing_output)
        if existing_payload != payload:
            raise SystemExit(f"immutable strategy revision already exists with different content: {existing_output}")
        output, digest = existing_output, canonical_digest(existing_payload)
    else:
        output, digest = submit_strategy(path, payload)
    summary = f"Submitted strategy {payload['strategy_id']} for coordinator review"
    if protocol_version == ARTIFACT_PROTOCOL_VERSION:
        attempt_path, _task, _status, metadata, binding = _require_attempt_ownership(
            path / "attempts" / str(attempt_id),
            allow_ready=True,
        )
        if metadata.get("phase") != expected_phase:
            raise SystemExit("strategy submission attempt phase does not match task state")
        worktree = _worktree_for_attempt(metadata)
        require_clean_task_worktree(worktree, str(status.get("branch") or ""))
        source_commit = git_output(worktree, "rev-parse", "HEAD")
        task_base_commit = str(binding.task_inputs["task_base_commit"])
        if expected_phase == "planning" and source_commit != task_base_commit:
            raise SystemExit(
                "planning strategy submission requires HEAD to equal the frozen task base commit"
            )
        submission_ref = "runtime/STRATEGY_SUBMISSION.json"
        publish_json_once(
            attempt_path / submission_ref,
            {
                "schema_version": ARTIFACT_PROTOCOL_VERSION,
                "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
                "task_id": status["task_id"],
                "attempt_id": str(attempt_id),
                "strategy_revision": payload["revision"],
                "strategy_id": payload["strategy_id"],
                "strategy_ref": f"../../strategy/{output.name}",
                "strategy_sha256": digest,
            },
        )
        before_ref = "runtime/worktree-before.json"
        if not (attempt_path / before_ref).is_file():
            raise SystemExit("planning attempt is missing runtime/worktree-before.json")
        after_ref = "runtime/worktree-after.json"
        publish_json_once(attempt_path / after_ref, fingerprint(worktree))
        planning_changes = _snapshot_changed_paths(
            attempt_path / before_ref,
            attempt_path / after_ref,
        )
        if expected_phase == "planning" and planning_changes:
            raise SystemExit(f"planning attempt modified the task worktree: {planning_changes}")
        if expected_phase == "execution":
            strategy_changed_paths = _git_changed_paths(
                worktree,
                task_base_commit,
                source_commit,
            )
            _validate_changed_path_policy(
                path,
                "full",
                strategy_changed_paths,
                attempt=attempt_path,
                metadata=metadata,
            )
        else:
            strategy_changed_paths = []
        try:
            bundle = publish_bundle(
                attempt_path,
                requested_state="strategy_review",
                summary=summary,
                direct_self_review={
                    "performed": False,
                    "passed": False,
                    "summary": "",
                    "findings": [],
                },
                source_commit=source_commit,
                command_record_ids=(),
                changed_paths=strategy_changed_paths,
                worktree={"before": before_ref, "after": after_ref},
                artifact_refs=(submission_ref,),
                expected_task_id=str(status["task_id"]),
                expected_attempt_id=str(attempt_id),
            )
        except ArtifactBundleError as exc:
            raise SystemExit(f"cannot publish strategy handoff: {exc}") from exc
        event(path, "strategy_submitted", "worker", strategy_id=payload["strategy_id"], revision=payload["revision"], strategy_sha256=digest)
        print(
            json.dumps(
                {
                    "path": str(output),
                    "strategy_sha256": digest,
                    "artifact_binding": artifact_binding(bundle),
                }
            )
        )
        return 0
    atomic_text(path / "EVIDENCE.md", f"# Evidence\n\nValidated `{output.name}` with SHA-256 `{digest}`.\n")
    atomic_text(path / "HANDOFF.md", f"# Strategy Handoff\n\n{summary}.\n")
    write_json(
        path / "HANDOFF.json",
        {
            "_template": False,
            "requested_state": "strategy_review",
            "summary": summary,
            "commands_run": [],
            "files_changed": [],
            "known_limitations": [],
            "needs_coordinator": True,
            "blocker_type": "",
            "blocking_reason": "",
            "strategy_revision": payload["revision"],
            "strategy_sha256": digest,
        },
    )
    event(path, "strategy_submitted", "worker", strategy_id=payload["strategy_id"], revision=payload["revision"], strategy_sha256=digest)
    write_completion(
        path,
        attempt_id=str(attempt_id),
        phase=expected_phase,
        requested_state="strategy_review",
        strategy_sha256=digest,
    )
    print(json.dumps({"path": str(output), "strategy_sha256": digest}))
    return 0


def strategy_review(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    protocol_version = task_protocol(path, status)
    profile = status.get("profile")
    if not _strategy_profile_is_full(protocol_version, profile):
        raise SystemExit("strategy review requires profile='full'")
    if (path / ".dispatch-lock").exists():
        raise SystemExit("strategy review is forbidden while a dispatch lock exists")
    if status.get("state") != "strategy_review":
        raise SystemExit("strategy review requires strategy_review state")
    submitted = load_json(path / "strategy" / f"STRATEGY-v{args.revision:03d}.json")
    digest = canonical_digest(submitted)
    if protocol_version == ARTIFACT_PROTOCOL_VERSION:
        attempt_id = status.get("current_attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise SystemExit("v2 strategy review requires a current attempt")
        try:
            bundle = require_current_bundle(
                path,
                status,
                expected_requested_state="strategy_review",
            )
        except ArtifactResolutionError as exc:
            raise SystemExit(f"strategy review bundle is invalid: {exc}") from exc
        submission_path = path / "attempts" / attempt_id / "runtime" / "STRATEGY_SUBMISSION.json"
        submission = load_json(submission_path)
        if (
            submission.get("strategy_revision") != args.revision
            or submission.get("strategy_sha256") != digest
            or submission.get("strategy_id") != submitted.get("strategy_id")
        ):
            raise SystemExit("strategy review revision does not match the frozen v2 handoff")
        artifacts = bundle.evidence.get("artifacts", [])
        if not any(
            isinstance(item, dict)
            and item.get("ref") == "runtime/STRATEGY_SUBMISSION.json"
            and item.get("sha256") == file_sha256(submission_path)
            for item in artifacts
        ):
            raise SystemExit("strategy review bundle does not bind STRATEGY_SUBMISSION.json")
    else:
        handoff = load_json(path / "HANDOFF.json")
        if handoff.get("strategy_revision") != args.revision or handoff.get("strategy_sha256") != digest:
            raise SystemExit("strategy review revision does not match the validated worker handoff")
    decision = "approved" if args.strategy_action == "approve" else "changes_requested"
    review = review_strategy(path, args.revision, decision=decision, reviewer=args.reviewer, notes=args.note)
    event(path, "strategy_reviewed", "coordinator", decision=decision, revision=args.revision, strategy_sha256=review["strategy_sha256"])
    if decision == "changes_requested":
        transition(path, "changes_requested", "coordinator")
    print(json.dumps(review))
    return 0


def task_review(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    if (path / ".dispatch-lock").exists():
        raise SystemExit("task review is forbidden while a dispatch lock exists")
    if status.get("state") != "review":
        raise SystemExit("task review requires review state")

    protocol_version = task_protocol(path, status)
    reviewed_bundle = None
    if protocol_version == ARTIFACT_PROTOCOL_VERSION:
        attempt_id = status.get("current_attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise SystemExit("v2 task review requires a current attempt")
        try:
            reviewed_bundle = require_current_bundle(
                path,
                status,
                expected_requested_state="review",
            )
        except ArtifactResolutionError as exc:
            raise SystemExit(f"v2 task review bundle is invalid: {exc}") from exc

    target_by_decision = {
        "approved": "approved",
        "changes_requested": "changes_requested",
        "failed": "failed",
    }
    event_by_decision = {
        "approved": "task_approved",
        "changes_requested": "changes_requested",
        "failed": "task_failed",
    }
    target = target_by_decision[args.decision]
    validate_transition(path, target, "coordinator")

    findings_path = Path(args.findings_file).resolve()
    try:
        findings_relative = findings_path.relative_to(path)
    except ValueError as exc:
        raise SystemExit("findings file must be inside the task directory") from exc
    if not findings_path.is_file():
        raise SystemExit(f"findings file does not exist: {findings_path}")
    findings = findings_path.read_text(encoding="utf-8")
    if not findings.strip():
        raise SystemExit("findings file must be non-empty")

    reviews = path / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    revision = len(list(reviews.glob("DECISION-v*.json"))) + 1
    decision_path = reviews / f"DECISION-v{revision:03d}.json"
    if decision_path.exists():
        raise SystemExit(f"refusing to overwrite task review decision: {decision_path}")
    payload = {
        "schema_version": ARTIFACT_PROTOCOL_VERSION if protocol_version == ARTIFACT_PROTOCOL_VERSION else 1,
        "task_id": load_json(path / "STATUS.json")["task_id"],
        "revision": revision,
        "decision": args.decision,
        "reviewer": args.reviewer,
        "reviewed_at": utc_now(),
        "findings_path": findings_relative.as_posix(),
        "findings_sha256": hashlib.sha256(findings.encode("utf-8")).hexdigest(),
        "notes": args.note,
    }
    if protocol_version == ARTIFACT_PROTOCOL_VERSION:
        assert reviewed_bundle is not None
        payload["artifact_protocol_version"] = ARTIFACT_PROTOCOL_VERSION
        payload["artifact_binding"] = artifact_binding(reviewed_bundle)
    if args.decision == "approved":
        payload.update(approval_git_binding(path, status))
    if protocol_version == ARTIFACT_PROTOCOL_VERSION:
        publish_json_once(decision_path, payload)
    else:
        write_json(decision_path, payload)
    write_json(
        reviews / "CURRENT_TASK_REVIEW.json",
        {
            "revision": revision,
            "decision_path": decision_path.relative_to(path).as_posix(),
            **(
                {"decision_sha256": file_sha256(decision_path)}
                if protocol_version == ARTIFACT_PROTOCOL_VERSION
                else {}
            ),
        },
    )
    transition(path, target, "coordinator")
    event(
        path,
        "coordinator_reviewed",
        "coordinator",
        decision=args.decision,
        review_revision=revision,
        findings_path=findings_relative.as_posix(),
    )
    event(
        path,
        event_by_decision[args.decision],
        "coordinator",
        review_revision=revision,
        findings_path=findings_relative.as_posix(),
    )
    print(json.dumps(payload, indent=2))
    return 0


def task_revise(args: argparse.Namespace) -> int:
    """Record a changes-requested decision through the canonical review primitive."""

    return task_review(
        argparse.Namespace(
            task_dir=args.task_dir,
            decision="changes_requested",
            reviewer=args.reviewer,
            findings_file=args.findings_file,
            note=list(args.note),
        )
    )


def _task_dispatch_identity(
    path: Path,
    status: dict[str, Any],
) -> tuple[Path, str, str]:
    root = repo_root(path)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise SystemExit("task directory is outside its repository") from exc
    parts = relative.parts
    if (
        len(parts) != 5
        or parts[0] != ".agent-collab"
        or parts[1] != "runs"
        or parts[3] != "tasks"
    ):
        raise SystemExit(
            "task resume requires .agent-collab/runs/<run-id>/tasks/<task-id>"
        )
    run_id, task_id = parts[2], parts[4]
    if status.get("task_id") != task_id:
        raise SystemExit("STATUS.task_id does not match the task directory")
    return root, run_id, task_id


def _resume_result(
    path: Path,
    status_before: dict[str, Any],
    existing_attempt_ids: set[str],
    dispatch_exit_code: int,
    requested_mode: str,
) -> dict[str, Any]:
    previous_attempt_id = status_before.get("current_attempt_id")
    result: dict[str, Any] = {
        "schema_version": 1,
        "task_id": status_before.get("task_id"),
        "dispatch_exit_code": dispatch_exit_code,
        "previous_attempt_id": previous_attempt_id,
        "attempt_created": False,
        "attempt_id": None,
        "selection_source": None,
        "requested_execution_mode": requested_mode,
        "execution_mode": None,
        "resume_fallback_reason": None,
        "backend_id": None,
        "runtime_backend": None,
        "phase": None,
        "attempt_state": None,
        "attempt_outcome": None,
        "worker_exit_code": None,
    }
    attempts_dir = path / "attempts"
    observed_ids = sorted(
        candidate.name
        for candidate in attempts_dir.iterdir()
        if not candidate.is_symlink()
        and candidate.is_dir()
        and candidate.name not in existing_attempt_ids
        and candidate.name not in {"", ".", ".."}
        and Path(candidate.name).name == candidate.name
        and "/" not in candidate.name
        and "\\" not in candidate.name
    )
    matching: list[tuple[str, dict[str, Any]]] = []
    invalid: list[str] = []
    for attempt_id in observed_ids:
        try:
            attempt = load_json(attempts_dir / attempt_id / "ATTEMPT.json")
        except Exception as exc:
            invalid.append(f"{attempt_id}: unreadable ATTEMPT.json ({exc})")
            continue
        if not isinstance(attempt, dict):
            invalid.append(f"{attempt_id}: ATTEMPT.json is not an object")
            continue
        if (
            attempt.get("attempt_id") == attempt_id
            and attempt.get("task_id") == status_before.get("task_id")
            and attempt.get("parent_attempt_id") == previous_attempt_id
        ):
            matching.append((attempt_id, attempt))
    if len(matching) != 1:
        if observed_ids or dispatch_exit_code == 0:
            detail = f"; invalid={invalid}" if invalid else ""
            result["result_error"] = (
                "cannot uniquely attribute a new attempt to this dispatch: "
                f"observed={observed_ids}, matching={[item[0] for item in matching]}"
                f"{detail}"
            )
        return result
    attempt_id, attempt = matching[0]
    runtime = attempt.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    result.update(
        attempt_created=True,
        attempt_id=attempt_id,
        selection_source=f"attempts/{attempt_id}/ATTEMPT.json",
        requested_execution_mode=attempt.get("requested_execution_mode"),
        execution_mode=attempt.get("execution_mode"),
        resume_fallback_reason=attempt.get("resume_fallback_reason"),
        backend_id=attempt.get("backend_id"),
        runtime_backend=runtime.get("backend"),
        phase=attempt.get("phase"),
        attempt_state=attempt.get("state"),
        attempt_outcome=attempt.get("outcome"),
        worker_exit_code=attempt.get("exit_code"),
    )
    return result


def _run_task_dispatch(command: list[str], root: Path) -> int:
    completed = subprocess.run(
        command,
        cwd=root,
        check=False,
        stdout=sys.stderr,
    )
    return int(completed.returncode)


def task_resume(args: argparse.Namespace) -> int:
    """Continue one task through the canonical dispatch entrypoint."""

    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    if status.get("state") not in {"blocked", "changes_requested"}:
        raise SystemExit("task resume requires blocked or changes_requested state")
    if (path / ".dispatch-lock").exists():
        raise SystemExit("task resume is forbidden while a dispatch lock exists")
    previous_attempt_id = status.get("current_attempt_id")
    if (
        not isinstance(previous_attempt_id, str)
        or not previous_attempt_id
        or Path(previous_attempt_id).name != previous_attempt_id
        or "/" in previous_attempt_id
        or "\\" in previous_attempt_id
    ):
        raise SystemExit("task resume requires a safe current attempt id")
    root, run_id, task_id = _task_dispatch_identity(path, status)
    existing_attempt_ids = {
        candidate.name
        for candidate in (path / "attempts").iterdir()
        if not candidate.is_symlink() and candidate.is_dir()
    }
    command = [
        str(Path(__file__).resolve().parent / "dispatch_agent.sh"),
        run_id,
        task_id,
    ]
    option_values = (
        ("--worker", args.worker_backend),
        ("--runtime", args.runtime_backend),
        ("--io", args.io_mode),
        ("--permission", args.permission_mode),
        ("--agent-name", args.agent_name),
        ("--session-id", args.session_id),
        ("--worker-id", args.worker_id),
    )
    for option, value in option_values:
        if value:
            command.extend((option, value))
    if args.execution_mode != "auto":
        command.extend(("--execution-mode", args.execution_mode))
    if args.phase != "auto":
        command.extend(("--phase", args.phase))

    dispatch_exit_code = _run_task_dispatch(command, root)
    result = _resume_result(
        path,
        status,
        existing_attempt_ids,
        dispatch_exit_code,
        args.execution_mode,
    )
    print(json.dumps(result, indent=2))
    return dispatch_exit_code


def _preview_dispatch_phase(
    task_path: Path, status: dict[str, Any], requested_phase: str
) -> str:
    if requested_phase != "auto":
        phase = requested_phase
    else:
        profile = status.get("profile", "full")
        state = status.get("state")
        if profile != "full":
            if state not in {"pending", "blocked", "changes_requested"}:
                raise SystemExit(
                    f"cannot auto-detect {profile} preview phase from state {state}"
                )
            phase = "execution"
        elif state == "pending":
            phase = "planning"
        elif state == "strategy_review":
            phase = "execution"
        elif state in {"blocked", "changes_requested"}:
            try:
                load_approved_strategy(task_path)
            except StrategyValidationError:
                phase = "planning"
            else:
                phase = "execution"
        else:
            raise SystemExit(f"cannot auto-detect preview phase from state {state}")
    if status.get("profile", "full") != "full" and phase != "execution":
        raise SystemExit(
            f"task profile {status.get('profile')} does not use planning attempts"
        )
    return phase


def task_preview_prompt(args: argparse.Namespace) -> int:
    """Render the next prompt candidate without mutating task or repository state."""

    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    root = repo_root(path)
    worktree_value = status.get("worktree")
    if not isinstance(worktree_value, str) or not worktree_value:
        raise SystemExit("task worktree is missing")
    worktree_candidate = Path(worktree_value)
    worktree = (
        worktree_candidate.resolve()
        if worktree_candidate.is_absolute()
        else (root / worktree_candidate).resolve()
    )
    config_result = load_config(root)
    if config_result.errors:
        raise SystemExit("cannot preview prompt with invalid RDO configuration")
    assigned = status.get("assigned_worker")
    assigned = assigned if isinstance(assigned, dict) else {}
    worker_backend = args.worker_backend or config_result.config.worker_backend
    assigned_backend = assigned.get("backend_id")
    session_id = assigned.get("backend_session_id") or assigned.get("session_id")

    execution_mode = args.execution_mode
    if execution_mode == "auto":
        if assigned_backend == worker_backend and session_id:
            execution_mode = "resume"
            reason = "same_backend_session_present_preflight_not_run"
        elif assigned and assigned_backend != worker_backend:
            execution_mode = "replace"
            reason = "assigned_worker_uses_different_backend"
        else:
            execution_mode = "start"
            reason = "no_resumable_assigned_session"
    elif execution_mode == "resume":
        if assigned_backend != worker_backend or not session_id:
            raise SystemExit(
                "resume preview requires an assigned session for the selected backend"
            )
        reason = "explicit_resume_preflight_not_run"
    elif execution_mode == "replace":
        reason = "explicit_backend_replacement"
    else:
        reason = "explicit_new_session"

    phase = _preview_dispatch_phase(path, status, args.phase)
    strategy_path = ""
    if status.get("profile", "full") == "full" and phase == "execution":
        strategy, _ = load_approved_strategy(path)
        strategy_path = str(
            path / "strategy" / f"STRATEGY-v{int(strategy['revision']):03d}.json"
        )
    prompt_mode = "compact_resume" if execution_mode == "resume" else "full"
    preview_attempt = path / "attempts" / "A-PREVIEW"
    dependency_context_payload = None
    current_attempt_id = status.get("current_attempt_id")
    if (
        prompt_mode == "full"
        and isinstance(current_attempt_id, str)
        and current_attempt_id
    ):
        if Path(current_attempt_id).name != current_attempt_id:
            raise SystemExit("task current_attempt_id is unsafe")
        try:
            dependency_context_payload = load_bound_dependency_context(
                path / "attempts" / current_attempt_id
            )
        except DependencyContextError as exc:
            raise SystemExit(
                f"cannot preview prompt with invalid dependency context: {exc}"
            ) from exc
    prompt = render_worker_prompt(
        worktree_path=str(worktree),
        task_dir=path,
        status_path=path / "STATUS.json",
        attempt_dir=preview_attempt,
        worker_backend=worker_backend,
        agent_name=str(
            assigned.get("agent_name")
            if assigned_backend == worker_backend and assigned.get("agent_name")
            else config_result.config.worker_agent_name
        ),
        phase=phase,
        strategy_path=strategy_path,
        prompt_mode=prompt_mode,
        prompt_mode_reason=reason,
        dependency_context_payload=dependency_context_payload,
    )
    if args.body_only:
        print(prompt)
    else:
        encoded = prompt.encode("utf-8")
        print(
            json.dumps(
                {
                    "selection_stage": "preflight_candidate",
                    "byte_exact": False,
                    "execution_mode_candidate": execution_mode,
                    "prompt_mode": prompt_mode,
                    "reason": reason,
                    "worker_backend": worker_backend,
                    "phase": phase,
                    "dependency_projection": (
                        "not_used_compact_resume"
                        if prompt_mode == "compact_resume"
                        else (
                            "bound_current_attempt"
                            if dependency_context_payload is not None
                            else "not_bound_for_preview"
                        )
                    ),
                    "preview_attempt_dir": str(preview_attempt),
                    "prompt_sha256": hashlib.sha256(encoded).hexdigest(),
                    "prompt_bytes": len(encoded),
                    "prompt": prompt,
                },
                indent=2,
            )
        )
    return 0


def merge_source_commit(path: Path, status: dict[str, Any], root: Path) -> tuple[str, str, str]:
    source_branch = str(status.get("branch") or "")
    task_worktree = resolve_worktree(root, status.get("worktree"), label="task worktree")
    require_same_repository(root, task_worktree)
    require_clean_task_worktree(task_worktree, source_branch)
    source_head = git_output(task_worktree, "rev-parse", "HEAD")
    run = load_json(run_dir(path) / "RUN.json")
    target_branch = run.get("target_branch")
    if not isinstance(target_branch, str) or not target_branch:
        raise SystemExit("RUN.json target_branch is missing")

    profile = status.get("profile", "full")
    if task_protocol(path, status) == ARTIFACT_PROTOCOL_VERSION:
        attempt_id = status.get("current_attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise SystemExit("v2 merge requires a current attempt")
        attempt = path / "attempts" / attempt_id
        expected_state = "verified" if profile == "direct" else "review"
        try:
            bundle = load_bundle(
                attempt,
                expected_task_id=str(status.get("task_id")),
                expected_attempt_id=attempt_id,
                expected_requested_state=expected_state,
                expected_source_commit=source_head,
            )
        except ArtifactBundleError as exc:
            raise SystemExit(f"v2 merge bundle is invalid: {exc}") from exc
        metadata = load_json(attempt / "ATTEMPT.json")
        if (
            metadata.get("state") != "completed"
            or metadata.get("handoff_valid") is not True
            or metadata.get("handoff_state") != expected_state
        ):
            raise SystemExit("v2 task does not have a valid completed handoff attempt")
        if profile == "direct":
            if status.get("state") not in {"verified", "merged"}:
                raise SystemExit("Direct task merge requires verified state")
        else:
            if status.get("state") not in {"approved", "merged"}:
                raise SystemExit("Delegated/Full task merge requires coordinator approval")
            decision = current_task_review(path)
            expected_binding = decision.get("artifact_binding")
            if not isinstance(expected_binding, dict):
                raise SystemExit("approved v2 review is missing artifact_binding")
            try:
                validate_artifact_binding(
                    attempt,
                    expected_binding,
                    expected_task_id=str(status.get("task_id")),
                    expected_attempt_id=attempt_id,
                    expected_source_commit=source_head,
                )
            except ArtifactBundleError as exc:
                raise SystemExit(f"approved artifact binding is invalid: {exc}") from exc
            if (
                decision.get("approved_commit") != source_head
                or decision.get("source_branch") != source_branch
                or decision.get("target_branch") != target_branch
            ):
                raise SystemExit("approved v2 Git binding no longer matches task/run metadata")
        current_binding = artifact_binding(bundle)
        if current_binding.get("source_commit") != source_head:
            raise SystemExit("task branch HEAD changed after frozen v2 handoff")
        return source_head, source_branch, target_branch

    if status.get("state") in {"approved", "merged"} and profile != "direct":
        decision = current_task_review(path)
        required = {
            "approved_commit", "source_branch", "target_branch",
            "target_commit_at_review", "evidence_sha256", "handoff_sha256",
        }
        missing = sorted(field for field in required if not decision.get(field))
        if missing:
            raise SystemExit(f"approved task review is missing Git binding fields: {missing}")
        if decision["source_branch"] != source_branch or decision["target_branch"] != target_branch:
            raise SystemExit("approved task review branch binding no longer matches task/run metadata")
        if decision["approved_commit"] != source_head:
            raise SystemExit("task branch HEAD changed after coordinator approval")
        if decision["evidence_sha256"] != hashlib.sha256((path / "EVIDENCE.md").read_bytes()).hexdigest():
            raise SystemExit("EVIDENCE.md changed after coordinator approval")
        if decision["handoff_sha256"] != hashlib.sha256((path / "HANDOFF.json").read_bytes()).hexdigest():
            raise SystemExit("HANDOFF.json changed after coordinator approval")
        return source_head, source_branch, target_branch

    if profile != "direct" or status.get("state") not in {"verified", "merged"}:
        raise SystemExit("task merge requires an approved task or a verified Direct task")
    attempt_id = status.get("current_attempt_id")
    attempt = path / "attempts" / str(attempt_id)
    metadata = load_json(attempt / "ATTEMPT.json")
    if (
        metadata.get("state") != "completed"
        or metadata.get("handoff_valid") is not True
        or metadata.get("handoff_state") != "verified"
    ):
        raise SystemExit("verified Direct task does not have a valid completed attempt")
    verified_commit = metadata.get("verified_commit")
    if not isinstance(verified_commit, str) or not verified_commit:
        raise SystemExit("verified Direct task attempt is missing its verified Git commit")
    if verified_commit != source_head:
        raise SystemExit("Direct task branch HEAD changed after verified handoff")
    after_path = attempt / "runtime" / "worktree-after.json"
    if not after_path.exists():
        raise SystemExit("verified Direct task is missing worktree-after fingerprint")
    if load_json(after_path).get("sha256") != fingerprint(task_worktree).get("sha256"):
        raise SystemExit("Direct task worktree changed after verified handoff")
    return source_head, source_branch, target_branch


def existing_task_event(
    path: Path,
    event_name: str,
    commit: str | None = None,
) -> dict[str, Any] | None:
    task_id = load_json(path / "STATUS.json").get("task_id")
    matches: list[dict[str, Any]] = []
    try:
        records, _warning = read_event_journal(
            run_dir(path),
            tolerate_interrupted_tail=True,
        )
    except EventJournalError as exc:
        raise SystemExit(f"cannot read {event_name} events: {exc}") from exc
    for record in records:
        if (
            record.get("event") == event_name
            and record.get("task_id") == task_id
            and (commit is None or record.get("commit") == commit)
        ):
            matches.append(record)
    return matches[-1] if matches else None


def existing_task_merged_event(path: Path, commit: str | None = None) -> dict[str, Any] | None:
    return existing_task_event(path, "task_merged", commit)


def existing_task_merge_applied_event(
    path: Path,
    commit: str | None = None,
) -> dict[str, Any] | None:
    return existing_task_event(path, "task_merge_applied", commit)


def run_merge_verification(
    path: Path,
    target_worktree: Path,
    commands: list[str],
    timeout_seconds: float,
) -> dict[str, Any] | None:
    if not commands:
        return None
    if timeout_seconds <= 0:
        raise SystemExit("verification timeout must be positive")
    log_path = path / "logs" / "post-merge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    with log_path.open("a", encoding="utf-8") as log:
        for raw in commands:
            argv = shlex.split(raw)
            if not argv:
                raise SystemExit("post-merge verification command cannot be empty")
            log.write(f"\n[{utc_now()}] $ {shlex.join(argv)}\n")
            log.flush()
            try:
                completed = run_supervised(
                    argv,
                    timeout_seconds=timeout_seconds,
                    cwd=target_worktree,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    grace_seconds=0.5,
                )
                exit_code = completed.exit_code
                timed_out = completed.timed_out
                surviving_pids = list(completed.surviving_pids)
                elapsed_seconds = completed.elapsed_seconds
                if timed_out:
                    log.write(f"command timed out after {timeout_seconds:g} seconds\n")
                if surviving_pids:
                    log.write(f"command left surviving processes: {surviving_pids}\n")
            except OSError as exc:
                exit_code = 127
                timed_out = False
                surviving_pids = []
                elapsed_seconds = 0.0
                log.write(f"command could not start: {exc}\n")
            result = {
                "command": argv,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "elapsed_seconds": elapsed_seconds,
                "surviving_pids": surviving_pids,
            }
            results.append(result)
            if exit_code != 0 or surviving_pids:
                break
    return {
        "passed": all(
            item["exit_code"] == 0 and not item["surviving_pids"]
            for item in results
        ),
        "results": results,
        "log": log_path.relative_to(path).as_posix(),
    }


def run_canonical_merge_checks(
    path: Path,
    worktree: Path,
    definitions: list[dict[str, Any]],
    *,
    phase: str,
    attempt_id: str,
) -> dict[str, Any] | None:
    """Run v2 coordinator checks exactly as declared in ACCEPTANCE.md."""

    if not definitions:
        return None
    results: list[dict[str, Any]] = []
    logs_dir = path / "logs" / "merge" / attempt_id
    logs_dir.mkdir(parents=True, exist_ok=True)
    for definition in definitions:
        check_id = str(definition["id"])
        log_path = logs_dir / f"{phase}-{check_id}.log"
        cwd = _command_cwd(worktree, str(definition["cwd"]))
        try:
            with log_path.open("ab") as log:
                result = run_supervised(
                    list(definition["argv"]),
                    timeout_seconds=float(definition["timeout_seconds"]),
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    grace_seconds=0.5,
                )
            exit_code = result.exit_code
            timed_out = result.timed_out
            elapsed_seconds = result.elapsed_seconds
            surviving_processes = list(result.surviving_pids)
        except OSError as exc:
            with log_path.open("ab") as log:
                log.write(f"command could not start: {exc}\n".encode("utf-8"))
            exit_code = 127
            timed_out = False
            elapsed_seconds = 0.0
            surviving_processes = []
        record = {
            "check_id": check_id,
            "argv": list(definition["argv"]),
            "cwd": str(definition["cwd"]),
            "timeout_seconds": definition["timeout_seconds"],
            "exit_code": exit_code,
            "timed_out": timed_out,
            "elapsed_seconds": elapsed_seconds,
            "surviving_processes": surviving_processes,
            "log": log_path.relative_to(path).as_posix(),
            "log_sha256": file_sha256(log_path),
        }
        results.append(record)
        if exit_code != 0 or timed_out or surviving_processes:
            break
    return {
        "phase": phase,
        "passed": len(results) == len(definitions)
        and all(
            item["exit_code"] == 0
            and item["timed_out"] is False
            and not item["surviving_processes"]
            for item in results
        ),
        "results": results,
    }


def task_merge(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    protocol_version = task_protocol(path, status)
    if status.get("state") not in {"approved", "verified", "merged"}:
        raise SystemExit("task merge requires approved, verified, or already merged state")
    if (path / ".dispatch-lock").exists():
        raise SystemExit("task merge is forbidden while a dispatch lock exists")
    if protocol_version == ARTIFACT_PROTOCOL_VERSION and args.verify_command:
        raise SystemExit(
            "v2 task merge forbids free --verify-command values; use canonical pre/post-merge commands"
        )

    root = repo_root(path)
    run = load_json(run_dir(path) / "RUN.json")
    configured_target = run.get("target_branch")
    if not isinstance(configured_target, str) or not configured_target:
        raise SystemExit("RUN.json target_branch is missing")
    target_worktree = Path(args.target_worktree).resolve()
    if not target_worktree.is_dir():
        raise SystemExit(f"target worktree does not exist: {target_worktree}")
    require_same_repository(root, target_worktree)
    require_clean_target_worktree(target_worktree, configured_target)

    recorded = existing_task_merged_event(path)
    if recorded is not None:
        source_commit = recorded.get("commit")
        if not isinstance(source_commit, str) or not source_commit:
            raise SystemExit("task_merged event is missing commit")
        if recorded.get("target_branch") != configured_target:
            raise SystemExit("task_merged target branch does not match RUN.json")
        if protocol_version == ARTIFACT_PROTOCOL_VERSION:
            attempt_id = recorded.get("attempt_id")
            expected_binding = recorded.get("artifact_binding")
            if not isinstance(attempt_id, str) or not isinstance(expected_binding, dict):
                raise SystemExit("v2 task_merged event is missing its attempt/artifact binding")
            try:
                recorded_bundle = validate_artifact_binding(
                    path / "attempts" / attempt_id,
                    expected_binding,
                    expected_task_id=str(status.get("task_id")),
                    expected_attempt_id=attempt_id,
                    expected_source_commit=source_commit,
                )
            except ArtifactBundleError as exc:
                raise SystemExit(f"v2 task_merged artifact binding is invalid: {exc}") from exc
            if status.get("profile") != "direct":
                decision = current_task_review(path)
                if (
                    decision.get("artifact_binding") != expected_binding
                    or decision.get("approved_commit") != source_commit
                ):
                    raise SystemExit("task_merged event no longer matches coordinator approval")
            contract = _validate_frozen_sources(
                path / "attempts" / attempt_id,
                path,
                recorded_bundle.task_inputs_binding,
            )
            _validate_bundle_required_outputs(
                target_worktree,
                source_commit,
                contract,
                recorded_bundle.evidence,
                require_live_match=False,
            )
        if args.expected_commit:
            expected = git_output(root, "rev-parse", args.expected_commit)
            if expected != source_commit:
                raise SystemExit(
                    f"expected commit {expected} does not match merged commit {source_commit}"
                )
        target_head = git_output(target_worktree, "rev-parse", "HEAD")
        source_contained = subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_commit, target_head],
            cwd=target_worktree,
            check=False,
        ).returncode == 0
        if not source_contained:
            print(
                json.dumps(
                    {
                        "state": status.get("state"),
                        "merge_consistency": "inconsistent",
                        "error": "recorded task merge is no longer contained by the target branch",
                        "task_merged": recorded,
                        "target_head": target_head,
                    },
                    indent=2,
                )
            )
            return 1
        if status.get("state") != "merged":
            transition(path, "merged", "coordinator")
        print(json.dumps(recorded, indent=2))
        verification = recorded.get("verification")
        return 0 if not isinstance(verification, dict) or verification.get("passed") is not False else 1

    if status.get("state") == "merged" and protocol_version == ARTIFACT_PROTOCOL_VERSION:
        attempt_id = status.get("current_attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise SystemExit("merged v2 task is missing its current attempt")
        expected_state = "verified" if status.get("profile") == "direct" else "review"
        try:
            recovered_bundle = require_current_bundle(
                path,
                status,
                expected_requested_state=expected_state,
            )
        except ArtifactResolutionError as exc:
            raise SystemExit(f"cannot recover missing task_merged event: {exc}") from exc
        source_commit = recovered_bundle.handoff.get("source_commit")
        if not isinstance(source_commit, str) or not source_commit:
            raise SystemExit("recovered v2 handoff is missing source_commit")
        source_branch = str(status.get("branch") or "")
        if status.get("profile") != "direct":
            decision = current_task_review(path)
            expected_binding = artifact_binding(recovered_bundle)
            if (
                decision.get("artifact_binding") != expected_binding
                or decision.get("approved_commit") != source_commit
                or decision.get("source_branch") != source_branch
                or decision.get("target_branch") != configured_target
            ):
                raise SystemExit("merged recovery does not match coordinator approval")
        contract = _validate_frozen_sources(
            path / "attempts" / attempt_id,
            path,
            recovered_bundle.task_inputs_binding,
        )
        _validate_bundle_required_outputs(
            target_worktree,
            source_commit,
            contract,
            recovered_bundle.evidence,
            require_live_match=False,
        )
        if args.expected_commit:
            expected = git_output(root, "rev-parse", args.expected_commit)
            if expected != source_commit:
                raise SystemExit(
                    f"expected commit {expected} does not match merged commit {source_commit}"
                )
        target_head = git_output(target_worktree, "rev-parse", "HEAD")
        if subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_commit, target_head],
            cwd=target_worktree,
            check=False,
        ).returncode != 0:
            raise SystemExit("merged STATUS commit is not contained by the target branch")
        verification = {
            "passed": False,
            "recovered": True,
            "reason": "STATUS was merged but task_merged verification evidence was missing",
            "target_head_after_verification": target_head,
            "target_branch_unchanged": True,
            "source_commit_contained": True,
        }
        payload = {
            "commit": source_commit,
            "source_branch": source_branch,
            "target_branch": configured_target,
            "coordinator_id": args.coordinator,
            "attempt_id": attempt_id,
            "artifact_binding": artifact_binding(recovered_bundle),
            "verification": verification,
        }
        event(path, "task_merged", "coordinator", **payload)
        print(json.dumps({"task_id": status.get("task_id"), "state": "merged", **payload}, indent=2))
        return 1

    source_commit, source_branch, target_branch = merge_source_commit(path, status, root)
    if args.expected_commit:
        expected = git_output(root, "rev-parse", args.expected_commit)
        if expected != source_commit:
            raise SystemExit(
                f"expected commit {expected} does not match approved source commit {source_commit}"
            )
    v2_bundle = None
    v2_contract = None
    pre_merge_verification = None
    if protocol_version == ARTIFACT_PROTOCOL_VERSION:
        attempt_id = str(status.get("current_attempt_id"))
        try:
            v2_bundle = load_bundle(
                path / "attempts" / attempt_id,
                expected_task_id=str(status.get("task_id")),
                expected_attempt_id=attempt_id,
                expected_source_commit=source_commit,
            )
        except ArtifactBundleError as exc:
            raise SystemExit(f"v2 merge bundle is invalid: {exc}") from exc
        v2_contract = _validate_frozen_sources(
            path / "attempts" / attempt_id,
            path,
            v2_bundle.task_inputs_binding,
        )
        task_worktree = resolve_worktree(root, status.get("worktree"), label="task worktree")
        _validate_bundle_required_outputs(
            task_worktree,
            source_commit,
            v2_contract,
            v2_bundle.evidence,
        )
        pre_merge_verification = run_canonical_merge_checks(
            path,
            task_worktree,
            list(v2_contract.get("pre_merge_commands", [])),
            phase="pre-merge",
            attempt_id=attempt_id,
        )
        if pre_merge_verification is not None and not pre_merge_verification["passed"]:
            print(json.dumps({"state": status.get("state"), "verification": pre_merge_verification}, indent=2))
            return 1
        require_clean_task_worktree(task_worktree, source_branch)
        if git_output(task_worktree, "rev-parse", "HEAD") != source_commit:
            raise SystemExit("task branch changed while running pre-merge checks")

    target_head_before_merge = git_output(target_worktree, "rev-parse", "HEAD")
    applied = existing_task_merge_applied_event(path, source_commit)
    if applied is not None:
        if applied.get("source_branch") != source_branch:
            raise SystemExit("task_merge_applied source branch does not match the task")
        if applied.get("target_branch") != configured_target:
            raise SystemExit("task_merge_applied target branch does not match RUN.json")
        expected_merge_head = applied.get("target_head_after_merge")
        if not isinstance(expected_merge_head, str) or not expected_merge_head:
            raise SystemExit("task_merge_applied event is missing target_head_after_merge")
        if protocol_version == ARTIFACT_PROTOCOL_VERSION:
            assert v2_bundle is not None
            if (
                applied.get("attempt_id") != str(status.get("current_attempt_id"))
                or applied.get("artifact_binding") != artifact_binding(v2_bundle)
            ):
                raise SystemExit("task_merge_applied event no longer matches the approved artifacts")
    else:
        contains_source = subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_commit, target_head_before_merge],
            cwd=target_worktree,
            check=False,
        ).returncode == 0
        merge_mode = "already_contained" if contains_source else "fast_forward"
        if not contains_source:
            fast_forwardable = subprocess.run(
                ["git", "merge-base", "--is-ancestor", target_head_before_merge, source_commit],
                cwd=target_worktree,
                check=False,
            ).returncode == 0
            if not fast_forwardable:
                raise SystemExit("task commit cannot be fast-forward merged into the target branch")
            merge = subprocess.run(
                ["git", "merge", "--ff-only", source_commit],
                cwd=target_worktree,
                text=True,
                capture_output=True,
                check=False,
            )
            if merge.returncode != 0:
                detail = merge.stderr.strip() or merge.stdout.strip()
                raise SystemExit(f"git merge --ff-only failed: {detail}")
        expected_merge_head = git_output(target_worktree, "rev-parse", "HEAD")
        if subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_commit, expected_merge_head],
            cwd=target_worktree,
            check=False,
        ).returncode != 0:
            raise SystemExit("target branch does not contain the task commit after merge")
        application_payload: dict[str, Any] = {
            "commit": source_commit,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "target_head_before_merge": target_head_before_merge,
            "target_head_after_merge": expected_merge_head,
            "mode": merge_mode,
            "coordinator_id": args.coordinator,
        }
        if protocol_version == ARTIFACT_PROTOCOL_VERSION:
            assert v2_bundle is not None
            application_payload["attempt_id"] = str(status.get("current_attempt_id"))
            application_payload["artifact_binding"] = artifact_binding(v2_bundle)
        event(path, "task_merge_applied", "coordinator", **application_payload)
        applied = application_payload

    target_head_before_verification = git_output(target_worktree, "rev-parse", "HEAD")
    target_branch_before_verification = git_output(
        target_worktree,
        "branch",
        "--show-current",
    )
    source_contained_before_verification = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_commit, target_head_before_verification],
        cwd=target_worktree,
        check=False,
    ).returncode == 0
    pre_verification_integrity = bool(
        target_head_before_verification == expected_merge_head
        and target_branch_before_verification == configured_target
        and source_contained_before_verification
    )

    if protocol_version == ARTIFACT_PROTOCOL_VERSION:
        assert v2_contract is not None
        post_merge_verification = (
            run_canonical_merge_checks(
                path,
                target_worktree,
                list(v2_contract.get("post_merge_commands", [])),
                phase="post-merge",
                attempt_id=str(status.get("current_attempt_id")),
            )
            if pre_verification_integrity
            else None
        )
        target_clean = True
        try:
            require_clean_target_worktree(target_worktree, configured_target)
        except SystemExit:
            target_clean = False
        verification = {
            "passed": (
                pre_verification_integrity
                and (pre_merge_verification is None or pre_merge_verification["passed"])
                and (post_merge_verification is None or post_merge_verification["passed"])
                and target_clean
            ),
            "pre_merge": pre_merge_verification,
            "post_merge": post_merge_verification,
            "post_merge_skipped_reason": (
                None
                if pre_verification_integrity
                else "target_changed_after_merge_application"
            ),
            "target_clean": target_clean,
        }
    else:
        verification = (
            run_merge_verification(
                path,
                target_worktree,
                list(args.verify_command),
                float(args.verification_timeout),
            )
            if pre_verification_integrity
            else {
                "passed": False,
                "results": [],
                "log": None,
                "post_merge_skipped_reason": "target_changed_after_merge_application",
            }
        )
    target_head_after_verification = git_output(target_worktree, "rev-parse", "HEAD")
    target_branch_after_verification = git_output(
        target_worktree,
        "branch",
        "--show-current",
    )
    source_still_contained = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_commit, target_head_after_verification],
        cwd=target_worktree,
        check=False,
    ).returncode == 0
    target_head_unchanged = target_head_after_verification == expected_merge_head
    target_branch_unchanged = target_branch_after_verification == configured_target
    integrity_reasons: list[str] = []
    if target_head_before_verification != expected_merge_head:
        integrity_reasons.append("target_head_changed_after_merge_application")
    if target_branch_before_verification != configured_target:
        integrity_reasons.append("target_branch_changed_after_merge_application")
    if not source_contained_before_verification:
        integrity_reasons.append("source_commit_removed_after_merge_application")
    if not target_head_unchanged:
        integrity_reasons.append("target_head_changed_during_post_merge_verification")
    if not target_branch_unchanged:
        integrity_reasons.append("target_branch_changed_during_post_merge_verification")
    if not source_still_contained:
        integrity_reasons.append("source_commit_not_contained_after_post_merge_verification")
    target_integrity = "consistent" if not integrity_reasons else "inconsistent"
    if verification is not None:
        verification["merge_application_event"] = "task_merge_applied"
        verification["expected_target_head"] = expected_merge_head
        verification["target_head_before_verification"] = target_head_before_verification
        verification["target_head_after_verification"] = target_head_after_verification
        verification["target_head_unchanged"] = target_head_unchanged
        verification["target_branch_unchanged"] = target_branch_unchanged
        verification["source_commit_contained"] = source_still_contained
        verification["target_integrity"] = target_integrity
        verification["integrity_reasons"] = integrity_reasons
        verification["passed"] = bool(
            verification.get("passed")
            and pre_verification_integrity
            and target_head_unchanged
            and target_branch_unchanged
            and source_still_contained
        )
    if verification is not None and protocol_version != ARTIFACT_PROTOCOL_VERSION:
        updated = load_json(path / "STATUS.json")
        evidence = updated.setdefault("evidence", {})
        commands_run = evidence.setdefault("commands_run", [])
        for result in verification.get("results", []):
            rendered = shlex.join(result["command"])
            if rendered not in commands_run:
                commands_run.append(rendered)
        logs = evidence.setdefault("logs", [])
        verification_log = verification.get("log")
        if isinstance(verification_log, str) and verification_log not in logs:
            logs.append(verification_log)
        evidence["passed"] = verification["passed"]
        write_json(path / "STATUS.json", updated)

    payload: dict[str, Any] = {
        "commit": source_commit,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "coordinator_id": args.coordinator,
    }
    if protocol_version == ARTIFACT_PROTOCOL_VERSION:
        assert v2_bundle is not None
        payload["attempt_id"] = str(status.get("current_attempt_id"))
        payload["artifact_binding"] = artifact_binding(v2_bundle)
    if verification is not None:
        payload["verification"] = verification
    event(path, "task_merged", "coordinator", **payload)
    if status.get("state") != "merged":
        transition(path, "merged", "coordinator")
    result = {
        "task_id": status.get("task_id"),
        "state": "merged",
        **payload,
    }
    print(json.dumps(result, indent=2))
    return 0 if verification is None or verification["passed"] else 1


def workflow_events(attempt: Path) -> list[dict[str, Any]]:
    path = attempt / "runtime" / "WORKFLOWS.ndjson"
    return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def command_events(attempt: Path) -> list[dict[str, Any]]:
    path = attempt / "runtime" / "COMMANDS.ndjson"
    return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _record_log_matches(attempt: Path, payload: dict[str, Any], prefix: str) -> bool:
    ref = payload.get(f"{prefix}_ref")
    digest = payload.get(f"{prefix}_sha256")
    if not isinstance(ref, str) or not isinstance(digest, str):
        return False
    candidate = (attempt / ref).resolve()
    try:
        candidate.relative_to(attempt.resolve())
    except ValueError:
        return False
    return candidate.is_file() and not candidate.is_symlink() and file_sha256(candidate) == digest


def select_required_check_records(
    attempt: Path,
    contract: dict[str, Any],
    binding: Any,
    *,
    expected_source_entries_sha256: str | None = None,
) -> tuple[list[Any], list[str]]:
    """Select one exact successful record for every frozen required check."""

    try:
        records = load_command_records(attempt, required=False)
    except ArtifactBundleError as exc:
        return [], [f"structured command log is invalid: {exc}"]
    acceptance_sha256 = binding.task_inputs["inputs"]["acceptance"]["sha256"]
    marker_path = attempt / FINALIZATION_REF
    marker: dict[str, Any] | None = None
    if not marker_path.exists() and expected_source_entries_sha256 is None:
        return [], [
            "required acceptance checks are not bound to the final source tree"
        ]
    if marker_path.exists():
        try:
            marker = _validate_finalization_marker(
                attempt,
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                task_inputs_sha256=binding.task_inputs_sha256,
            )
            snapshot = load_json(attempt / FINALIZATION_SNAPSHOT_REF)
            frozen_entries_sha256 = snapshot["entries_sha256"]
        except (OSError, ValueError, SystemExit) as exc:
            return [], [f"finalization source binding is invalid: {exc}"]
    else:
        frozen_entries_sha256 = expected_source_entries_sha256
    selected: list[Any] = []
    reasons: list[str] = []
    for definition in contract.get("required_commands", []):
        matches = []
        for record in records:
            payload = record.payload
            if (
                payload.get("artifact_protocol_version") == ARTIFACT_PROTOCOL_VERSION
                and payload.get("task_id") == binding.task_id
                and payload.get("attempt_id") == binding.attempt_id
                and payload.get("task_inputs_sha256") == binding.task_inputs_sha256
                and payload.get("acceptance_contract_sha256") == acceptance_sha256
                and payload.get("category") == "required_commands"
                and payload.get("check_id") == definition["id"]
                and payload.get("argv") == definition["argv"]
                and payload.get("cwd") == definition["cwd"]
                and payload.get("timeout_seconds") == definition["timeout_seconds"]
                and payload.get("source_before_entries_sha256")
                == frozen_entries_sha256
                and payload.get("source_after_entries_sha256")
                == frozen_entries_sha256
                and payload.get("source_unchanged") is True
                and (
                    "finalization_started_at_epoch" not in payload
                    or marker is not None
                    and payload.get("finalization_started_at_epoch")
                    == marker["started_at_epoch"]
                )
                and (
                    "source_snapshot_entries_sha256" not in payload
                    or payload.get("source_snapshot_entries_sha256")
                    == frozen_entries_sha256
                )
            ):
                matches.append(record)
        passing = [
            record
            for record in matches
            if record.payload.get("exit_code") == 0
            and record.payload.get("timed_out") is False
            and record.payload.get("surviving_processes") == []
            and record.payload.get("cleanup_verified", True) is True
            and _record_log_matches(attempt, record.payload, "stdout")
            and _record_log_matches(attempt, record.payload, "stderr")
        ]
        if not passing:
            if matches:
                reasons.append(
                    f"required check {definition['id']!r} has no successful, fully-clean record"
                )
            else:
                reasons.append(
                    f"required check {definition['id']!r} has no exact record for the frozen acceptance contract"
                )
            continue
        selected.append(passing[-1])
    return selected, reasons


def completed_workflows(records: list[dict[str, Any]]) -> set[str]:
    return {
        str(record.get("workflow_id"))
        for record in records
        if record.get("event") in {"workflow_completed", "workflow_carried_forward"}
        and record.get("workflow_id")
    }


def completion_gate_reasons(
    attempt: Path,
    strategy: dict[str, Any],
    *,
    completing_workflow: str | None = None,
    include_acceptance: bool = True,
) -> list[str]:
    """Validate task-level execution gates, optionally before appending completion."""

    records = workflow_events(attempt)
    completed = completed_workflows(records)
    if completing_workflow:
        completed.add(completing_workflow)
    gate = strategy["completion_gate"]
    reasons: list[str] = []
    if gate["required_workflows_complete"]:
        missing = sorted(
            item["workflow_id"]
            for item in strategy["workflows"]
            if item["required"] and item["workflow_id"] not in completed
        )
        if missing:
            reasons.append(f"required workflows are incomplete: {missing}")
    if gate["acceptance_commands_pass"] and include_acceptance:
        task = attempt.parent.parent
        status_path = task / "STATUS.json"
        status = load_json(status_path) if status_path.exists() else None
        if (
            isinstance(status, dict)
            and task_protocol(task, status) == ARTIFACT_PROTOCOL_VERSION
        ):
            try:
                binding = validate_task_inputs_binding(
                    attempt,
                    expected_task_id=str(status.get("task_id")),
                    expected_attempt_id=attempt.name,
                )
                contract = _validate_frozen_sources(attempt, task, binding)
                _selected, check_reasons = select_required_check_records(
                    attempt,
                    contract,
                    binding,
                )
                reasons.extend(check_reasons)
            except (ArtifactBundleError, SystemExit) as exc:
                reasons.append(f"acceptance evidence is invalid: {exc}")
        else:
            acceptance = [item for item in command_events(attempt) if item.get("acceptance") is True]
            if not acceptance:
                reasons.append("acceptance command records are missing")
            elif any(item.get("exit_code") != 0 or item.get("timed_out") for item in acceptance):
                reasons.append("one or more acceptance commands failed or timed out")
    if not gate["optional_workflows_may_timeout"] and any(
        record.get("event") == "workflow_timed_out" for record in records
    ):
        reasons.append("workflow timeout is forbidden by the completion gate")
    return reasons


def _reviewer_event_identity(record: dict[str, Any]) -> str | None:
    identifier = record.get("session_id") or record.get("agent_id")
    return identifier if isinstance(identifier, str) and identifier else None


def _reviewer_lifecycle_receipt(
    attempt: Path,
    *,
    workflow_id: str,
    instance_id: str,
    workflow_started_at: str,
    reviewer_id: str,
    artifact: Path,
) -> dict[str, str]:
    """Bind reviewer evidence to one completed backend lifecycle interval."""

    workflow_start = parse_iso(workflow_started_at)
    if workflow_start is None:
        raise SystemExit("independent review workflow has an invalid start timestamp")
    events_path = attempt / "runtime" / "BACKEND_EVENTS.ndjson"
    if not events_path.is_file() or events_path.is_symlink():
        raise SystemExit("independent review backend lifecycle evidence is unavailable")
    active_start: dict[str, Any] | None = None
    completed_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"independent review lifecycle evidence is unreadable: {exc}") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"independent review lifecycle evidence is invalid: {exc}") from exc
        if not isinstance(record, dict) or _reviewer_event_identity(record) != reviewer_id:
            continue
        observed_at = parse_iso(record.get("at"))
        if observed_at is None:
            continue
        event_name = record.get("event")
        started = event_name in {
            "subagent_started",
            "subagent-start",
            "backend_agent_started",
        } and not (event_name == "subagent-start" and record.get("result") != "started")
        stopped = event_name in {
            "subagent_stopped",
            "subagent-stop",
            "backend_agent_stopped",
        } and not (event_name == "subagent-stop" and record.get("result") != "stopped")
        if started:
            active_start = record if observed_at >= workflow_start else None
        elif stopped and active_start is not None:
            started_at = parse_iso(active_start.get("at"))
            if started_at is not None and observed_at >= started_at:
                completed_pairs.append((active_start, record))
            active_start = None
    if not completed_pairs:
        raise SystemExit(
            f"reviewer {reviewer_id!r} has no completed lifecycle inside "
            f"workflow instance {instance_id!r}"
        )
    start_event, stop_event = completed_pairs[-1]
    start_at = parse_iso(start_event.get("at"))
    stop_at = parse_iso(stop_event.get("at"))
    assert start_at is not None and stop_at is not None
    modified_at = artifact.stat(follow_symlinks=False).st_mtime
    if modified_at < start_at.timestamp() - 1.0 or modified_at > stop_at.timestamp() + 1.0:
        raise SystemExit(
            f"review artifact for {reviewer_id!r} was not written during its "
            "observed reviewer lifecycle"
        )
    artifact_ref = artifact.relative_to(attempt.resolve()).as_posix()
    artifact_sha256 = file_sha256(artifact)
    receipt_key = hashlib.sha256(
        f"{workflow_id}\0{instance_id}\0{reviewer_id}".encode("utf-8")
    ).hexdigest()
    receipt_ref = f"runtime/reviewer-receipts/{receipt_key}.json"
    receipt_path = attempt / receipt_ref
    receipt = {
        "schema_version": 1,
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "receipt_type": "independent_review",
        "task_id": attempt.parent.parent.name,
        "attempt_id": attempt.name,
        "workflow_id": workflow_id,
        "instance_id": instance_id,
        "reviewer_id": reviewer_id,
        "reviewer_started_at": start_event["at"],
        "reviewer_start_event_sha256": canonical_digest(start_event),
        "reviewer_stopped_at": stop_event["at"],
        "reviewer_stop_event_sha256": canonical_digest(stop_event),
        "artifact_ref": artifact_ref,
        "artifact_sha256": artifact_sha256,
        "artifact_mtime_ns": artifact.stat(follow_symlinks=False).st_mtime_ns,
    }
    try:
        receipt_sha256 = publish_json_once(receipt_path, receipt)
    except ArtifactBundleError as exc:
        raise SystemExit(f"reviewer lifecycle receipt could not be published: {exc}") from exc
    return {
        "reviewer_id": reviewer_id,
        "artifact": str(artifact),
        "sha256": artifact_sha256,
        "workflow_id": workflow_id,
        "instance_id": instance_id,
        "receipt_ref": receipt_ref,
        "receipt_sha256": receipt_sha256,
    }


def independent_review_evidence(
    attempt: Path,
    definition: dict[str, Any],
    values: list[str],
    *,
    workflow_id: str,
    instance_id: str,
    workflow_started_at: str,
) -> list[dict[str, str]]:
    review = definition.get("review", {})
    if review.get("mode") != "independent":
        if values:
            raise SystemExit("--review-evidence is only valid for an independent review workflow")
        return []
    evidence: list[dict[str, str]] = []
    evidence_root = (attempt / "runtime" / "reviews").resolve()
    for value in values:
        reviewer, separator, raw_path = value.partition("=")
        if not separator or not reviewer or not raw_path:
            raise SystemExit("review evidence must use REVIEWER_ID=ARTIFACT_PATH")
        raw_artifact = Path(os.path.abspath(raw_path))
        raw_evidence_root = Path(os.path.abspath(attempt / "runtime" / "reviews"))
        if raw_evidence_root not in raw_artifact.parents:
            raise SystemExit(f"review artifact must be under {raw_evidence_root}")
        cursor = raw_artifact
        while cursor != raw_evidence_root:
            if cursor.is_symlink():
                raise SystemExit(f"review artifact path must not traverse symlinks: {raw_artifact}")
            cursor = cursor.parent
        artifact = raw_artifact.resolve()
        if evidence_root not in artifact.parents:
            raise SystemExit(f"review artifact must be under {evidence_root}")
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise SystemExit(f"review artifact is missing or empty: {artifact}")
        evidence.append(
            _reviewer_lifecycle_receipt(
                attempt,
                workflow_id=workflow_id,
                instance_id=instance_id,
                workflow_started_at=workflow_started_at,
                reviewer_id=reviewer,
                artifact=artifact,
            )
        )
    reviewer_ids = [item["reviewer_id"] for item in evidence]
    required = int(review["required_reviewers"])
    if len(set(reviewer_ids)) < required:
        raise SystemExit(f"independent review requires {required} distinct reviewer artifacts")
    return evidence


def active_execution_attempt(value: str) -> tuple[Path, Path, dict[str, Any]]:
    attempt = Path(value).resolve()
    task = attempt.parent.parent
    status = load_json(task / "STATUS.json")
    if status.get("state") != "running" or status.get("current_attempt_id") != attempt.name:
        raise SystemExit("command requires the current running execution attempt")
    metadata = load_json(attempt / "ATTEMPT.json")
    if metadata.get("phase") != "execution" or metadata.get("state") not in {"created", "running"}:
        raise SystemExit("attempt is not an active execution attempt")
    if (attempt / READY_REF).exists():
        raise SystemExit("handoff is already published; attempt evidence is frozen")
    strategy = _bound_strategy_for_attempt(task, attempt, metadata)
    return attempt, task, strategy


def _semantic_worktree_entries(worktree: Path) -> list[dict[str, Any]]:
    """Return index-independent source facts for the current worktree."""

    entries: list[dict[str, Any]] = []
    for item in fingerprint(worktree).get("entries", []):
        if not isinstance(item, dict) or item.get("kind") == "missing":
            continue
        entries.append(
            {
                "path": item.get("path"),
                "kind": item.get("kind"),
                "mode": item.get("mode"),
                "sha256": item.get("sha256"),
            }
        )
    return sorted(entries, key=lambda item: str(item.get("path")))


def _load_attempt_deadline(attempt: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = attempt / ATTEMPT_DEADLINE_REF
    if not path.exists():
        return None, None
    if path.is_symlink() or not path.is_file():
        raise SystemExit("attempt deadline is missing or unsafe")
    try:
        payload = load_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"attempt deadline is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("attempt deadline must be a JSON object")
    try:
        payload = validate_attempt_deadline_payload(payload)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return payload, file_sha256(path)


def _emit_deadline_notice(attempt: Path) -> None:
    """Emit a compact deterministic reminder on worker-visible command output."""

    try:
        deadline, _digest = _load_attempt_deadline(attempt)
    except SystemExit:
        return
    if deadline is None:
        return
    now = time.time()
    phase = "execution"
    active_deadline = float(deadline["execution_deadline_at_epoch"])
    code = "attempt_deadline_approaching"
    marker_path = attempt / FINALIZATION_REF
    if marker_path.is_file() and not marker_path.is_symlink():
        try:
            marker = load_json(marker_path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            marker = None
        if isinstance(marker, dict) and isinstance(
            marker.get("deadline_at_epoch"),
            (int, float),
        ):
            phase = "finalization"
            code = "finalization_grace_active"
            active_deadline = float(marker["deadline_at_epoch"])
    remaining = max(0.0, active_deadline - now)
    if (
        phase == "execution"
        and remaining > float(deadline["reminder_seconds"])
    ):
        return
    if (
        phase == "finalization"
        and remaining
        > float(deadline["finalization_grace_seconds"])
        + float(deadline["reminder_seconds"])
    ):
        return
    notice = {
        "code": code,
        "phase": phase,
        "remaining_seconds": round(remaining, 3),
        "required_action": (
            "freeze source and finalize or publish blocked"
            if phase == "execution"
            else "only checks, commit, handoff, or finalize are allowed"
        ),
    }
    print(
        "RDO_DEADLINE_NOTICE "
        + json.dumps(notice, sort_keys=True, separators=(",", ":")),
        file=sys.stderr,
    )


def _finalization_snapshot_payload(
    *,
    task_id: str,
    attempt_id: str,
    worktree: Path,
) -> dict[str, Any]:
    entries = _semantic_worktree_entries(worktree)
    return {
        "schema_version": 2,
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "entries_sha256": canonical_digest(entries),
        "file_count": len(entries),
        "entries": entries,
    }


def _validate_finalization_marker(
    attempt: Path,
    *,
    task_id: str,
    attempt_id: str,
    task_inputs_sha256: str,
) -> dict[str, Any]:
    marker_path = attempt / FINALIZATION_REF
    if marker_path.is_symlink() or not marker_path.is_file():
        raise SystemExit("finalization marker is missing or unsafe")
    try:
        marker_ctime = float(
            marker_path.stat(follow_symlinks=False).st_ctime
        )
    except OSError as exc:
        raise SystemExit(f"finalization marker cannot be statted: {exc}") from exc
    try:
        marker = load_json(marker_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"finalization marker is unreadable: {exc}") from exc
    expected = {
        "schema_version": 2,
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "stage": "finalizing",
        "task_id": task_id,
        "attempt_id": attempt_id,
        "task_inputs_sha256": task_inputs_sha256,
        "source_snapshot_ref": FINALIZATION_SNAPSHOT_REF,
    }
    for field, value in expected.items():
        if marker.get(field) != value:
            raise SystemExit(f"finalization marker {field} does not match the active attempt")
    grace = marker.get("grace_seconds")
    started = marker.get("started_at_epoch")
    marker_deadline = marker.get("deadline_at_epoch")
    if (
        not isinstance(grace, (int, float))
        or isinstance(grace, bool)
        or not math.isfinite(float(grace))
        or float(grace) <= 0
        or not isinstance(started, (int, float))
        or isinstance(started, bool)
        or not math.isfinite(float(started))
        or not isinstance(marker_deadline, (int, float))
        or isinstance(marker_deadline, bool)
        or not math.isfinite(float(marker_deadline))
    ):
        raise SystemExit("finalization marker deadline fields are invalid")
    snapshot_path = attempt / FINALIZATION_SNAPSHOT_REF
    digest = marker.get("source_snapshot_sha256")
    if (
        not isinstance(digest, str)
        or not snapshot_path.is_file()
        or snapshot_path.is_symlink()
        or file_sha256(snapshot_path) != digest
    ):
        raise SystemExit("finalization source snapshot binding is invalid")
    snapshot = load_json(snapshot_path)
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("task_id") != task_id
        or snapshot.get("attempt_id") != attempt_id
        or not isinstance(snapshot.get("entries"), list)
        or snapshot.get("entries_sha256")
        != canonical_digest(snapshot.get("entries"))
    ):
        raise SystemExit("finalization source snapshot is invalid")
    try:
        snapshot_ctime = float(
            snapshot_path.stat(follow_symlinks=False).st_ctime
        )
    except OSError as exc:
        raise SystemExit(
            f"finalization source snapshot cannot be statted: {exc}"
        ) from exc
    deadline_path = attempt / ATTEMPT_DEADLINE_REF
    if (
        marker.get("deadline_ref") != ATTEMPT_DEADLINE_REF
        or not isinstance(marker.get("deadline_sha256"), str)
        or not deadline_path.is_file()
        or deadline_path.is_symlink()
        or file_sha256(deadline_path) != marker.get("deadline_sha256")
    ):
        raise SystemExit("finalization deadline binding is invalid")
    deadline, _deadline_sha256 = _load_attempt_deadline(attempt)
    if deadline is None:
        raise SystemExit("finalization deadline is missing")
    attempt_started = float(deadline["started_at_epoch"])
    execution_deadline = float(deadline["execution_deadline_at_epoch"])
    expected_marker_deadline = execution_deadline + float(grace)
    started_iso = parse_iso(marker.get("started_at"))
    if (
        started_iso is None
        or abs(started_iso.timestamp() - float(started)) > 1.001
        or abs(float(started) - marker_ctime) > 1.001
        or marker_ctime < attempt_started - 1.001
        or snapshot_ctime < attempt_started - 1.001
        or snapshot_ctime > marker_ctime + 0.001
        or snapshot_ctime > execution_deadline + 0.001
        or marker_ctime > execution_deadline + 1e-6
        or marker_ctime > time.time() + 1.0
        or not math.isclose(
            float(marker_deadline),
            expected_marker_deadline,
            rel_tol=0,
            abs_tol=1e-6,
        )
    ):
        raise SystemExit("finalization marker is outside the bound attempt deadline")
    if time.time() > float(marker_deadline) + 1e-6:
        raise SystemExit("finalization grace deadline has expired")
    return marker


def _start_finalization_locked(
    attempt: Path,
    task: Path,
    status: dict[str, Any],
    metadata: dict[str, Any],
    binding: Any,
    *,
    require_deadline: bool,
    require_completion_gate: bool = True,
) -> dict[str, Any]:
    """Atomically freeze the source tree and enter one non-resettable grace."""

    marker_path = attempt / FINALIZATION_REF
    if marker_path.exists():
        return _validate_finalization_marker(
            attempt,
            task_id=str(status.get("task_id")),
            attempt_id=attempt.name,
            task_inputs_sha256=binding.task_inputs_sha256,
        )
    expected_state = "planning" if metadata.get("phase") == "planning" else "running"
    if status.get("state") != expected_state:
        raise SystemExit("finalization requires the current active attempt")
    profile = str(status.get("profile") or "full")
    if profile == "full" and require_completion_gate:
        strategy = _bound_strategy_for_attempt(task, attempt, metadata)
        reasons = completion_gate_reasons(
            attempt,
            strategy,
            include_acceptance=False,
        )
        if reasons:
            raise SystemExit("finalization entry gate failed: " + "; ".join(reasons))

    deadline, deadline_sha256 = _load_attempt_deadline(attempt)
    if require_deadline and deadline is None:
        raise SystemExit("finalization entry requires runtime/DEADLINE.json")
    if deadline is not None and time.time() > float(
        deadline["execution_deadline_at_epoch"]
    ):
        raise SystemExit("attempt execution deadline expired before finalization entry")
    grace_seconds = (
        float(deadline["finalization_grace_seconds"])
        if deadline is not None
        else DEFAULT_FINALIZATION_GRACE_SECONDS
    )
    worktree = _worktree_for_attempt(metadata)
    snapshot = _finalization_snapshot_payload(
        task_id=str(status.get("task_id")),
        attempt_id=attempt.name,
        worktree=worktree,
    )
    try:
        snapshot_sha256 = publish_json_once(
            attempt / FINALIZATION_SNAPSHOT_REF,
            snapshot,
        )
        started_at_epoch = time.time()
        if deadline is not None and started_at_epoch > float(
            deadline["execution_deadline_at_epoch"]
        ):
            raise SystemExit(
                "attempt execution deadline expired while freezing finalization entry"
            )
        marker = {
            "schema_version": 2,
            "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
            "stage": "finalizing",
            "task_id": status.get("task_id"),
            "attempt_id": attempt.name,
            "task_inputs_sha256": binding.task_inputs_sha256,
            "started_at": utc_now(),
            "started_at_epoch": started_at_epoch,
            "grace_seconds": grace_seconds,
            "deadline_at_epoch": (
                float(deadline["execution_deadline_at_epoch"]) + grace_seconds
                if deadline is not None
                else started_at_epoch + grace_seconds
            ),
            "source_snapshot_ref": FINALIZATION_SNAPSHOT_REF,
            "source_snapshot_sha256": snapshot_sha256,
            "deadline_ref": ATTEMPT_DEADLINE_REF if deadline is not None else None,
            "deadline_sha256": deadline_sha256,
            "allowed_actions": [
                "required rdo check records",
                "git commit",
                "handoff",
                "rdo finalize",
            ],
            "forbidden_actions": [
                "production file edits",
                "workflow activity",
                "rdo exec",
                "implementation expansion",
            ],
        }
        publish_json_once(marker_path, marker)
    except ArtifactBundleError as exc:
        raise SystemExit(f"cannot enter finalization: {exc}") from exc
    return _validate_finalization_marker(
        attempt,
        task_id=str(status.get("task_id")),
        attempt_id=attempt.name,
        task_inputs_sha256=binding.task_inputs_sha256,
    )


def _validate_finalization_source_unchanged(
    attempt: Path,
    worktree: Path,
    marker: dict[str, Any],
) -> None:
    snapshot = load_json(attempt / str(marker["source_snapshot_ref"]))
    expected = snapshot.get("entries")
    actual = _semantic_worktree_entries(worktree)
    if expected != actual:
        raise SystemExit(
            "task worktree changed after finalization started; "
            "start a new attempt for further implementation"
        )


def finalization_action(args: argparse.Namespace) -> int:
    with attempt_artifact_lock(args.attempt_dir, exclusive=True):
        attempt, task, status, metadata, binding = _require_attempt_ownership(
            args.attempt_dir,
            allow_ready=False,
        )
        marker = _start_finalization_locked(
            attempt,
            task,
            status,
            metadata,
            binding,
            require_deadline=True,
        )
        _emit_deadline_notice(attempt)
    print(json.dumps(marker, indent=2))
    return 0


def _workflow_action_locked(args: argparse.Namespace) -> int:
    attempt, task, strategy = active_execution_attempt(args.attempt_dir)
    _emit_deadline_notice(attempt)
    if (attempt / FINALIZATION_REF).exists():
        raise SystemExit("workflow activity is forbidden after finalization starts")
    definitions = {item["workflow_id"]: item for item in strategy["workflows"]}
    if args.workflow_id not in definitions:
        raise SystemExit(f"workflow is not approved: {args.workflow_id}")
    records = workflow_events(attempt)
    active: dict[str, dict[str, Any]] = {}
    completed: set[str] = set()
    seen: set[str] = set()
    for record in records:
        instance = record.get("instance_id")
        seen.add(instance)
        if record.get("event") == "workflow_started":
            active[instance] = record
        elif record.get("event") == "workflow_carried_forward":
            completed.add(str(record["workflow_id"]))
        elif record.get("event") in {"workflow_completed", "workflow_timed_out", "workflow_cancelled"}:
            previous = active.pop(instance, None)
            if previous and record.get("event") == "workflow_completed":
                completed.add(previous["workflow_id"])
    definition = definitions[args.workflow_id]
    timed_out = False
    if args.workflow_action == "start":
        if args.workflow_id in completed:
            raise SystemExit("workflow is already satisfied by completion or a carried-forward checkpoint")
        if args.instance_id in seen:
            raise SystemExit("workflow instance_id must be unique")
        missing = sorted(set(definition["depends_on"]) - completed)
        if missing:
            raise SystemExit(f"workflow dependencies are incomplete: {missing}")
        if len(active) >= strategy["global_budget"]["max_parallel_workflows"]:
            raise SystemExit("max_parallel_workflows exceeded")
        starts_for_workflow = sum(
            1 for record in records
            if record.get("event") == "workflow_started" and record.get("workflow_id") == args.workflow_id
        )
        if starts_for_workflow >= definition["budget"]["max_instances"]:
            raise SystemExit("workflow max_instances exceeded")
        total_starts = sum(1 for record in records if record.get("event") == "workflow_started")
        if total_starts >= strategy["global_budget"]["max_workflow_instances"]:
            raise SystemExit("global max_workflow_instances exceeded")
        name = "workflow_started"
    elif args.workflow_action == "heartbeat":
        if args.instance_id not in active:
            raise SystemExit("heartbeat requires an active workflow instance")
        name = "workflow_heartbeat"
    else:
        if args.instance_id not in active:
            raise SystemExit("completion requires an active workflow instance")
        reviews = []
        completed_after = completed | {args.workflow_id}
        required = {item["workflow_id"] for item in strategy["workflows"] if item["required"]}
        if required.issubset(completed_after):
            status_path = task / "STATUS.json"
            task_status = load_json(status_path) if status_path.exists() else None
            v2_execution = bool(
                isinstance(task_status, dict)
                and task_protocol(task, task_status) == ARTIFACT_PROTOCOL_VERSION
            )
            reasons = completion_gate_reasons(
                attempt,
                strategy,
                completing_workflow=args.workflow_id,
                include_acceptance=not v2_execution,
            )
            if reasons:
                raise SystemExit("workflow completion gate failed: " + "; ".join(reasons))
        name = "workflow_completed"
    if args.workflow_action != "start":
        started_at = parse_iso(active[args.instance_id].get("at"))
        if started_at is None:
            raise SystemExit("active workflow has an invalid start timestamp")
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        if elapsed > definition["budget"]["wall_seconds"]:
            timed_out = True
            name = "workflow_timed_out"
    if name == "workflow_completed":
        reviews = independent_review_evidence(
            attempt,
            definition,
            getattr(args, "review_evidence", []),
            workflow_id=args.workflow_id,
            instance_id=args.instance_id,
            workflow_started_at=str(active[args.instance_id].get("at")),
        )
    record = {"at": utc_now(), "event": name, "workflow_id": args.workflow_id, "instance_id": args.instance_id, "attempt_id": attempt.name}
    if name == "workflow_completed" and reviews:
        record["reviews"] = reviews
    runtime = attempt / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "WORKFLOWS.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    if name == "workflow_completed":
        completed_after = completed | {args.workflow_id}
        required = {item["workflow_id"] for item in strategy["workflows"] if item["required"]}
        if required.issubset(completed_after):
            status_path = task / "STATUS.json"
            status = load_json(status_path) if status_path.exists() else None
            if (
                isinstance(status, dict)
                and task_protocol(task, status) == ARTIFACT_PROTOCOL_VERSION
            ):
                owned_attempt, owned_task, status, metadata, binding = (
                    _require_attempt_ownership(
                        attempt,
                        allow_ready=False,
                    )
                )
                _start_finalization_locked(
                    owned_attempt,
                    owned_task,
                    status,
                    metadata,
                    binding,
                    require_deadline=True,
                    require_completion_gate=True,
                )
            else:
                marker = runtime / "FINALIZATION.json"
                if not marker.exists():
                    write_json(
                        marker,
                        {
                            "schema_version": 1,
                            "stage": "finalizing",
                            "attempt_id": attempt.name,
                            "started_at": utc_now(),
                            "deadline_seconds": DEFAULT_FINALIZATION_GRACE_SECONDS,
                        },
                    )
    event(task, name, "worker", **{key: value for key, value in record.items() if key not in {"at", "event"}})
    print(json.dumps(record))
    if timed_out and definition["on_timeout"] != "continue_without_result":
        raise SystemExit(f"workflow timed out; policy action is {definition['on_timeout']}")
    return 0


def workflow_action(args: argparse.Namespace) -> int:
    with attempt_artifact_lock(args.attempt_dir, exclusive=True):
        return _workflow_action_locked(args)


def _git_changed_paths(worktree: Path, base_commit: str, source_commit: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", "-z", base_commit, source_commit, "--"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"cannot derive changed paths from frozen task base: {detail}")
    return sorted(
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )


def _snapshot_changed_paths(before_path: Path, after_path: Path) -> list[str]:
    before_payload = load_json(before_path)
    after_payload = load_json(after_path)
    before = {
        item["path"]: (item.get("kind"), item.get("mode"), item.get("sha256"))
        for item in before_payload.get("entries", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    after = {
        item["path"]: (item.get("kind"), item.get("mode"), item.get("sha256"))
        for item in after_payload.get("entries", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _required_outputs_exist(worktree: Path, contract: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    root = worktree.resolve()
    for relative in contract.get("required_outputs", []):
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            missing.append(f"{relative} (escapes worktree)")
            continue
        if not candidate.exists():
            missing.append(relative)
    return missing


def _path_contains(parent: str, child: str) -> bool:
    normalized_parent = parent.replace("\\", "/").rstrip("/") or "."
    normalized_child = child.replace("\\", "/").rstrip("/") or "."
    return (
        normalized_parent == "."
        or normalized_child == normalized_parent
        or normalized_child.startswith(normalized_parent + "/")
    )


def _validate_changed_path_policy(
    task: Path,
    profile: str,
    changed_paths: list[str],
    *,
    attempt: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        policy = parse_execution_policy(
            (task / "EXECUTION_POLICY.json").read_bytes(),
            profile=profile,
        )
    except (OSError, TaskContractError) as exc:
        raise SystemExit(f"frozen execution policy is invalid: {exc}") from exc
    allowed = list(policy["allowed_paths"])
    if profile == "full":
        if attempt is None or metadata is None:
            raise SystemExit("Full path validation requires the attempt-frozen strategy binding")
        strategy = _bound_strategy_for_attempt(task, attempt, metadata)
        allowed = sorted(
            {
                path
                for workflow in strategy["workflows"]
                if workflow["executor"]["write_access"]
                for path in workflow["executor"]["allowed_paths"]
            }
        )
    forbidden = list(policy["forbidden_paths"])
    violations = [
        path
        for path in changed_paths
        if not any(_path_contains(root, path) for root in allowed)
        or any(_path_contains(root, path) for root in forbidden)
    ]
    if violations:
        raise SystemExit(
            "committed task diff violates the frozen write policy: "
            f"{sorted(violations)}"
        )


def _validate_bundle_required_outputs(
    worktree: Path,
    source_commit: str,
    contract: dict[str, Any],
    evidence: dict[str, Any],
    *,
    require_live_match: bool = True,
) -> None:
    try:
        validate_required_output_bindings(
            worktree,
            source_commit,
            evidence.get("required_outputs"),
            expected_paths=list(contract.get("required_outputs", [])),
            require_live_match=require_live_match,
        )
    except ArtifactBundleError as exc:
        raise SystemExit(f"required output binding is invalid: {exc}") from exc


def _reviewer_evidence_refs(attempt: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for workflow in workflow_events(attempt):
        reviews = workflow.get("reviews")
        if not isinstance(reviews, list):
            continue
        for review in reviews:
            if not isinstance(review, dict):
                continue
            reviewer_id = review.get("reviewer_id")
            artifact = review.get("artifact")
            if not isinstance(reviewer_id, str) or not isinstance(artifact, str):
                continue
            resolved = Path(artifact).resolve()
            try:
                ref = resolved.relative_to(attempt.resolve()).as_posix()
            except ValueError as exc:
                raise SystemExit("reviewer evidence must be attempt-local") from exc
            declared_digest = review.get("sha256")
            if not isinstance(declared_digest, str) or file_sha256(resolved) != declared_digest:
                raise SystemExit("reviewer evidence changed after workflow completion")
            receipt_ref = review.get("receipt_ref")
            receipt_digest = review.get("receipt_sha256")
            workflow_id = review.get("workflow_id")
            instance_id = review.get("instance_id")
            if not all(
                isinstance(value, str) and value
                for value in (receipt_ref, receipt_digest, workflow_id, instance_id)
            ):
                raise SystemExit("reviewer evidence is missing its workflow lifecycle receipt")
            try:
                receipt_path = safe_ref(attempt, receipt_ref)
            except ArtifactBundleError as exc:
                raise SystemExit("reviewer lifecycle receipt must be a safe attempt-local ref") from exc
            if (
                not receipt_path.is_file()
                or file_sha256(receipt_path) != receipt_digest
            ):
                raise SystemExit("reviewer lifecycle receipt changed after workflow completion")
            receipt = load_json(receipt_path)
            if not isinstance(receipt, dict):
                raise SystemExit("reviewer lifecycle receipt must be a JSON object")
            expected_receipt = {
                "receipt_type": "independent_review",
                "task_id": attempt.parent.parent.name,
                "attempt_id": attempt.name,
                "workflow_id": workflow_id,
                "instance_id": instance_id,
                "reviewer_id": reviewer_id,
                "artifact_ref": ref,
                "artifact_sha256": declared_digest,
            }
            if any(receipt.get(field) != value for field, value in expected_receipt.items()):
                raise SystemExit("reviewer lifecycle receipt does not match workflow evidence")
            key = (reviewer_id, ref)
            if key not in seen:
                result.append(
                    {
                        "reviewer_id": reviewer_id,
                        "ref": ref,
                        "workflow_id": workflow_id,
                        "instance_id": instance_id,
                        "receipt_ref": receipt_ref,
                        "receipt_sha256": receipt_digest,
                    }
                )
                seen.add(key)
    return result


def _finalize_v2_locked(args: argparse.Namespace) -> int:
    attempt, task, status, metadata, binding = _require_attempt_ownership(
        args.attempt_dir,
        allow_ready=True,
    )
    _emit_deadline_notice(attempt)
    if args.state not in {"verified", "review", "blocked"}:
        raise SystemExit("finalize state must be verified, review, or blocked")
    profile = status.get("profile", "full")
    expected_terminal = {"direct": "verified", "delegated": "review", "full": "review"}.get(profile)
    if expected_terminal is None:
        raise SystemExit(f"unknown execution profile: {profile!r}")
    if args.state != "blocked" and args.state != expected_terminal:
        raise SystemExit(f"profile {profile!r} requires {expected_terminal!r} finalization")
    if args.state == "blocked" and (not args.blocker_type or not args.blocking_reason):
        raise SystemExit("blocked finalization requires blocker type and reason")
    expected_task_state = "planning" if metadata.get("phase") == "planning" else "running"
    if status.get("state") != expected_task_state:
        raise SystemExit(
            f"attempt phase {metadata.get('phase')!r} does not own task state {status.get('state')!r}"
        )
    if metadata.get("phase") != "execution" and args.state != "blocked":
        raise SystemExit("planning attempts may only publish a blocked finalization")

    summary = args.summary
    if getattr(args, "summary_file", ""):
        summary = Path(args.summary_file).read_text(encoding="utf-8").strip()
    if not isinstance(summary, str) or not summary.strip():
        raise SystemExit("finalize requires a non-empty summary or --summary-file")
    if getattr(args, "command", []):
        raise SystemExit("v2 finalize forbids free-text --command evidence; use rdo check")

    worktree = _worktree_for_attempt(metadata)
    contract = _validate_frozen_sources(attempt, task, binding)
    finalization_marker: dict[str, Any] | None = None
    selected: list[Any] = []
    required_output_bindings: list[dict[str, str]] = []
    source_commit: str | None
    if args.state == "blocked":
        finalization_marker = _start_finalization_locked(
            attempt,
            task,
            status,
            metadata,
            binding,
            require_deadline=True,
            require_completion_gate=False,
        )
        _validate_finalization_source_unchanged(
            attempt,
            worktree,
            finalization_marker,
        )
        try:
            source_commit = git_output(worktree, "rev-parse", "HEAD")
        except SystemExit:
            source_commit = None
    else:
        if status.get("state") != "running":
            raise SystemExit(f"{args.state} finalization requires running state")
        if not (attempt / FINALIZATION_REF).exists():
            # Compatibility path: do not freeze an attempt merely because
            # finalize was called too early.  First prove that the current
            # clean source tree already has matching acceptance records and
            # outputs; only then create the immutable marker.
            require_clean_task_worktree(
                worktree,
                str(status.get("branch") or ""),
            )
            prospective_commit = git_output(worktree, "rev-parse", "HEAD")
            prospective_entries_sha256 = canonical_digest(
                _semantic_worktree_entries(worktree)
            )
            _prospective_selected, prospective_reasons = (
                select_required_check_records(
                    attempt,
                    contract,
                    binding,
                    expected_source_entries_sha256=prospective_entries_sha256,
                )
            )
            if prospective_reasons:
                raise SystemExit(
                    "acceptance gate failed: " + "; ".join(prospective_reasons)
                )
            prospective_missing_outputs = _required_outputs_exist(worktree, contract)
            if prospective_missing_outputs:
                raise SystemExit(
                    f"required outputs are missing: {prospective_missing_outputs}"
                )
            try:
                build_required_output_bindings(
                    worktree,
                    prospective_commit,
                    list(contract.get("required_outputs", [])),
                )
            except ArtifactBundleError as exc:
                raise SystemExit(
                    f"required outputs are not bound to source_commit: {exc}"
                ) from exc
            if profile == "full":
                strategy = _bound_strategy_for_attempt(task, attempt, metadata)
                workflow_reasons = completion_gate_reasons(
                    attempt,
                    strategy,
                    include_acceptance=False,
                )
                if workflow_reasons:
                    raise SystemExit(
                        "handoff completion gate failed: "
                        + "; ".join(workflow_reasons)
                    )
            finalization_marker = _start_finalization_locked(
                attempt,
                task,
                status,
                metadata,
                binding,
                require_deadline=True,
                require_completion_gate=profile == "full",
            )
        else:
            finalization_marker = _validate_finalization_marker(
                attempt,
                task_id=str(status.get("task_id")),
                attempt_id=attempt.name,
                task_inputs_sha256=binding.task_inputs_sha256,
            )
        _validate_finalization_source_unchanged(
            attempt,
            worktree,
            finalization_marker,
        )
        require_clean_task_worktree(worktree, str(status.get("branch") or ""))
        source_commit = git_output(worktree, "rev-parse", "HEAD")
        selected, reasons = select_required_check_records(attempt, contract, binding)
        if reasons:
            raise SystemExit("acceptance gate failed: " + "; ".join(reasons))
        missing_outputs = _required_outputs_exist(worktree, contract)
        if missing_outputs:
            raise SystemExit(f"required outputs are missing: {missing_outputs}")
        try:
            required_output_bindings = build_required_output_bindings(
                worktree,
                source_commit,
                list(contract.get("required_outputs", [])),
            )
        except ArtifactBundleError as exc:
            raise SystemExit(f"required outputs are not bound to source_commit: {exc}") from exc
        if profile == "full":
            strategy = _bound_strategy_for_attempt(task, attempt, metadata)
            workflow_reasons = completion_gate_reasons(attempt, strategy)
            if workflow_reasons:
                raise SystemExit("handoff completion gate failed: " + "; ".join(workflow_reasons))

    before_ref = "runtime/worktree-before.json"
    before_path = attempt / before_ref
    if not before_path.is_file():
        raise SystemExit("attempt is missing runtime/worktree-before.json")
    after_ref = "runtime/worktree-after.json"
    try:
        publish_json_once(attempt / after_ref, fingerprint(worktree))
    except ArtifactBundleError as exc:
        raise SystemExit(f"cannot freeze worktree-after snapshot: {exc}") from exc

    if args.state != "blocked" and source_commit is not None:
        changed_paths = _git_changed_paths(
            worktree,
            str(binding.task_inputs["task_base_commit"]),
            source_commit,
        )
    else:
        changed_paths = _snapshot_changed_paths(before_path, attempt / after_ref)
    if metadata.get("phase") == "planning":
        require_clean_task_worktree(worktree, str(status.get("branch") or ""))
        task_base_commit = str(binding.task_inputs["task_base_commit"])
        live_head = git_output(worktree, "rev-parse", "HEAD")
        if source_commit != task_base_commit or live_head != task_base_commit:
            raise SystemExit(
                "planning attempt requires source_commit and HEAD to equal the frozen task base commit"
            )
        if changed_paths:
            raise SystemExit(f"planning attempt modified the task worktree: {changed_paths}")
    else:
        _validate_changed_path_policy(
            task,
            profile,
            changed_paths,
            attempt=attempt,
            metadata=metadata,
        )
    explicit_files = sorted(set(getattr(args, "file", [])))
    if explicit_files and explicit_files != changed_paths:
        raise SystemExit(
            "explicit --file values do not match the committed task diff: "
            f"explicit={explicit_files}, derived={changed_paths}"
        )

    log_refs: list[str] = []
    for record in selected:
        for field in ("stdout_ref", "stderr_ref"):
            ref = record.payload.get(field)
            if isinstance(ref, str) and ref not in log_refs:
                log_refs.append(ref)
    direct_self_review = {
        "performed": profile == "direct" and bool(args.self_review_passed),
        "passed": profile == "direct" and bool(args.self_review_passed),
        "summary": (
            (getattr(args, "self_review_summary", "") or f"Self-review completed: {summary}")
            if profile == "direct" and args.self_review_passed
            else ""
        ),
        "findings": list(args.self_review_finding),
    }
    if args.state == "verified" and not args.self_review_passed:
        raise SystemExit("Direct verified finalization requires --self-review-passed")
    blocker = (
        {"blocker_type": args.blocker_type, "reason": args.blocking_reason}
        if args.state == "blocked"
        else None
    )
    if finalization_marker is not None:
        _validate_finalization_source_unchanged(
            attempt,
            worktree,
            finalization_marker,
        )
    try:
        bundle = publish_bundle(
            attempt,
            requested_state=args.state,
            summary=summary,
            direct_self_review=direct_self_review,
            known_limitations=list(args.limitation),
            conditional_blocker=blocker,
            source_commit=source_commit,
            command_record_ids=[record.record_id for record in selected],
            changed_paths=changed_paths,
            worktree={"before": before_ref, "after": after_ref},
            log_refs=log_refs,
            artifact_refs=(
                [
                    FINALIZATION_SNAPSHOT_REF,
                    FINALIZATION_REF,
                    ATTEMPT_DEADLINE_REF,
                ]
                if finalization_marker is not None
                else []
            ),
            reviewer_evidence=_reviewer_evidence_refs(attempt),
            required_outputs=required_output_bindings,
            expected_task_id=str(status.get("task_id")),
            expected_attempt_id=attempt.name,
        )
    except ArtifactBundleError as exc:
        raise SystemExit(f"cannot publish v2 handoff: {exc}") from exc
    print(
        json.dumps(
            {
                "handoff": bundle.handoff,
                "artifact_binding": artifact_binding(bundle),
            },
            indent=2,
        )
    )
    return 0


def _finalize_v2(args: argparse.Namespace) -> int:
    with attempt_artifact_lock(args.attempt_dir, exclusive=True):
        return _finalize_v2_locked(args)


def handoff(args: argparse.Namespace) -> int:
    if getattr(args, "auto_derive", False) and getattr(args, "attempt_dir", ""):
        candidate = Path(args.attempt_dir).resolve().parent.parent
        candidate_status = load_json(candidate / "STATUS.json")
        if task_protocol(candidate, candidate_status) == ARTIFACT_PROTOCOL_VERSION:
            return _finalize_v2(args)
    if not getattr(args, "task_dir", ""):
        raise SystemExit("legacy handoff/finalize requires --task-dir")
    path = task_dir(args.task_dir)
    if task_protocol(path) == ARTIFACT_PROTOCOL_VERSION:
        raise SystemExit("Artifact Protocol v2 requires rdo finalize --attempt-dir <attempt>")
    if args.state not in {"verified", "review", "blocked"}:
        raise SystemExit("handoff state must be verified, review, or blocked")
    if args.state == "blocked" and (not args.blocker_type or not args.blocking_reason):
        raise SystemExit("blocked handoff requires blocker type and reason")
    status = load_json(path / "STATUS.json")
    profile = status.get("profile", "full")
    expected_terminal = {"direct": "verified", "delegated": "review", "full": "review"}[profile]
    if args.state in {"verified", "review"} and status.get("state") != "running":
        raise SystemExit(f"{args.state} handoff requires running state")
    if args.state in {"verified", "review"} and args.state != expected_terminal:
        raise SystemExit(f"profile {profile!r} requires {expected_terminal!r} handoff")
    if args.state == "verified" and not args.self_review_passed:
        raise SystemExit("direct verified handoff requires --self-review-passed")
    if args.state == "blocked" and status.get("state") not in {"planning", "running"}:
        raise SystemExit("blocked handoff requires an active attempt")
    if args.state == "review" and profile == "full":
        attempt = path / "attempts" / str(status.get("current_attempt_id"))
        strategy, _ = load_approved_strategy(path)
        reasons = completion_gate_reasons(attempt, strategy)
        if reasons:
            raise SystemExit("handoff completion gate failed: " + "; ".join(reasons))
    summary = args.summary
    if getattr(args, "summary_file", ""):
        summary = Path(args.summary_file).read_text(encoding="utf-8").strip()
    if not summary:
        raise SystemExit("handoff requires a non-empty summary or --summary-file")
    attempt_id = str(status.get("current_attempt_id"))
    attempt_path = path / "attempts" / attempt_id
    commands = list(args.command)
    files = list(args.file)
    source_commit = None
    if profile == "direct" and args.state == "verified" and not getattr(args, "auto_derive", False):
        raise SystemExit("direct verified handoff requires rdo finalize")
    if getattr(args, "auto_derive", False):
        recorded = [item for item in command_events(attempt_path) if item.get("acceptance") is True]
        if recorded:
            commands = [" ".join(map(str, item.get("command", []))) for item in recorded]
        metadata = load_json(attempt_path / "ATTEMPT.json")
        cwd = metadata.get("runtime", {}).get("cwd") if isinstance(metadata.get("runtime"), dict) else None
        if isinstance(cwd, str) and cwd:
            cwd_path = Path(cwd).resolve()
            require_clean_task_worktree(cwd_path, str(status.get("branch") or ""))
            if args.state in {"verified", "review"}:
                source_commit = git_output(cwd_path, "rev-parse", "HEAD")
            derived_files = derive_task_changed_files(path, attempt_path, cwd_path)
            if files and sorted(set(files)) != derived_files:
                raise SystemExit(
                    "explicit --file values do not match the task worktree diff: "
                    f"explicit={sorted(set(files))}, derived={derived_files}"
                )
            files = derived_files
    if profile == "direct" and args.state == "verified" and not commands:
        raise SystemExit("direct verified handoff requires at least one recorded acceptance command")
    evidence_lines = ["# Evidence", "", "## Commands Run", ""]
    evidence_lines.extend(f"- `{item}`" for item in commands)
    evidence_lines.extend(["", "## Files Changed", ""])
    evidence_lines.extend(f"- `{item}`" for item in files)
    atomic_text(path / "EVIDENCE.md", "\n".join(evidence_lines) + "\n")
    atomic_text(path / "HANDOFF.md", f"# Handoff\n\n## Summary\n\n{summary}\n")
    request = {
        "_template": False,
        "requested_state": args.state,
        "summary": summary,
        "commands_run": commands,
        "files_changed": files,
        "known_limitations": args.limitation,
        "self_review": {
            "acceptance_checked": bool(args.self_review_passed),
            "changed_paths_checked": bool(args.self_review_passed),
            "tests_passed": bool(args.self_review_passed),
            "diff_check_passed": bool(args.self_review_passed),
            "findings": args.self_review_finding,
            "fixes_applied": args.self_review_fix,
            "passed": bool(args.self_review_passed),
        },
        "needs_coordinator": args.state == "blocked",
        "blocker_type": args.blocker_type,
        "blocking_reason": args.blocking_reason,
    }
    if source_commit is not None:
        request["source_commit"] = source_commit
    temporary = path / "HANDOFF.json.tmp"
    write_json(temporary, request)
    os.replace(temporary, path / "HANDOFF.json")
    attempt_metadata = load_json(attempt_path / "ATTEMPT.json")
    write_completion(
        path,
        attempt_id=attempt_id,
        phase=str(attempt_metadata.get("phase")),
        requested_state=args.state,
        strategy_sha256=attempt_metadata.get("strategy_sha256"),
        source_commit=source_commit,
    )
    print(json.dumps(request))
    return 0


def _execute_command_locked(args: argparse.Namespace) -> int:
    raw_attempt = Path(args.attempt_dir).resolve()
    raw_task = raw_attempt.parent.parent
    raw_status_path = raw_task / "STATUS.json"
    v2 = False
    if raw_status_path.exists():
        raw_status = load_json(raw_status_path)
        if task_protocol(raw_task, raw_status) == ARTIFACT_PROTOCOL_VERSION:
            v2 = True
            if args.acceptance:
                raise SystemExit(
                    "Artifact Protocol v2 forbids free rdo exec acceptance evidence; "
                    "use rdo check --check-id"
                )
    attempt, _task, strategy = active_execution_attempt(args.attempt_dir)
    _emit_deadline_notice(attempt)
    if (attempt / FINALIZATION_REF).exists():
        raise SystemExit("rdo exec is forbidden after finalization starts")
    definitions = {item["workflow_id"]: item for item in strategy["workflows"]}
    definition = definitions.get(args.workflow_id)
    if definition is None:
        raise SystemExit(f"workflow is not approved: {args.workflow_id}")
    records = workflow_events(attempt)
    active = {
        record.get("instance_id")
        for record in records
        if record.get("event") == "workflow_started" and record.get("workflow_id") == args.workflow_id
    }
    for record in records:
        if record.get("event") in {"workflow_completed", "workflow_timed_out", "workflow_cancelled"}:
            active.discard(record.get("instance_id"))
    if args.instance_id not in active:
        raise SystemExit("rdo exec requires an active approved workflow instance")
    if args.timeout > definition["budget"]["command_seconds"]:
        raise SystemExit("command timeout exceeds approved workflow command budget")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise SystemExit("rdo exec requires a command after --")
    result = run_supervised(
        command,
        timeout_seconds=args.timeout,
        cwd=Path(args.cwd).resolve() if args.cwd else None,
        stdin=0,
        stdout=1,
        stderr=2,
    )
    record = {
        "at": utc_now(),
        "event": "command_completed",
        "workflow_id": args.workflow_id,
        "instance_id": args.instance_id,
        "command": command,
        "timeout_seconds": args.timeout,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "elapsed_seconds": result.elapsed_seconds,
        "surviving_pids": list(result.surviving_pids),
        "acceptance": args.acceptance,
    }
    runtime = attempt / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    command_log = "WORKFLOW_COMMANDS.ndjson" if v2 else "COMMANDS.ndjson"
    _append_command_record(runtime / command_log, record)
    return result.exit_code


def execute_command(args: argparse.Namespace) -> int:
    attempt = Path(args.attempt_dir).resolve()
    task = attempt.parent.parent
    status_path = task / "STATUS.json"
    if status_path.exists():
        status = load_json(status_path)
        if task_protocol(task, status) == ARTIFACT_PROTOCOL_VERSION:
            with attempt_artifact_lock(attempt, exclusive=False):
                return _execute_command_locked(args)
    return _execute_command_locked(args)


def status_action(args: argparse.Namespace) -> int:
    task_path = task_dir(args.task_dir)
    status = load_json(task_path / "STATUS.json")
    attempt_id = status.get("current_attempt_id")
    payload: dict[str, Any] = {
        "projection": None,
        "status": status,
        "attempt": None,
        "supervisor": None,
        "workflows": [],
        "backend_profile": None,
        "agents": None,
        "backend_events": [],
        "governance_violations": [],
    }
    if (
        task_protocol(task_path, status) == ARTIFACT_PROTOCOL_VERSION
        and (task_path / "EXECUTION_POLICY.json").is_file()
    ):
        try:
            payload["task_budget"] = assess_task_budget(task_path)
        except TaskBudgetError as exc:
            payload["task_budget"] = {
                "enabled": True,
                "admission": {
                    "allowed": False,
                    "blocker_type": "budget",
                    "blocking_reason": str(exc),
                    "reasons": [{"code": "budget_evidence_invalid", "reason": str(exc)}],
                },
            }
    if attempt_id:
        attempt_dir = task_path / "attempts" / str(attempt_id)
        attempt_path = attempt_dir / "ATTEMPT.json"
        if attempt_path.exists():
            payload["attempt"] = load_json(attempt_path)
        supervisor_path = attempt_dir / "runtime" / "supervisor.json"
        if supervisor_path.exists():
            payload["supervisor"] = load_json(supervisor_path)
        payload["workflows"] = workflow_events(attempt_dir)
        runtime_dir = attempt_dir / "runtime"
        for key, filename in (
            ("backend_profile", "BACKEND_PROFILE.json"),
            ("agents", "AGENTS.json"),
        ):
            artifact_path = runtime_dir / filename
            if artifact_path.exists():
                payload[key] = load_json(artifact_path)
        for key, filename in (
            ("backend_events", "BACKEND_EVENTS.ndjson"),
            ("governance_violations", "VIOLATIONS.ndjson"),
        ):
            artifact_path = runtime_dir / filename
            if artifact_path.exists():
                payload[key] = [
                    json.loads(line)
                    for line in artifact_path.read_text().splitlines()
                    if line.strip()
                ]
    payload["projection"] = resolve_status_projection(
        task_path,
        status,
        attempt=payload["attempt"],
    ).projection
    print(json.dumps(payload, indent=2))
    return 0


def cleanup_audit(args: argparse.Namespace) -> int:
    """Read-only post-attempt audit of current token-bearing processes."""

    attempt = Path(args.attempt_dir).resolve()
    if attempt.parent.name != "attempts" or attempt.name in {"", ".", ".."}:
        raise SystemExit("attempt directory must be task-local under attempts/")
    task = attempt.parent.parent.resolve()
    if attempt.parent.resolve() != (task / "attempts").resolve():
        raise SystemExit("attempt directory is not owned by the task")
    status = load_json(task / "STATUS.json")
    metadata = load_json(attempt / "ATTEMPT.json")
    supervisor_path = attempt / "runtime" / "supervisor.json"
    supervisor = (
        load_json(supervisor_path)
        if supervisor_path.is_file() and not supervisor_path.is_symlink()
        else None
    )
    task_id = status.get("task_id") if isinstance(status, dict) else None
    identity_valid = bool(
        isinstance(status, dict)
        and isinstance(metadata, dict)
        and metadata.get("task_id") == task_id
        and metadata.get("attempt_id") == attempt.name
    )
    supervisor_state = supervisor.get("state") if isinstance(supervisor, dict) else None
    attempt_terminal = metadata.get("state") in {"completed", "invalid_handoff"}
    supervisor_terminal = supervisor_state in {
        "completed",
        "timed_out",
        "cleanup_failed",
    }
    eligible = bool(
        identity_valid
        and attempt_terminal
        and supervisor_terminal
    )
    recorded_cleanup = (
        {
            "state": supervisor.get("state"),
            "cleanup_verified": supervisor.get("cleanup_verified"),
            "cleanup_failure_reason": supervisor.get("cleanup_failure_reason"),
            "surviving_pids": supervisor.get("surviving_pids", []),
        }
        if isinstance(supervisor, dict)
        else None
    )
    base: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "attempt_id": attempt.name,
        "audited_at": utc_now(),
        "eligible": eligible,
        "observation_scope": "current_token_visible_processes",
        "recorded_cleanup": recorded_cleanup,
        "live_processes": [],
    }
    if not eligible:
        reason = (
            "attempt_identity_mismatch"
            if not identity_valid
            else "supervisor_state_missing"
            if not isinstance(supervisor, dict)
            else "attempt_not_terminal"
            if not attempt_terminal
            else "supervisor_still_running"
            if supervisor_state == "running"
            else "supervisor_not_terminal"
        )
        print(json.dumps({**base, "status": "ineligible", "reason": reason}, indent=2))
        return 2

    token = supervisor.get("supervision_token")
    try:
        audit = audit_supervision_token(token)
    except ValueError as exc:
        print(
            json.dumps(
                {**base, "status": "invalid_evidence", "reason": str(exc)},
                indent=2,
            )
        )
        return 2
    live = [
        {"pid": pid, "ppid": ppid, "pgid": pgid}
        for pid, ppid, pgid in audit.live_processes
    ]
    if not audit.inspection_verified:
        result = {
            **base,
            "status": "inspection_unavailable",
            "reason": audit.inspection_failure_reason,
            "live_processes": live,
        }
        exit_code = 126
    elif live:
        result = {**base, "status": "live_processes", "reason": None, "live_processes": live}
        exit_code = 1
    else:
        result = {
            **base,
            "status": "no_live_processes_observed",
            "reason": None,
        }
        exit_code = 0
    print(json.dumps(result, indent=2))
    return exit_code


def _tmux_inventory(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return build_tmux_inventory(
            Path(args.repo_root),
            list_live_tmux_sessions(),
            run_id=args.run,
            active_only=getattr(args, "active", False),
        )
    except TmuxLifecycleError as exc:
        raise SystemExit(str(exc)) from exc


def tmux_list(args: argparse.Namespace) -> int:
    """List live tmux sessions attributable to this repository's attempts."""

    print(json.dumps(_tmux_inventory(args), indent=2))
    return 0


def tmux_prune(args: argparse.Namespace) -> int:
    """Close only clean terminal tmux sessions after identity revalidation."""

    if getattr(args, "terminal", False) is not True:
        raise SystemExit("tmux prune requires explicit --terminal")
    inventory = _tmux_inventory(args)
    selected = [row for row in inventory["sessions"] if row.get("prunable") is True]
    results: list[dict[str, Any]] = []
    for row in selected:
        try:
            outcome = kill_live_tmux_session(row)
        except TmuxLifecycleError as exc:
            outcome = {"status": "failed", "reason": str(exc)}
        results.append(
            {
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "attempt_id": row["attempt_id"],
                "session_name": row["session_name"],
                "session_id": row["session_id"],
                **outcome,
            }
        )
    failures = [
        item for item in results if item["status"] in {"failed", "identity_changed"}
    ]
    payload = {
        "schema_version": 1,
        "action": "prune_terminal",
        "repo_root": inventory["repo_root"],
        "run_filter": inventory["run_filter"],
        "selected": len(selected),
        "results": results,
        "summary": {
            "killed": sum(item["status"] == "killed" for item in results),
            "already_absent": sum(
                item["status"] == "already_absent" for item in results
            ),
            "failed": len(failures),
            "retained_active": inventory["summary"]["active"],
            "retained_attention_required": inventory["summary"][
                "attention_required"
            ],
            "retained_ambiguous": inventory["summary"]["ambiguous"],
            "retained_untracked": inventory["summary"]["untracked_live"],
        },
    }
    print(json.dumps(payload, indent=2))
    return 1 if failures else 0


def control(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    if status.get("state") not in {"planning", "running"}:
        raise SystemExit("worker control requires an active planning or execution attempt")
    attempt_id = status.get("current_attempt_id")
    lock = path / ".dispatch-lock"
    if args.worker_action in {"message", "interrupt"}:
        if not isinstance(attempt_id, str) or not attempt_id:
            raise SystemExit("worker control requires a current attempt identity")
        attempt = path / "attempts" / attempt_id
        metadata = load_json(attempt / "ATTEMPT.json")
        runtime = metadata.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        session = runtime.get("tmux_session")
        if runtime.get("backend") != "tmux" or not isinstance(session, str) or not session:
            raise SystemExit("worker control requires an active tmux attempt")
        session_file = lock / "tmux_session"
        lock_attempt_file = lock / "attempt_id"
        if not session_file.is_file() or not lock_attempt_file.is_file():
            raise SystemExit("worker control requires an active tmux session")
        if (
            session_file.read_text(encoding="utf-8").strip() != session
            or lock_attempt_file.read_text(encoding="utf-8").strip() != attempt_id
        ):
            raise SystemExit("dispatch lock does not match the current tmux attempt")
        try:
            identity = load_attempt_tmux_identity(
                attempt,
                run_id=path.parent.parent.name,
                task_id=str(status.get("task_id")),
                attempt_id=attempt_id,
                session_name=session,
            )
            revalidate_live_tmux_identity(identity)
        except TmuxLifecycleError as exc:
            raise SystemExit(str(exc)) from exc
        target = str(identity["session_id"])
        if args.worker_action == "message":
            subprocess.run(["tmux", "send-keys", "-t", target, "-l", args.text], check=True)
            try:
                revalidate_live_tmux_identity(identity)
            except TmuxLifecycleError as exc:
                raise SystemExit(str(exc)) from exc
            subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)
            name, result = "worker_instruction_submitted", {
                "status": "submitted",
                "session": session,
                "session_id": target,
            }
        else:
            subprocess.run(["tmux", "send-keys", "-t", target, "C-c"], check=True)
            name, result = "worker_interrupted", {
                "status": "interrupt_sent",
                "session": session,
                "session_id": target,
            }
    else:
        metadata = path / "attempts" / str(attempt_id) / "runtime" / "supervisor.json"
        if not metadata.exists():
            raise SystemExit("attempt supervisor metadata is unavailable")
        runtime = load_json(metadata)
        if runtime.get("state") != "running":
            termination = None
            result = {
                "status": "not_running",
                "reason": "attempt supervisor is no longer running",
                "supervisor_state": runtime.get("state"),
                "surviving_pids": [],
                "cleanup_verified": False,
            }
        else:
            termination = terminate_current_supervision(
                runtime.get("worker_pid"),
                runtime.get("worker_pgid"),
                runtime.get("worker_start_identity"),
                runtime.get("supervision_token"),
            )
            result = {
                "status": (
                    "terminated"
                    if termination.identity_verified and termination.cleanup_verified
                    else "cleanup_failed"
                    if termination.identity_verified
                    else "identity_unverified"
                ),
                "reason": (
                    termination.identity_failure_reason
                    or termination.cleanup_failure_reason
                ),
                "root_running": termination.root_running,
                "targeted_pids": list(termination.targeted_pids),
                "targeted_pgids": list(termination.targeted_pgids),
                "surviving_pids": list(termination.surviving_pids),
                "cleanup_verified": termination.cleanup_verified,
            }
        succeeded = result["status"] == "terminated"
        name = "worker_terminated" if succeeded else "worker_termination_failed"
    event(path, name, "coordinator", attempt_id=attempt_id, **result)
    print(json.dumps(result))
    return 0 if args.worker_action != "terminate" or result["status"] == "terminated" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RDO local command surface")
    areas = parser.add_subparsers(dest="area", required=True)
    strategy = areas.add_parser("strategy").add_subparsers(dest="strategy_action", required=True)
    for name in ("submit", "revise"):
        command = strategy.add_parser(name); command.add_argument("--task-dir", required=True)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--file"); source.add_argument("--draft", action="store_true")
        command.set_defaults(func=strategy_submit)
    command = strategy.add_parser("scaffold"); command.add_argument("--attempt-dir", required=True); command.set_defaults(func=strategy_scaffold)
    command = strategy.add_parser("preflight"); command.add_argument("--attempt-dir", required=True)
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--file"); source.add_argument("--draft", action="store_true")
    command.set_defaults(func=strategy_preflight)
    command = strategy.add_parser("draft"); command.add_argument("--attempt-dir", required=True); command.add_argument("--file", required=True); command.set_defaults(func=strategy_draft)
    for name in ("approve", "changes"):
        command = strategy.add_parser(name); command.add_argument("--task-dir", required=True); command.add_argument("--revision", type=int, required=True); command.add_argument("--reviewer", required=True); command.add_argument("--note", action="append", default=[]); command.set_defaults(func=strategy_review)
    tasks = areas.add_parser("task").add_subparsers(dest="task_action", required=True)
    command = tasks.add_parser("review"); command.add_argument("--task-dir", required=True); command.add_argument("--decision", choices=("approved", "changes_requested", "failed"), required=True); command.add_argument("--reviewer", required=True); command.add_argument("--findings-file", required=True); command.add_argument("--note", action="append", default=[]); command.set_defaults(func=task_review)
    command = tasks.add_parser("revise"); command.add_argument("--task-dir", required=True); command.add_argument("--reviewer", required=True); command.add_argument("--findings-file", required=True); command.add_argument("--note", action="append", default=[]); command.set_defaults(func=task_revise)
    command = tasks.add_parser("resume"); command.add_argument("--task-dir", required=True)
    command.add_argument("--worker-backend", choices=("claude-code", "codex", "opencode", "kimi-code"), default="")
    command.add_argument("--runtime-backend", choices=("plain", "tmux"), default="")
    command.add_argument("--io-mode", choices=("machine", "human"), default="")
    command.add_argument("--permission-mode", choices=("default", "auto", "yolo"), default="")
    command.add_argument("--agent-name", default=""); command.add_argument("--session-id", default=""); command.add_argument("--worker-id", default="")
    command.add_argument("--execution-mode", choices=("auto", "start", "resume", "replace"), default="auto")
    command.add_argument("--phase", choices=("auto", "planning", "execution"), default="auto")
    command.set_defaults(func=task_resume)
    command = tasks.add_parser("preview-prompt"); command.add_argument("--task-dir", required=True); command.add_argument("--worker-backend", choices=("claude-code", "codex", "opencode", "kimi-code"), default=""); command.add_argument("--execution-mode", choices=("auto", "start", "resume", "replace"), default="auto"); command.add_argument("--phase", choices=("auto", "planning", "execution"), default="auto"); command.add_argument("--body-only", action="store_true"); command.set_defaults(func=task_preview_prompt)
    command = tasks.add_parser("merge"); command.add_argument("--task-dir", required=True); command.add_argument("--target-worktree", required=True); command.add_argument("--expected-commit", default=""); command.add_argument("--verify-command", action="append", default=[]); command.add_argument("--verification-timeout", type=float, default=300); command.add_argument("--coordinator", required=True); command.set_defaults(func=task_merge)
    workflows = areas.add_parser("workflow").add_subparsers(dest="workflow_action", required=True)
    for name in ("start", "heartbeat", "complete"):
        command = workflows.add_parser(name); command.add_argument("--attempt-dir", required=True); command.add_argument("--workflow-id", required=True); command.add_argument("--instance-id", required=True); command.add_argument("--review-evidence", action="append", default=[]); command.set_defaults(func=workflow_action)
    finalization = areas.add_parser("finalization").add_subparsers(dest="finalization_action", required=True)
    command = finalization.add_parser("begin"); command.add_argument("--attempt-dir", required=True); command.set_defaults(func=finalization_action)
    command = areas.add_parser("handoff"); command.add_argument("--task-dir", required=True); command.add_argument("--state", required=True); command.add_argument("--summary", required=True); command.add_argument("--command", action="append", default=[]); command.add_argument("--file", action="append", default=[]); command.add_argument("--limitation", action="append", default=[]); command.add_argument("--self-review-passed", action="store_true"); command.add_argument("--self-review-summary", default=""); command.add_argument("--self-review-finding", action="append", default=[]); command.add_argument("--self-review-fix", action="append", default=[]); command.add_argument("--blocker-type", default=""); command.add_argument("--blocking-reason", default=""); command.set_defaults(func=handoff)
    command = areas.add_parser("finalize"); command.add_argument("--task-dir", default=""); command.add_argument("--attempt-dir", default=""); command.add_argument("--state", required=True); command.add_argument("--summary", default=""); command.add_argument("--summary-file", default=""); command.add_argument("--command", action="append", default=[]); command.add_argument("--file", action="append", default=[]); command.add_argument("--limitation", action="append", default=[]); command.add_argument("--self-review-passed", action="store_true"); command.add_argument("--self-review-summary", default=""); command.add_argument("--self-review-finding", action="append", default=[]); command.add_argument("--self-review-fix", action="append", default=[]); command.add_argument("--blocker-type", default=""); command.add_argument("--blocking-reason", default=""); command.set_defaults(func=handoff, auto_derive=True)
    command = areas.add_parser("check"); command.add_argument("--attempt-dir", required=True); command.add_argument("--check-id", required=True); command.add_argument("--workflow-id", default=""); command.add_argument("--instance-id", default=""); command.set_defaults(func=check_command)
    command = areas.add_parser("exec"); command.add_argument("--attempt-dir", required=True); command.add_argument("--workflow-id", required=True); command.add_argument("--instance-id", required=True); command.add_argument("--timeout", type=float, required=True); command.add_argument("--cwd", default=""); command.add_argument("--acceptance", action="store_true"); command.add_argument("command", nargs=argparse.REMAINDER); command.set_defaults(func=execute_command)
    command = areas.add_parser("status"); command.add_argument("--task-dir", required=True); command.set_defaults(func=status_action)
    cleanup = areas.add_parser("cleanup").add_subparsers(dest="cleanup_action", required=True)
    command = cleanup.add_parser("audit"); command.add_argument("--attempt-dir", required=True); command.set_defaults(func=cleanup_audit)
    tmux = areas.add_parser("tmux").add_subparsers(dest="tmux_action", required=True)
    command = tmux.add_parser("list"); command.add_argument("--repo-root", default="."); command.add_argument("--run", default=""); command.add_argument("--active", action="store_true"); command.set_defaults(func=tmux_list)
    command = tmux.add_parser("prune"); command.add_argument("--repo-root", default="."); command.add_argument("--run", default=""); command.add_argument("--terminal", action="store_true", required=True); command.set_defaults(func=tmux_prune, active=False)
    workers = areas.add_parser("worker").add_subparsers(dest="worker_action", required=True)
    command = workers.add_parser("message"); command.add_argument("--task-dir", required=True); command.add_argument("--text", required=True); command.set_defaults(func=control)
    for name in ("interrupt", "terminate"):
        command = workers.add_parser(name); command.add_argument("--task-dir", required=True); command.set_defaults(func=control)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        return args.func(args)
    except StrategyValidationError as exc:
        print(f"strategy error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
