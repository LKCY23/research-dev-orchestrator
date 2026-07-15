#!/usr/bin/env python3
"""Shared protocol constants and pure helpers for research-dev-orchestrator scripts."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import fcntl
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = SKILL_ROOT / "templates"


def load_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    version_path = SKILL_ROOT / "VERSION"

    for raw_line in version_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"Invalid VERSION line: {raw_line!r}")
        key, value = line.split("=", 1)
        versions[key.strip()] = value.strip()

    for key in ("PACKAGE_VERSION", "PROTOCOL_VERSION"):
        if not versions.get(key):
            raise RuntimeError(f"VERSION missing {key}")

    return versions


_VERSIONS = load_versions()
PACKAGE_VERSION = _VERSIONS["PACKAGE_VERSION"]
PROTOCOL_VERSION = _VERSIONS["PROTOCOL_VERSION"]
ARTIFACT_PROTOCOL_VERSION = 2
LEGACY_PROTOCOL_VERSIONS = {"research-dev-orchestrator/v0.5"}

REQUIRED_STATUS_FIELDS = {
    "task_id",
    "state",
    "previous_state",
    "owner",
    "branch",
    "worktree",
    "updated_at",
    "needs_coordinator",
    "summary",
    "blocking_reason",
    "blocker_type",
    "current_attempt_id",
    "assigned_worker",
    "evidence",
    "state_history",
}

TASK_STATES = {
    "pending",
    "planning",
    "strategy_review",
    "running",
    "blocked",
    "verified",
    "review",
    "changes_requested",
    "approved",
    "merged",
    "failed",
}

BLOCKER_TYPES = {"needs_coordinator", "needs_user", "environment", "budget", "irrecoverable"}
ATTEMPT_STATES = {"created", "running", "completed", "invalid_handoff"}
HANDOFF_STATES = {"strategy_review", "verified", "review", "blocked", None}
EXECUTION_PROFILES = {"direct", "delegated", "full"}
EXECUTION_MODES = {"start", "resume", "replace"}
RUNTIME_BACKENDS = {"plain", "tmux"}
IO_MODES = {"machine", "human"}
WORKER_BACKENDS = {"claude-code", "codex", "opencode", "kimi-code"}
COORDINATOR_BACKENDS = {"codex", "claude-code"}
PERMISSION_MODES = {"default", "auto", "yolo"}

CORE_EVENTS = {
    "run_created",
    "requirements_updated",
    "design_method_selected",
    "adr_added",
    "task_created",
    "task_dispatched",
    "worker_process_started",
    "prompt_dispatched",
    "worker_started",
    "worker_waiting_for_user",
    "worker_startup_failed",
    "strategy_submitted",
    "strategy_reviewed",
    "strategy_review_ready",
    "strategy_revision_requested",
    "workflow_started",
    "workflow_heartbeat",
    "workflow_completed",
    "workflow_carried_forward",
    "workflow_timed_out",
    "worker_instruction_submitted",
    "worker_interrupted",
    "worker_terminated",
    "attempt_timed_out",
    "worker_blocked",
    "worker_review_ready",
    "worker_verified",
    "worker_exit_without_valid_status",
    "dispatch_lock_removed",
    "coordinator_reviewed",
    "codex_reviewed",
    "changes_requested",
    "task_approved",
    "task_merged",
    "task_failed",
    "experiment_recorded",
    "scope_changed",
    "session_closed",
}

TASK_EVENTS = {
    "task_created",
    "task_dispatched",
    "worker_process_started",
    "prompt_dispatched",
    "worker_started",
    "worker_waiting_for_user",
    "worker_startup_failed",
    "strategy_submitted",
    "strategy_reviewed",
    "strategy_review_ready",
    "strategy_revision_requested",
    "worker_blocked",
    "worker_review_ready",
    "worker_verified",
    "worker_exit_without_valid_status",
    "dispatch_lock_removed",
    "coordinator_reviewed",
    "codex_reviewed",
    "changes_requested",
    "task_approved",
    "task_merged",
    "task_failed",
}

ATTEMPT_EVENTS = {
    "task_dispatched",
    "worker_process_started",
    "prompt_dispatched",
    "worker_started",
    "worker_waiting_for_user",
    "worker_startup_failed",
    "strategy_review_ready",
    "workflow_started",
    "workflow_heartbeat",
    "workflow_completed",
    "workflow_timed_out",
    "worker_blocked",
    "worker_review_ready",
    "worker_verified",
    "worker_exit_without_valid_status",
    "attempt_timed_out",
    "worker_instruction_submitted",
    "worker_interrupted",
    "worker_terminated",
}

TEMPLATE_MARKERS = {
    "EVIDENCE.md": "<!-- RDO_TEMPLATE: EVIDENCE -->",
    "HANDOFF.md": "<!-- RDO_TEMPLATE: HANDOFF -->",
}


def artifact_protocol_version(task_dir: Path, status: Any | None = None) -> int | None:
    """Return the explicit artifact protocol family for one task.

    New tasks record the family in STATUS. Legacy v0.5 runs are routed by the
    run-level protocol version. Unknown run versions deliberately return None
    so mutating consumers can fail closed rather than guess from file presence.
    """

    payload = status if isinstance(status, dict) else None
    if payload is None:
        status_path = task_dir / "STATUS.json"
        if status_path.exists():
            loaded = load_json(status_path)
            payload = loaded if isinstance(loaded, dict) else None
    declared = payload.get("artifact_protocol_version") if payload else None
    if declared is not None:
        return (
            declared
            if isinstance(declared, int)
            and not isinstance(declared, bool)
            and declared in {1, ARTIFACT_PROTOCOL_VERSION}
            else None
        )
    run_path = task_dir.parent.parent / "RUN.json"
    if not run_path.exists():
        return 1
    run = load_json(run_path)
    version = run.get("protocol_version") if isinstance(run, dict) else None
    if version == PROTOCOL_VERSION:
        # Current runs require an explicit task discriminator. Inferring v2
        # from the run would make a malformed STATUS.json look valid while
        # read-only consumers route the same task as legacy.
        return None
    if version in LEGACY_PROTOCOL_VERSIONS or version is None:
        return 1
    return None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_git(args: list[str], cwd: Path, default: str = "") -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return default


def repo_root(cwd: Path) -> Path:
    root = run_git(["rev-parse", "--show-toplevel"], cwd)
    return Path(root) if root else cwd


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any, *, sort_keys: bool = False) -> None:
    """Atomically replace mutable protocol JSON without exposing truncated files."""

    encoded = (json.dumps(payload, indent=2, sort_keys=sort_keys) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def render_template(relative_path: str, values: dict[str, str] | None = None) -> str:
    text = (TEMPLATE_DIR / relative_path).read_text(encoding="utf-8")
    for key, value in (values or {}).items():
        text = text.replace("{{" + key + "}}", value)
    return text.rstrip() + "\n"


class EventJournalError(ValueError):
    """Raised when a complete EVENTS.ndjson record is malformed."""


def read_event_journal(
    run_dir: Path,
    *,
    tolerate_interrupted_tail: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read complete event records and optionally ignore one interrupted tail.

    Only a final record without a newline can be classified as an interrupted
    append.  Malformed newline-terminated or interior records always fail.
    """

    path = run_dir / "EVENTS.ndjson"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return [], None
    terminated = raw.endswith(b"\n")
    lines = raw.split(b"\n")
    records: list[dict[str, Any]] = []
    warning: str | None = None
    for index, encoded in enumerate(lines, start=1):
        if not encoded.strip():
            continue
        is_unterminated_tail = index == len(lines) and not terminated
        try:
            decoded = encoded.decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if is_unterminated_tail and tolerate_interrupted_tail:
                warning = (
                    f"EVENTS.ndjson has an interrupted trailing record at line {index}; "
                    "the incomplete bytes were ignored"
                )
                break
            raise EventJournalError(
                f"EVENTS.ndjson line {index} is malformed: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise EventJournalError(
                f"EVENTS.ndjson line {index} must contain a JSON object"
            )
        records.append(payload)
        if is_unterminated_tail:
            warning = (
                f"EVENTS.ndjson line {index} is complete JSON but lacks its final newline"
            )
    return records, warning


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("event journal write made no progress")
        offset += written


def _quarantine_interrupted_event_tail(run_dir: Path, tail: bytes) -> Path:
    digest = hashlib.sha256(tail).hexdigest()
    diagnostics = run_dir / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    path = diagnostics / f"EVENTS-interrupted-tail-{digest[:16]}.json"
    if not path.exists():
        write_json(
            path,
            {
                "schema_version": 1,
                "kind": "interrupted_event_journal_tail",
                "source": "EVENTS.ndjson",
                "sha256": digest,
                "byte_count": len(tail),
                "text": tail.decode("utf-8", errors="replace"),
                "recovered_at": utc_now(),
            },
            sort_keys=True,
        )
    return path


def append_event(run_dir: Path, event: dict[str, Any]) -> None:
    """Durably append one event while recovering only an interrupted tail."""

    if not isinstance(event, dict):
        raise TypeError("event must be a JSON object")
    encoded = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
    path = run_dir / "EVENTS.ndjson"
    lock_path = run_dir / ".EVENTS.ndjson.lock"
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        created = not path.exists()
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            size = os.lseek(descriptor, 0, os.SEEK_END)
            if size:
                os.lseek(descriptor, 0, os.SEEK_SET)
                raw = bytearray()
                while len(raw) < size:
                    chunk = os.read(descriptor, min(1024 * 1024, size - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                if raw and not raw.endswith(b"\n"):
                    boundary = raw.rfind(b"\n") + 1
                    tail = bytes(raw[boundary:])
                    try:
                        payload = json.loads(tail.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        _quarantine_interrupted_event_tail(run_dir, tail)
                        os.ftruncate(descriptor, boundary)
                    else:
                        if not isinstance(payload, dict):
                            raise EventJournalError(
                                "unterminated EVENTS.ndjson tail is not a JSON object"
                            )
                        _write_all(descriptor, b"\n")
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            directory = os.open(run_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_int_not_bool(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def has_substantive_content(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return False
    marker = TEMPLATE_MARKERS.get(path.name)
    if marker and marker in text:
        return False
    return True
