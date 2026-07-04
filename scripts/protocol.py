#!/usr/bin/env python3
"""Shared protocol constants and pure helpers for research-dev-orchestrator scripts."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "research-dev-orchestrator/v0.1"
SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = SKILL_ROOT / "templates"

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
    "running",
    "blocked",
    "review",
    "changes_requested",
    "approved",
    "merged",
    "failed",
}

BLOCKER_TYPES = {"needs_coordinator", "needs_user", "environment", "budget", "irrecoverable"}
ATTEMPT_STATES = {"created", "running", "completed", "invalid_handoff"}
HANDOFF_STATES = {"review", "blocked", None}
RUNTIME_BACKENDS = {"plain", "tmux"}
TMUX_EXIT_CODE_GRACE_SECONDS = 60

CORE_EVENTS = {
    "run_created",
    "requirements_updated",
    "design_method_selected",
    "adr_added",
    "task_created",
    "task_dispatched",
    "worker_blocked",
    "worker_review_ready",
    "worker_exit_without_valid_status",
    "dispatch_lock_removed",
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
    "worker_blocked",
    "worker_review_ready",
    "worker_exit_without_valid_status",
    "dispatch_lock_removed",
    "codex_reviewed",
    "changes_requested",
    "task_approved",
    "task_merged",
    "task_failed",
}

ATTEMPT_EVENTS = {"task_dispatched", "worker_blocked", "worker_review_ready", "worker_exit_without_valid_status"}

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
