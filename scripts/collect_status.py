#!/usr/bin/env python3
"""Collect and validate orchestration run status without mutating protocol state."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_STATUS_FIELDS = {
    "task_id",
    "state",
    "previous_state",
    "owner",
    "branch",
    "worktree",
    "updated_at",
    "needs_codex",
    "summary",
    "blocking_reason",
    "blocker_type",
    "current_attempt_id",
    "assigned_worker",
    "evidence",
    "state_history",
}

BLOCKER_TYPES = {"needs_codex", "needs_user", "environment", "budget", "irrecoverable"}
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


def load_recent_events(run_dir: Path, limit: int = 10) -> list[dict[str, Any]]:
    events_path = run_dir / "EVENTS.ndjson"
    if not events_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"event": "invalid_event_line", "raw": line})
    return events[-limit:]


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_fsm() -> dict[str, Any]:
    return load_json(skill_root() / "references" / "state-machine.json")


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


def validate_status(task_dir: Path, status: dict[str, Any], fsm: dict[str, Any]) -> list[str]:
    violations: list[str] = []
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

    if state in {"review", "approved", "merged"} and not has_substantive_content(task_dir / "EVIDENCE.md"):
        violations.append(f"{task_dir.name}: {state} task has missing or template-only EVIDENCE.md")

    attempt_id = status.get("current_attempt_id")
    if state in {"running", "blocked", "review", "approved", "merged"} and not attempt_id:
        violations.append(f"{task_dir.name}: {state} task is missing current_attempt_id")
    if attempt_id and not (task_dir / "attempts" / str(attempt_id)).exists():
        violations.append(f"{task_dir.name}: current_attempt_id directory missing: {attempt_id}")

    lock = task_dir / "LOCK"
    if lock.exists() and attempt_id:
        text = lock.read_text(encoding="utf-8", errors="replace")
        if f"attempt_id: {attempt_id}" not in text:
            violations.append(f"{task_dir.name}: LOCK attempt_id does not match STATUS current_attempt_id")

    return violations


def collect(run_id: str, stale_lock_hours: float) -> dict[str, Any]:
    root = repo_root(Path.cwd())
    run_dir = root / ".agent-collab" / "runs" / run_id
    if not run_dir.exists():
        raise SystemExit(f"Run not found: {run_dir}")

    fsm = load_fsm()
    tasks: list[dict[str, Any]] = []
    violations: list[str] = []
    stale_locks: list[str] = []
    invalid_status_files: list[str] = []
    now_ts = datetime.now(timezone.utc).timestamp()

    if not (run_dir / "EVENTS.ndjson").exists():
        violations.append("run: missing required EVENTS.ndjson")
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

        violations.extend(validate_status(task_dir, status, fsm))

        lock = task_dir / "LOCK"
        lock_info = None
        if lock.exists():
            age_hours = (now_ts - lock.stat().st_mtime) / 3600
            lock_info = {"path": str(lock), "age_hours": round(age_hours, 2)}
            if age_hours > stale_lock_hours:
                stale_locks.append(str(lock))

        tasks.append(
            {
                "task_id": status.get("task_id", task_dir.name),
                "state": status.get("state"),
                "owner": status.get("owner"),
                "branch": status.get("branch"),
                "worktree": status.get("worktree"),
                "current_attempt_id": status.get("current_attempt_id"),
                "needs_codex": status.get("needs_codex"),
                "blocker_type": status.get("blocker_type"),
                "blocking_reason": status.get("blocking_reason"),
                "summary": status.get("summary"),
                "lock": lock_info,
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
        "protocol_violations": violations,
        "recent_events": load_recent_events(run_dir),
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
        lines.append(f"  {task['task_id']}: {task['state']} attempt={task.get('current_attempt_id')}{blocker}{lock}")

    if report["protocol_violations"]:
        lines.append("")
        lines.append("Protocol violations:")
        for violation in report["protocol_violations"]:
            lines.append(f"  - {violation}")

    if report["stale_locks"]:
        lines.append("")
        lines.append("Stale locks:")
        for lock in report["stale_locks"]:
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
    args = parser.parse_args()

    report = collect(args.run_id, args.stale_lock_hours)

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
