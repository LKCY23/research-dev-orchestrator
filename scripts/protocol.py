#!/usr/bin/env python3
"""Shared protocol constants and pure helpers for research-dev-orchestrator scripts."""

from __future__ import annotations

import json
import os
import subprocess
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
    path.write_text(json.dumps(payload, indent=2, sort_keys=sort_keys) + "\n", encoding="utf-8")


def render_template(relative_path: str, values: dict[str, str] | None = None) -> str:
    text = (TEMPLATE_DIR / relative_path).read_text(encoding="utf-8")
    for key, value in (values or {}).items():
        text = text.replace("{{" + key + "}}", value)
    return text.rstrip() + "\n"


def append_event(run_dir: Path, event: dict[str, Any]) -> None:
    with (run_dir / "EVENTS.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


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
