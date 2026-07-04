#!/usr/bin/env python3
"""Collect and validate orchestration run status without mutating protocol state."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protocol import (  # noqa: E402
    ATTEMPT_EVENTS,
    ATTEMPT_STATES,
    BLOCKER_TYPES,
    CORE_EVENTS,
    HANDOFF_STATES,
    REQUIRED_STATUS_FIELDS,
    RUNTIME_BACKENDS,
    TASK_EVENTS,
    TMUX_EXIT_CODE_GRACE_SECONDS,
    has_substantive_content,
    is_int_not_bool,
    is_non_empty_string,
    load_json,
    parse_iso,
    pid_is_alive,
    repo_root,
    utc_now,
)
from validation import validate_worker_handoff


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
        missing = [field for field in ("at", "actor", "event", "run_id") if not event.get(field)]
        if missing:
            violations.append(f"EVENTS.ndjson line {line_no}: missing required fields: {', '.join(missing)}")
        if event.get("run_id") and event.get("run_id") != run_id:
            violations.append(f"EVENTS.ndjson line {line_no}: run_id {event.get('run_id')!r} does not match {run_id!r}")
        event_name = event.get("event")
        if event_name and event_name not in CORE_EVENTS:
            warnings.append(f"EVENTS.ndjson line {line_no}: unknown event type {event_name!r}")
        if event_name in TASK_EVENTS and not event.get("task_id"):
            violations.append(f"EVENTS.ndjson line {line_no}: event {event_name!r} requires task_id")
        if event_name in ATTEMPT_EVENTS and not event.get("attempt_id"):
            violations.append(f"EVENTS.ndjson line {line_no}: event {event_name!r} requires attempt_id")
        if event.get("at") and parse_iso(event.get("at")) is None:
            violations.append(f"EVENTS.ndjson line {line_no}: invalid at timestamp {event.get('at')!r}")
        events.append(event)
    return events, violations, warnings


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_fsm() -> dict[str, Any]:
    return load_json(skill_root() / "references" / "state-machine.json")


def validate_attempt(task_dir: Path, status: dict[str, Any], stale_created_minutes: float) -> tuple[list[str], list[str], dict[str, Any] | None]:
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

    required = {
        "attempt_id",
        "task_id",
        "agent",
        "agent_name",
        "session_id",
        "state",
        "handoff_valid",
        "handoff_state",
        "started_at",
        "ended_at",
        "exit_code",
        "runtime",
    }
    missing = sorted(required - set(attempt))
    if missing:
        violations.append(f"{task_dir.name}: ATTEMPT.json missing fields: {', '.join(missing)}")
    for field in ("attempt_id", "task_id", "agent", "agent_name"):
        if field in attempt and not is_non_empty_string(attempt.get(field)):
            violations.append(f"{task_dir.name}: ATTEMPT.{field} must be a non-empty string")
    if "session_id" in attempt and not isinstance(attempt.get("session_id"), str):
        violations.append(f"{task_dir.name}: ATTEMPT.session_id must be a string")
    if attempt.get("attempt_id") != attempt_id:
        violations.append(f"{task_dir.name}: ATTEMPT.json attempt_id does not match current_attempt_id")
    if attempt.get("task_id") != status.get("task_id"):
        violations.append(f"{task_dir.name}: ATTEMPT.json task_id does not match STATUS task_id")
    if attempt.get("state") not in ATTEMPT_STATES:
        violations.append(f"{task_dir.name}: invalid ATTEMPT.state {attempt.get('state')!r}")
    if attempt.get("handoff_state") not in HANDOFF_STATES:
        violations.append(f"{task_dir.name}: invalid ATTEMPT.handoff_state {attempt.get('handoff_state')!r}")
    if parse_iso(attempt.get("started_at")) is None:
        violations.append(f"{task_dir.name}: invalid ATTEMPT.started_at {attempt.get('started_at')!r}")
    if attempt.get("ended_at") and parse_iso(attempt.get("ended_at")) is None:
        violations.append(f"{task_dir.name}: invalid ATTEMPT.ended_at {attempt.get('ended_at')!r}")
    runtime = attempt.get("runtime")
    if not isinstance(runtime, dict):
        violations.append(f"{task_dir.name}: ATTEMPT.runtime must be an object")
        runtime = {}
    for field in ("cli", "command", "cwd"):
        if field in runtime and not is_non_empty_string(runtime.get(field)):
            violations.append(f"{task_dir.name}: ATTEMPT.runtime.{field} must be a non-empty string")
        elif field not in runtime:
            violations.append(f"{task_dir.name}: ATTEMPT.runtime missing field: {field}")
    backend = runtime.get("backend")
    if backend not in RUNTIME_BACKENDS:
        violations.append(f"{task_dir.name}: ATTEMPT.runtime.backend must be plain or tmux")
    if backend == "tmux":
        for field in ("tmux_session", "attach_command"):
            if not is_non_empty_string(runtime.get(field)):
                violations.append(f"{task_dir.name}: ATTEMPT.runtime.{field} is required for tmux backend")

    attempt_state = attempt.get("state")
    if attempt_state in {"completed", "invalid_handoff"}:
        if not attempt.get("ended_at"):
            violations.append(f"{task_dir.name}: ATTEMPT.state {attempt_state} requires ended_at")
    if attempt_state == "completed":
        if not is_int_not_bool(attempt.get("exit_code")):
            violations.append(f"{task_dir.name}: completed ATTEMPT requires integer exit_code")
    if attempt_state == "invalid_handoff":
        if attempt.get("exit_code") is not None and not is_int_not_bool(attempt.get("exit_code")):
            violations.append(f"{task_dir.name}: invalid_handoff ATTEMPT requires exit_code integer or null")
    if attempt_state in {"created", "running"}:
        if attempt.get("ended_at") is not None:
            violations.append(f"{task_dir.name}: ATTEMPT.state {attempt_state} requires ended_at=null")
        if attempt.get("exit_code") is not None:
            violations.append(f"{task_dir.name}: ATTEMPT.state {attempt_state} requires exit_code=null")
    if attempt_state == "completed":
        if attempt.get("handoff_valid") is not True:
            violations.append(f"{task_dir.name}: completed ATTEMPT requires handoff_valid=true")
        if attempt.get("handoff_state") not in {"review", "blocked"}:
            violations.append(f"{task_dir.name}: completed ATTEMPT requires handoff_state review or blocked")
    if attempt_state == "invalid_handoff":
        if attempt.get("handoff_valid") is not False:
            violations.append(f"{task_dir.name}: invalid_handoff ATTEMPT requires handoff_valid=false")

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
                    if dispatch_pid_alive is True and exit_code_age <= TMUX_EXIT_CODE_GRACE_SECONDS:
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
    if state in {"review", "blocked"}:
        if attempt_state != "completed" or attempt.get("handoff_valid") is not True or attempt.get("handoff_state") != state:
            violations.append(f"{task_dir.name}: STATUS {state} requires completed attempt with handoff_state={state}")
        exit_code_raw = "" if attempt.get("exit_code") is None else str(attempt.get("exit_code"))
        handoff_result = validate_worker_handoff(status, str(attempt_id), task_dir, exit_code_raw)
        for reason in handoff_result.reasons:
            violations.append(f"{task_dir.name}: handoff validation failed: {reason}")

    return violations, warnings, attempt


def validate_status(task_dir: Path, status: dict[str, Any], fsm: dict[str, Any], stale_created_minutes: float) -> tuple[list[str], list[str]]:
    violations: list[str] = []
    warnings: list[str] = []
    missing = sorted(REQUIRED_STATUS_FIELDS - set(status))
    if missing:
        violations.append(f"{task_dir.name}: missing STATUS fields: {', '.join(missing)}")
    state = status.get("state")
    states = set(fsm.get("states", []))
    if state not in states:
        violations.append(f"{task_dir.name}: invalid state {state!r}")

    history = status.get("state_history")
    if not isinstance(history, list):
        violations.append(f"{task_dir.name}: state_history must be a list")
        history = []

    transitions = fsm.get("transitions", {})
    for idx, item in enumerate(history):
        if not isinstance(item, dict):
            violations.append(f"{task_dir.name}: state_history[{idx}] is not an object")
            continue
        from_state = item.get("from")
        to_state = item.get("to")
        actor = item.get("actor")
        allowed = transitions.get(from_state, {}).get(to_state)
        if allowed is None:
            violations.append(f"{task_dir.name}: illegal transition {from_state!r} -> {to_state!r}")
        elif actor not in allowed:
            violations.append(f"{task_dir.name}: illegal actor {actor!r} for {from_state!r} -> {to_state!r}")
        if idx == 0 and from_state != "pending":
            violations.append(f"{task_dir.name}: first state_history transition must start from 'pending'")
        if idx > 0 and isinstance(history[idx - 1], dict):
            previous_to = history[idx - 1].get("to")
            if previous_to != from_state:
                violations.append(
                    f"{task_dir.name}: non-continuous state_history at index {idx}: "
                    f"previous to={previous_to!r}, current from={from_state!r}"
                )

    if history:
        last = history[-1]
        if isinstance(last, dict):
            if state != last.get("to"):
                violations.append(f"{task_dir.name}: state {state!r} does not match last history target {last.get('to')!r}")
            if status.get("previous_state") != last.get("from"):
                violations.append(f"{task_dir.name}: previous_state does not match last history source")
    elif state != "pending":
        violations.append(f"{task_dir.name}: non-pending task has empty state_history")

    if state == "blocked":
        blocker_type = status.get("blocker_type")
        if blocker_type not in BLOCKER_TYPES:
            violations.append(f"{task_dir.name}: blocked task has invalid blocker_type {blocker_type!r}")
        if not status.get("blocking_reason"):
            violations.append(f"{task_dir.name}: blocked task has empty blocking_reason")

    evidence = status.get("evidence")
    if not isinstance(evidence, dict):
        violations.append(f"{task_dir.name}: evidence must be an object")
    else:
        logs = evidence.get("logs", [])
        if not isinstance(logs, list):
            violations.append(f"{task_dir.name}: evidence.logs must be a list")
        else:
            for log_ref in logs:
                log_path = task_dir / str(log_ref)
                if not log_path.exists():
                    violations.append(f"{task_dir.name}: evidence log missing: {log_ref}")

    if state in {"approved", "merged"} and not has_substantive_content(task_dir / "EVIDENCE.md"):
        violations.append(f"{task_dir.name}: {state} task has missing or template-only EVIDENCE.md")

    attempt_id = status.get("current_attempt_id")
    if state in {"running", "blocked", "review", "approved", "merged"} and not attempt_id:
        violations.append(f"{task_dir.name}: {state} task is missing current_attempt_id")
    if attempt_id and not (task_dir / "attempts" / str(attempt_id)).exists():
        violations.append(f"{task_dir.name}: current_attempt_id directory missing: {attempt_id}")
    attempt_violations, attempt_warnings, _ = validate_attempt(task_dir, status, stale_created_minutes)
    violations.extend(attempt_violations)
    warnings.extend(attempt_warnings)

    lock = task_dir / "LOCK"
    if lock.exists() and attempt_id:
        text = lock.read_text(encoding="utf-8", errors="replace")
        if f"attempt_id: {attempt_id}" not in text:
            violations.append(f"{task_dir.name}: LOCK attempt_id does not match STATUS current_attempt_id")

    return violations, warnings


def collect(run_id: str, stale_lock_hours: float, stale_created_minutes: float = 10.0) -> dict[str, Any]:
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

        status_violations, status_warnings = validate_status(task_dir, status, fsm, stale_created_minutes)
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
        lock = " lock=yes" if task.get("lock") else ""
        dispatch_lock = " dispatch_lock=yes" if task.get("dispatch_lock") else ""
        lines.append(f"  {task['task_id']}: {task['state']} attempt={task.get('current_attempt_id')}{blocker}{lock}{dispatch_lock}")

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
    rows = ["| Task | State | Owner | Attempt | Blocker | Review |", "|---|---|---|---|---|---|"]
    for task in report["tasks"]:
        review = "ready" if task["state"] == "review" else ""
        rows.append(
            f"| {task['task_id']} | {task['state']} | {task.get('owner') or ''} | "
            f"{task.get('current_attempt_id') or ''} | {task.get('blocker_type') or ''} | {review} |"
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
    parser.add_argument("--stale-lock-hours", type=float, default=6.0)
    parser.add_argument("--stale-created-minutes", type=float, default=10.0)
    args = parser.parse_args()

    report = collect(args.run_id, args.stale_lock_hours, args.stale_created_minutes)

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
