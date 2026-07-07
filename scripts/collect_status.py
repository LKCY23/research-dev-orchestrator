#!/usr/bin/env python3
"""Collect and validate orchestration run status without mutating protocol state."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import load_config
from protocol import (  # noqa: E402
    PACKAGE_VERSION,
    PROTOCOL_VERSION,
    has_substantive_content,
    load_json,
    parse_iso,
    pid_is_alive,
    repo_root,
    utc_now,
)
from validation import (
    load_handoff_request,
    validate_attempt_schema,
    validate_event,
    validate_state_history,
    validate_status_schema,
)


def load_events(run_dir: Path, run_id: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    events_path = run_dir / "EVENTS.ndjson"
    if not events_path.exists():
        return [], ["run: missing required EVENTS.ndjson"], []
    events: list[dict[str, Any]] = []
    violations: list[str] = []
    warnings: list[str] = []
    for line_no, line in enumerate(events_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            violations.append(f"EVENTS.ndjson line {line_no}: malformed JSON: {exc}")
            continue
        if not isinstance(event, dict):
            violations.append(f"EVENTS.ndjson line {line_no}: event must be a JSON object")
            continue
        event_violations, event_warnings = validate_event(event, run_id, line_no)
        violations.extend(event_violations)
        warnings.extend(event_warnings)
        events.append(event)
    return events, violations, warnings


def load_handoff_index(task_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Load HANDOFF.json for summaries; terminal-state validation is stricter."""

    path = task_dir / "HANDOFF.json"
    if not path.exists():
        return None, []
    try:
        payload = load_json(path)
    except json.JSONDecodeError as exc:
        return None, [f"{task_dir.name}: HANDOFF.json malformed JSON: {exc}"]
    if not isinstance(payload, dict):
        return None, [f"{task_dir.name}: HANDOFF.json must be a JSON object when present"]
    if payload.get("_template") is True:
        return {"template": True}, []

    warnings: list[str] = []
    requested_state = payload.get("requested_state")
    if requested_state not in {"", None, "review", "blocked"}:
        warnings.append(f"{task_dir.name}: HANDOFF.json requested_state should be review or blocked")
    for field in ("commands_run", "files_changed", "known_limitations"):
        if field in payload and not isinstance(payload.get(field), list):
            warnings.append(f"{task_dir.name}: HANDOFF.json {field} must be a list")
    if "needs_coordinator" in payload and not isinstance(payload.get("needs_coordinator"), bool):
        warnings.append(f"{task_dir.name}: HANDOFF.json needs_coordinator must be boolean")
    return {
        "template": False,
        "requested_state": requested_state,
        "summary": payload.get("summary", ""),
        "commands_run": payload.get("commands_run", []),
        "files_changed": payload.get("files_changed", []),
        "known_limitations": payload.get("known_limitations", []),
        "needs_coordinator": payload.get("needs_coordinator", False),
        "blocker_type": payload.get("blocker_type", ""),
        "blocking_reason": payload.get("blocking_reason", ""),
    }, warnings


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_fsm() -> dict[str, Any]:
    return load_json(skill_root() / "references" / "state-machine.json")


def validate_attempt(
    task_dir: Path,
    status: dict[str, Any],
    stale_created_minutes: float,
    tmux_exit_code_grace_seconds: int,
) -> tuple[list[str], list[str], dict[str, Any] | None]:
    violations: list[str] = []
    warnings: list[str] = []
    state = status.get("state")
    attempt_id = status.get("current_attempt_id")
    if not attempt_id:
        return violations, warnings, None

    attempt_path = task_dir / "attempts" / str(attempt_id) / "ATTEMPT.json"
    if not attempt_path.exists():
        violations.append(f"{task_dir.name}: ATTEMPT.json missing for current_attempt_id {attempt_id}")
        return violations, warnings, None
    try:
        attempt = load_json(attempt_path)
    except json.JSONDecodeError as exc:
        violations.append(f"{task_dir.name}: invalid ATTEMPT.json for {attempt_id}: {exc}")
        return violations, warnings, None
    if not isinstance(attempt, dict):
        violations.append(f"{task_dir.name}: ATTEMPT.json must be a JSON object")
        return violations, warnings, None

    violations.extend(validate_attempt_schema(attempt, status, str(attempt_id), task_dir.name))
    runtime = attempt.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}

    attempt_state = attempt.get("state")
    if attempt_state == "created":
        started = parse_iso(attempt.get("started_at"))
        if started:
            age_minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
            if age_minutes > stale_created_minutes:
                warnings.append(f"{task_dir.name}: ATTEMPT.state created for {age_minutes:.1f} minutes")

    lock = task_dir / "LOCK"
    dispatch_lock = task_dir / ".dispatch-lock"
    if state == "running":
        dispatch_pid_alive: bool | None = None
        if attempt_state not in {"created", "running"}:
            violations.append(f"{task_dir.name}: STATUS running requires ATTEMPT.state created or running, got {attempt_state!r}")
        if not lock.exists():
            violations.append(f"{task_dir.name}: STATUS running requires LOCK")
        if not dispatch_lock.is_dir():
            violations.append(f"{task_dir.name}: STATUS running requires active .dispatch-lock")
        else:
            dispatch_attempt = dispatch_lock / "attempt_id"
            if not dispatch_attempt.exists():
                violations.append(f"{task_dir.name}: .dispatch-lock missing attempt_id")
            elif dispatch_attempt.read_text(encoding="utf-8", errors="replace").strip() != str(attempt_id):
                violations.append(f"{task_dir.name}: .dispatch-lock attempt_id does not match STATUS current_attempt_id")
            pid_path = dispatch_lock / "pid"
            if not pid_path.exists():
                violations.append(f"{task_dir.name}: .dispatch-lock missing pid while STATUS is running")
            else:
                pid_text = pid_path.read_text(encoding="utf-8", errors="replace").strip()
                try:
                    pid = int(pid_text)
                except ValueError:
                    violations.append(f"{task_dir.name}: .dispatch-lock pid is not an integer while STATUS is running: {pid_text!r}")
                else:
                    dispatch_pid_alive = pid_is_alive(pid)
                    if not dispatch_pid_alive:
                        violations.append(f"{task_dir.name}: .dispatch-lock pid is not alive while STATUS is running: {pid}")
            if runtime.get("backend") == "tmux" and attempt_state == "running":
                exit_code_path = attempt_path.parent / "exit_code"
                if exit_code_path.exists():
                    exit_code_age = (datetime.now(timezone.utc).timestamp() - exit_code_path.stat().st_mtime)
                    if dispatch_pid_alive is True and exit_code_age <= tmux_exit_code_grace_seconds:
                        warnings.append(
                            f"{task_dir.name}: tmux exit_code file exists while dispatch appears alive; "
                            f"handoff validation may be in progress ({exit_code_age:.1f}s old)"
                        )
                    else:
                        violations.append(
                            f"{task_dir.name}: tmux exit_code file exists while STATUS and ATTEMPT still report running"
                        )
    elif dispatch_lock.exists():
        violations.append(f"{task_dir.name}: .dispatch-lock exists while STATUS state is {state!r}")
    if state == "review":
        if attempt_state != "completed" or attempt.get("handoff_valid") is not True or attempt.get("handoff_state") != "review":
            violations.append(f"{task_dir.name}: STATUS review requires completed attempt with handoff_state=review")
        if attempt.get("exit_code") != 0:
            violations.append(f"{task_dir.name}: STATUS review requires worker exit_code=0")
        if not has_substantive_content(task_dir / "HANDOFF.md"):
            violations.append(f"{task_dir.name}: STATUS review requires substantive HANDOFF.md")
        if not has_substantive_content(task_dir / "EVIDENCE.md"):
            violations.append(f"{task_dir.name}: STATUS review requires substantive EVIDENCE.md")
        request, request_reasons = load_handoff_request(task_dir)
        for reason in request_reasons:
            violations.append(f"{task_dir.name}: handoff request invalid: {reason}")
        if isinstance(request, dict) and request.get("requested_state") != "review":
            violations.append(f"{task_dir.name}: STATUS review requires HANDOFF.json requested_state=review")
    elif state == "blocked":
        if attempt_state == "completed":
            if attempt.get("handoff_valid") is not True or attempt.get("handoff_state") != "blocked":
                violations.append(f"{task_dir.name}: completed blocked task requires handoff_state=blocked")
            request, request_reasons = load_handoff_request(task_dir)
            for reason in request_reasons:
                violations.append(f"{task_dir.name}: handoff request invalid: {reason}")
            if isinstance(request, dict) and request.get("requested_state") != "blocked":
                violations.append(f"{task_dir.name}: STATUS blocked requires HANDOFF.json requested_state=blocked")
        elif attempt_state == "invalid_handoff":
            if status.get("blocker_type") != "needs_coordinator":
                violations.append(f"{task_dir.name}: invalid_handoff blocked task requires blocker_type=needs_coordinator")
        else:
            violations.append(f"{task_dir.name}: STATUS blocked requires completed or invalid_handoff attempt")

    return violations, warnings, attempt


def validate_status(
    task_dir: Path,
    status: dict[str, Any],
    fsm: dict[str, Any],
    stale_created_minutes: float,
    tmux_exit_code_grace_seconds: int,
) -> tuple[list[str], list[str]]:
    violations: list[str] = []
    warnings: list[str] = []
    state = status.get("state")
    violations.extend(validate_status_schema(status, fsm, task_dir.name))
    violations.extend(validate_state_history(status, fsm, task_dir.name))

    evidence = status.get("evidence")
    if isinstance(evidence, dict):
        logs = evidence.get("logs", [])
        if isinstance(logs, list):
            for log_ref in logs:
                log_path = task_dir / str(log_ref)
                if not log_path.exists():
                    violations.append(f"{task_dir.name}: evidence log missing: {log_ref}")

    if state in {"approved", "merged"} and not has_substantive_content(task_dir / "EVIDENCE.md"):
        violations.append(f"{task_dir.name}: {state} task has missing or template-only EVIDENCE.md")

    attempt_id = status.get("current_attempt_id")
    if attempt_id and not (task_dir / "attempts" / str(attempt_id)).exists():
        violations.append(f"{task_dir.name}: current_attempt_id directory missing: {attempt_id}")
    attempt_violations, attempt_warnings, _ = validate_attempt(
        task_dir,
        status,
        stale_created_minutes,
        tmux_exit_code_grace_seconds,
    )
    violations.extend(attempt_violations)
    warnings.extend(attempt_warnings)

    lock = task_dir / "LOCK"
    if lock.exists() and attempt_id:
        text = lock.read_text(encoding="utf-8", errors="replace")
        if f"attempt_id: {attempt_id}" not in text:
            violations.append(f"{task_dir.name}: LOCK attempt_id does not match STATUS current_attempt_id")

    return violations, warnings


def collect(
    run_id: str,
    stale_lock_hours: float,
    stale_created_minutes: float = 10.0,
    tmux_exit_code_grace_seconds: int = 60,
    config_warnings: list[str] | None = None,
    config_errors: list[str] | None = None,
) -> dict[str, Any]:
    root = repo_root(Path.cwd())
    run_dir = root / ".agent-collab" / "runs" / run_id
    if not run_dir.exists():
        raise SystemExit(f"Run not found: {run_dir}")

    fsm = load_fsm()
    tasks: list[dict[str, Any]] = []
    violations: list[str] = []
    warnings: list[str] = []
    stale_locks: list[str] = []
    stale_dispatch_locks: list[str] = []
    invalid_status_files: list[str] = []
    now_ts = datetime.now(timezone.utc).timestamp()

    events, event_violations, event_warnings = load_events(run_dir, run_id)
    violations.extend(event_violations)
    warnings.extend(event_warnings)
    warnings.extend(f"config: {warning}" for warning in (config_warnings or []))
    violations.extend(f"config: {error}" for error in (config_errors or []))
    run_json_path = run_dir / "RUN.json"
    if not run_json_path.exists():
        violations.append("run: missing required RUN.json")
    else:
        try:
            run_json = load_json(run_json_path)
        except json.JSONDecodeError as exc:
            violations.append(f"run: invalid RUN.json: {exc}")
        else:
            recorded_package = run_json.get("package_version")
            if not recorded_package:
                warnings.append("run: RUN.json package_version missing; run may have been created by an older package")
            elif recorded_package != PACKAGE_VERSION:
                warnings.append(
                    f"run: RUN.json package_version {recorded_package!r} differs from installed package version {PACKAGE_VERSION!r}"
                )
            recorded_protocol = run_json.get("protocol_version")
            if recorded_protocol != PROTOCOL_VERSION:
                warnings.append(
                    f"run: RUN.json protocol_version {recorded_protocol!r} differs from installed {PROTOCOL_VERSION!r}"
                )
    if not (run_dir / "JOURNAL.md").exists():
        violations.append("run: missing required JOURNAL.md")

    for task_dir in sorted((run_dir / "tasks").glob("*")):
        if not task_dir.is_dir():
            continue
        status_path = task_dir / "STATUS.json"
        if not status_path.exists():
            invalid_status_files.append(str(status_path))
            violations.append(f"{task_dir.name}: missing STATUS.json")
            continue
        try:
            status = load_json(status_path)
        except json.JSONDecodeError as exc:
            invalid_status_files.append(str(status_path))
            violations.append(f"{task_dir.name}: invalid STATUS.json: {exc}")
            continue
        if not isinstance(status, dict):
            invalid_status_files.append(str(status_path))
            violations.append(f"{task_dir.name}: STATUS.json must be a JSON object")
            tasks.append(
                {
                    "task_id": task_dir.name,
                    "state": None,
                    "owner": None,
                    "branch": None,
                    "worktree": None,
                    "current_attempt_id": None,
                    "needs_coordinator": None,
                    "blocker_type": None,
                    "blocking_reason": None,
                    "summary": None,
                    "lock": None,
                    "dispatch_lock": None,
                    "handoff_index": None,
                }
            )
            continue

        status_violations, status_warnings = validate_status(
            task_dir,
            status,
            fsm,
            stale_created_minutes,
            tmux_exit_code_grace_seconds,
        )
        violations.extend(status_violations)
        warnings.extend(status_warnings)

        lock = task_dir / "LOCK"
        lock_info = None
        if lock.exists():
            age_hours = (now_ts - lock.stat().st_mtime) / 3600
            lock_info = {"path": str(lock), "age_hours": round(age_hours, 2)}
            if age_hours > stale_lock_hours:
                stale_locks.append(str(lock))
        dispatch_lock = task_dir / ".dispatch-lock"
        dispatch_lock_info = None
        if dispatch_lock.exists():
            age_hours = (now_ts - dispatch_lock.stat().st_mtime) / 3600
            dispatch_lock_info = {"path": str(dispatch_lock), "age_hours": round(age_hours, 2)}
            if age_hours > stale_lock_hours:
                stale_dispatch_locks.append(str(dispatch_lock))
            pid_path = dispatch_lock / "pid"
            if not pid_path.exists():
                if status.get("state") != "running":
                    warnings.append(f"{task_dir.name}: .dispatch-lock missing pid")
            else:
                pid_text = pid_path.read_text(encoding="utf-8", errors="replace").strip()
                try:
                    pid = int(pid_text)
                except ValueError:
                    if status.get("state") != "running":
                        warnings.append(f"{task_dir.name}: .dispatch-lock pid is not an integer: {pid_text!r}")
                else:
                    if not pid_is_alive(pid):
                        if status.get("state") != "running":
                            warnings.append(f"{task_dir.name}: .dispatch-lock pid is not alive: {pid}")

        handoff_index, handoff_warnings = load_handoff_index(task_dir)
        warnings.extend(handoff_warnings)

        tasks.append(
            {
                "task_id": status.get("task_id", task_dir.name),
                "state": status.get("state"),
                "owner": status.get("owner"),
                "branch": status.get("branch"),
                "worktree": status.get("worktree"),
                "current_attempt_id": status.get("current_attempt_id"),
                "needs_coordinator": status.get("needs_coordinator"),
                "blocker_type": status.get("blocker_type"),
                "blocking_reason": status.get("blocking_reason"),
                "summary": status.get("summary"),
                "handoff_index": handoff_index,
                "lock": lock_info,
                "dispatch_lock": dispatch_lock_info,
            }
        )

    counts = dict(Counter(task["state"] for task in tasks))
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "collected_at": utc_now(),
        "valid": not violations and not invalid_status_files,
        "counts": counts,
        "tasks": tasks,
        "blocked": [task for task in tasks if task["state"] == "blocked"],
        "ready_for_review": [task for task in tasks if task["state"] == "review"],
        "invalid_status_files": invalid_status_files,
        "stale_locks": stale_locks,
        "stale_dispatch_locks": stale_dispatch_locks,
        "protocol_violations": violations,
        "protocol_warnings": warnings,
        "recent_events": events[-10:],
    }


def render_human(report: dict[str, Any]) -> str:
    lines = [
        f"Run: {report['run_id']}",
        f"Collected: {report['collected_at']}",
        f"Valid: {report['valid']}",
        "",
        "Counts:",
    ]
    if report["counts"]:
        for state, count in sorted(report["counts"].items()):
            lines.append(f"  {state}: {count}")
    else:
        lines.append("  no tasks")

    lines.append("")
    lines.append("Tasks:")
    for task in report["tasks"]:
        blocker = f" blocker={task['blocker_type']}" if task.get("blocker_type") else ""
        handoff = ""
        if isinstance(task.get("handoff_index"), dict) and not task["handoff_index"].get("template"):
            handoff = " handoff_json=yes"
        lock = " lock=yes" if task.get("lock") else ""
        dispatch_lock = " dispatch_lock=yes" if task.get("dispatch_lock") else ""
        lines.append(
            f"  {task['task_id']}: {task['state']} attempt={task.get('current_attempt_id')}"
            f"{blocker}{handoff}{lock}{dispatch_lock}"
        )

    if report["protocol_violations"]:
        lines.append("")
        lines.append("Protocol violations:")
        for violation in report["protocol_violations"]:
            lines.append(f"  - {violation}")
    if report["protocol_warnings"]:
        lines.append("")
        lines.append("Protocol warnings:")
        for warning in report["protocol_warnings"]:
            lines.append(f"  - {warning}")

    if report["stale_locks"]:
        lines.append("")
        lines.append("Stale locks:")
        for lock in report["stale_locks"]:
            lines.append(f"  - {lock}")
    if report["stale_dispatch_locks"]:
        lines.append("")
        lines.append("Stale dispatch locks:")
        for lock in report["stale_dispatch_locks"]:
            lines.append(f"  - {lock}")

    if report["recent_events"]:
        lines.append("")
        lines.append("Recent events:")
        for event in report["recent_events"][-5:]:
            lines.append(f"  - {event.get('at', '')} {event.get('event', '')} {event.get('task_id', '')}")

    return "\n".join(lines) + "\n"


def render_summary(report: dict[str, Any]) -> str:
    rows = ["| Task | State | Owner | Attempt | Handoff | Blocker | Review |", "|---|---|---|---|---|---|---|"]
    for task in report["tasks"]:
        review = "ready" if task["state"] == "review" else ""
        handoff_summary = ""
        if isinstance(task.get("handoff_index"), dict) and not task["handoff_index"].get("template"):
            handoff_summary = str(task["handoff_index"].get("summary") or "")[:80]
        rows.append(
            f"| {task['task_id']} | {task['state']} | {task.get('owner') or ''} | "
            f"{task.get('current_attempt_id') or ''} | {handoff_summary} | {task.get('blocker_type') or ''} | {review} |"
        )

    blockers = ["| Task | Type | Reason |", "|---|---|---|"]
    for task in report["blocked"]:
        blockers.append(f"| {task['task_id']} | {task.get('blocker_type') or ''} | {task.get('blocking_reason') or ''} |")

    warnings = report["protocol_violations"] or ["None"]
    protocol_warnings = report["protocol_warnings"] or ["None"]
    return "\n".join(
        [
            "# Run Summary",
            "",
            "## Objective",
            "",
            "See `RUN.json`.",
            "",
            "## Current Status",
            "",
            f"- Collected: {report['collected_at']}",
            f"- Valid: {report['valid']}",
            f"- Counts: {report['counts']}",
            "",
            "## Task Board",
            "",
            *rows,
            "",
            "## Active Blockers",
            "",
            *blockers,
            "",
            "## Ready For Codex Review",
            "",
            *(f"- {task['task_id']}" for task in report["ready_for_review"]),
            "",
            "## Protocol Warnings",
            "",
            *(f"- {warning}" for warning in warnings),
            "",
            "## Protocol Non-Fatal Warnings",
            "",
            *(f"- {warning}" for warning in protocol_warnings),
            "",
            "## Recent Decisions",
            "",
            "## Recent Events",
            "",
            *(f"- {event.get('at', '')} `{event.get('event', '')}` {event.get('task_id', '')}" for event in report["recent_events"][-10:]),
            "",
            "## Experiment Results",
            "",
            "See `RESULT_LEDGER.md`.",
            "",
            "## Next Actions",
            "",
        ]
    )


def write_diagnostics(report: dict[str, Any]) -> None:
    run_dir = Path(report["run_dir"])
    diagnostics = run_dir / "diagnostics"
    diagnostics.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = diagnostics / f"collect-status-{stamp}.json"
    md_path = diagnostics / f"collect-status-{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_human(report), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect run status without mutating task state.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--write-summary", action="store_true", help="Write derived SUMMARY.md.")
    parser.add_argument("--write-diagnostics", action="store_true", help="Write derived diagnostics files.")
    parser.add_argument("--stale-lock-hours", type=float, default=None)
    parser.add_argument("--stale-created-minutes", type=float, default=None)
    args = parser.parse_args()

    root = repo_root(Path.cwd())
    config_result = load_config(root)
    config = config_result.config
    stale_lock_hours = args.stale_lock_hours if args.stale_lock_hours is not None else config.stale_lock_hours
    stale_created_minutes = (
        args.stale_created_minutes if args.stale_created_minutes is not None else config.stale_created_minutes
    )
    report = collect(
        args.run_id,
        stale_lock_hours,
        stale_created_minutes,
        config.tmux_exit_code_grace_seconds,
        config_result.warnings,
        config_result.errors,
    )

    if args.write_summary:
        Path(report["run_dir"], "SUMMARY.md").write_text(render_summary(report), encoding="utf-8")
    if args.write_diagnostics:
        write_diagnostics(report)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_human(report), end="")

    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
