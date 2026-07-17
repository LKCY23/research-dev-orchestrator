#!/usr/bin/env python3
"""Conservative inventory and cleanup for artifact-bound tmux sessions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from protocol import parse_iso


class TmuxLifecycleError(RuntimeError):
    """Raised when tmux or repository lifecycle evidence is unavailable."""


_IDENTITY_FORMAT = "#{session_id}\t#{session_created}\t#{session_name}"
_ACTIVE_TASK_STATES = {"planning", "running"}
_PRUNABLE_HANDOFF_STATES = {"strategy_review", "verified", "review"}
TMUX_IDENTITY_REF = "runtime/TMUX_SESSION.json"


def _parse_identity(line: str) -> dict[str, Any]:
    parts = line.split("\t", 2)
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise TmuxLifecycleError("tmux returned an invalid session identity")
    try:
        created = int(parts[1])
    except ValueError as exc:
        raise TmuxLifecycleError("tmux returned an invalid session creation time") from exc
    return {
        "session_id": parts[0],
        "created_at_epoch": created,
        "session_name": parts[2],
    }


def _missing_session(stderr: str) -> bool:
    detail = stderr.lower()
    return "no server running" in detail or "can't find session" in detail


def list_live_tmux_sessions() -> list[dict[str, Any]]:
    """Return stable tmux identities without inferring protocol state from tmux."""

    try:
        completed = subprocess.run(
            ["tmux", "list-sessions", "-F", _IDENTITY_FORMAT],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TmuxLifecycleError("tmux executable is unavailable") from exc
    if completed.returncode != 0:
        if not completed.stdout.strip() and _missing_session(completed.stderr):
            return []
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise TmuxLifecycleError(f"cannot list tmux sessions: {detail}")
    return sorted(
        (_parse_identity(line) for line in completed.stdout.splitlines() if line),
        key=lambda item: (item["session_name"], item["session_id"]),
    )


def inspect_live_tmux_session(session_id: str) -> dict[str, Any] | None:
    """Resolve one tmux ID immediately before mutation."""

    try:
        completed = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session_id, _IDENTITY_FORMAT],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TmuxLifecycleError("tmux executable is unavailable") from exc
    if completed.returncode != 0:
        if _missing_session(completed.stderr):
            return None
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise TmuxLifecycleError(f"cannot inspect tmux session {session_id}: {detail}")
    return _parse_identity(completed.stdout.rstrip("\n"))


def kill_live_tmux_session(expected: dict[str, Any]) -> dict[str, Any]:
    """Kill only the same tmux identity observed during inventory."""

    observed = inspect_live_tmux_session(str(expected["session_id"]))
    if observed is None:
        return {"status": "already_absent", "reason": None}
    if observed != {
        "session_id": expected["session_id"],
        "created_at_epoch": expected["created_at_epoch"],
        "session_name": expected["session_name"],
    }:
        return {
            "status": "identity_changed",
            "reason": "tmux session identity changed after inventory",
            "observed": observed,
        }
    try:
        completed = subprocess.run(
            ["tmux", "kill-session", "-t", str(expected["session_id"])],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TmuxLifecycleError("tmux executable is unavailable") from exc
    if completed.returncode == 0:
        return {"status": "killed", "reason": None}
    try:
        if inspect_live_tmux_session(str(expected["session_id"])) is None:
            return {"status": "already_absent", "reason": None}
    except TmuxLifecycleError:
        pass
    detail = completed.stderr.strip() or f"exit {completed.returncode}"
    return {"status": "failed", "reason": detail}


def record_tmux_session_identity(
    output: Path,
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    session_name: str,
) -> dict[str, Any]:
    """Atomically bind the live tmux identity created for one attempt."""

    identity = inspect_live_tmux_session(session_name)
    if identity is None or identity["session_name"] != session_name:
        raise TmuxLifecycleError("new tmux session identity is unavailable")
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        **identity,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    return payload


def _safe_component(value: str, label: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
    ):
        raise TmuxLifecycleError(f"unsafe {label}: {value!r}")
    return value


def _load_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file() or path.is_symlink():
        return None, f"{path.name} is missing or unsafe"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"{path.name} is unreadable: {exc}"
    if not isinstance(payload, dict):
        return None, f"{path.name} is not an object"
    return payload, None


def _safe_text(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _cleanup_evidence(attempt_dir: Path) -> tuple[bool, str]:
    payload, error = _load_object(attempt_dir / "supervisor-result.json")
    if error or payload is None:
        return False, error or "supervisor result is unavailable"
    if payload.get("cleanup_verified") is not True:
        return False, "process cleanup was not verified"
    if payload.get("surviving_pids") != []:
        return False, "supervisor recorded surviving processes"
    return True, "process cleanup verified"


def _transcript_ref(attempt_dir: Path) -> str | None:
    for relative in ("runtime/transcript.log", "transcript.log"):
        candidate = attempt_dir / relative
        if (
            candidate.is_file()
            and not candidate.is_symlink()
            and not candidate.parent.is_symlink()
        ):
            return relative
    return None


def _attempt_reference(
    run_id: str,
    task_dir: Path,
    status: dict[str, Any] | None,
    status_error: str | None,
    attempt_dir: Path,
    attempt: dict[str, Any],
) -> dict[str, Any] | None:
    runtime = attempt.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    session_name = runtime.get("tmux_session")
    if not isinstance(session_name, str) or not session_name:
        return None

    attempt_id = attempt_dir.name
    task_id = task_dir.name
    task_state = status.get("state") if status is not None else None
    current_attempt_id = status.get("current_attempt_id") if status is not None else None
    identity_reasons: list[str] = []
    if status_error:
        identity_reasons.append(status_error)
    if status is not None and status.get("task_id") != task_id:
        identity_reasons.append("STATUS task identity does not match its directory")
    if attempt.get("task_id") != task_id or attempt.get("attempt_id") != attempt_id:
        identity_reasons.append("ATTEMPT identity does not match its directory")
    if runtime.get("backend") != "tmux":
        identity_reasons.append("ATTEMPT runtime backend is not tmux")

    lock = task_dir / ".dispatch-lock"
    lock_is_dir = lock.is_dir() and not lock.is_symlink()
    lock_present = lock_is_dir or lock.is_symlink()
    lock_session = _safe_text(lock / "tmux_session") if lock_is_dir else None
    lock_attempt = _safe_text(lock / "attempt_id") if lock_is_dir else None
    lock_matches = bool(
        lock_present
        and (
            lock_session == session_name
            or lock_attempt == attempt_id
            or current_attempt_id == attempt_id
        )
    )
    is_current = current_attempt_id == attempt_id
    active = bool(is_current and task_state in _ACTIVE_TASK_STATES)
    cleanup_verified, cleanup_reason = _cleanup_evidence(attempt_dir)
    transcript = _transcript_ref(attempt_dir)
    tmux_identity, tmux_identity_error = _load_object(
        attempt_dir / TMUX_IDENTITY_REF
    )
    expected_identity: dict[str, Any] | None = None
    if tmux_identity is not None:
        if (
            tmux_identity.get("schema_version") == 1
            and tmux_identity.get("run_id") == run_id
            and tmux_identity.get("task_id") == task_id
            and tmux_identity.get("attempt_id") == attempt_id
            and tmux_identity.get("session_name") == session_name
            and isinstance(tmux_identity.get("session_id"), str)
            and tmux_identity.get("session_id")
            and isinstance(tmux_identity.get("created_at_epoch"), int)
            and not isinstance(tmux_identity.get("created_at_epoch"), bool)
        ):
            expected_identity = {
                "session_id": tmux_identity["session_id"],
                "created_at_epoch": tmux_identity["created_at_epoch"],
                "session_name": session_name,
            }
        else:
            tmux_identity_error = "tmux identity receipt does not match its attempt"

    reasons = list(identity_reasons)
    if lock_matches and not active:
        reasons.append("dispatch lock is retained for this attempt")
    if is_current and task_state == "blocked":
        reasons.append("current task is blocked and may require inspection")
    if attempt.get("state") != "completed":
        reasons.append("attempt lifecycle is not completed")
    if attempt.get("outcome") != "completed":
        reasons.append("attempt outcome is not completed")
    if attempt.get("handoff_valid") is not True:
        reasons.append("attempt handoff is not valid")
    if attempt.get("handoff_state") not in _PRUNABLE_HANDOFF_STATES:
        reasons.append("attempt handoff state requires retention")
    exit_code = attempt.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
        reasons.append("attempt exit code is not a successful integer zero")
    if parse_iso(attempt.get("ended_at")) is None:
        reasons.append("attempt terminal timestamp is invalid")
    if not cleanup_verified:
        reasons.append(cleanup_reason)
    if transcript is None:
        reasons.append("attempt transcript is not preserved")
    if expected_identity is None:
        reasons.append(tmux_identity_error or "tmux identity receipt is unavailable")

    if active:
        classification = "active"
        prunable = False
        reasons = ["current task attempt is active"]
    elif reasons:
        classification = "attention_required"
        prunable = False
    else:
        classification = "terminal_prunable"
        prunable = True
        reasons = ["completed attempt has verified cleanup and a preserved transcript"]

    return {
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "task_state": task_state,
        "attempt_state": attempt.get("state"),
        "attempt_outcome": attempt.get("outcome"),
        "handoff_state": attempt.get("handoff_state"),
        "classification": classification,
        "prunable": prunable,
        "reasons": reasons,
        "transcript_ref": transcript,
        "expected_tmux_identity": expected_identity,
        "session_name": session_name,
    }


def _artifact_references(repo_root: Path, run_id: str) -> dict[str, list[dict[str, Any]]]:
    runs_dir = repo_root / ".agent-collab" / "runs"
    if not runs_dir.is_dir() or runs_dir.is_symlink():
        raise TmuxLifecycleError(f"RDO run store is unavailable: {runs_dir}")
    candidates = sorted(
        path for path in runs_dir.iterdir() if path.is_dir() and not path.is_symlink()
    )
    if run_id:
        run_id = _safe_component(run_id, "run id")
        selected_run = runs_dir / run_id
        if not selected_run.is_dir() or selected_run.is_symlink():
            raise TmuxLifecycleError(f"run does not exist or is unsafe: {run_id}")

    references: dict[str, list[dict[str, Any]]] = {}
    for run_dir in candidates:
        tasks_dir = run_dir / "tasks"
        if not tasks_dir.is_dir() or tasks_dir.is_symlink():
            continue
        for task_dir in sorted(
            path for path in tasks_dir.iterdir() if path.is_dir() and not path.is_symlink()
        ):
            status, status_error = _load_object(task_dir / "STATUS.json")
            attempts_dir = task_dir / "attempts"
            if not attempts_dir.is_dir() or attempts_dir.is_symlink():
                continue
            for attempt_dir in sorted(
                path
                for path in attempts_dir.iterdir()
                if path.is_dir() and not path.is_symlink()
            ):
                attempt, _attempt_error = _load_object(attempt_dir / "ATTEMPT.json")
                if attempt is None:
                    continue
                reference = _attempt_reference(
                    run_dir.name,
                    task_dir,
                    status,
                    status_error,
                    attempt_dir,
                    attempt,
                )
                if reference is not None:
                    references.setdefault(reference["session_name"], []).append(reference)
    return references


def build_tmux_inventory(
    repo_root: Path,
    live_sessions: list[dict[str, Any]],
    *,
    run_id: str = "",
    active_only: bool = False,
) -> dict[str, Any]:
    """Join live tmux identities to task artifacts without mutating either."""

    repo_root = repo_root.resolve()
    references = _artifact_references(repo_root, run_id)
    sessions: list[dict[str, Any]] = []
    untracked: list[dict[str, Any]] = []
    for identity in live_sessions:
        matches = references.get(identity["session_name"], [])
        if run_id and not any(item["run_id"] == run_id for item in matches):
            continue
        if not matches:
            if not active_only and not run_id:
                untracked.append(identity)
            continue
        if len(matches) != 1:
            row = {
                **identity,
                "classification": "ambiguous",
                "prunable": False,
                "reasons": ["multiple RDO attempts reference this session name"],
                "references": matches,
            }
        else:
            row = {**identity, **matches[0]}
            expected = row.get("expected_tmux_identity")
            if row.get("prunable") is True and expected != identity:
                row.update(
                    classification="attention_required",
                    prunable=False,
                    reasons=["live tmux identity does not match the dispatch receipt"],
                )
        active_match = row["classification"] == "active" or (
            row["classification"] == "ambiguous"
            and any(item["classification"] == "active" for item in row["references"])
        )
        if not active_only or active_match:
            sessions.append(row)

    counts = {
        key: sum(1 for row in sessions if row["classification"] == key)
        for key in ("active", "terminal_prunable", "attention_required", "ambiguous")
    }
    return {
        "schema_version": 1,
        "repo_root": str(repo_root),
        "run_filter": run_id or None,
        "active_only": active_only,
        "sessions": sessions,
        "untracked_sessions": untracked,
        "summary": {
            "shown": len(sessions),
            **counts,
            "untracked_live": len(untracked),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="RDO tmux lifecycle helper")
    subparsers = parser.add_subparsers(dest="action", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--output", required=True)
    record.add_argument("--run-id", required=True)
    record.add_argument("--task-id", required=True)
    record.add_argument("--attempt-id", required=True)
    record.add_argument("--session-name", required=True)
    args = parser.parse_args()
    try:
        payload = record_tmux_session_identity(
            Path(args.output),
            run_id=args.run_id,
            task_id=args.task_id,
            attempt_id=args.attempt_id,
            session_name=args.session_name,
        )
    except TmuxLifecycleError as exc:
        parser.error(str(exc))
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
