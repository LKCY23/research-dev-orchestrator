#!/usr/bin/env python3
"""Close a work session by updating SUMMARY.md and appending JOURNAL/EVENTS."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from collect_status import collect, render_summary  # noqa: E402


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


def append_event(run_dir: Path, event: dict) -> None:
    with (run_dir / "EVENTS.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def bullet_lines(values: list[str]) -> str:
    if not values:
        return "- None"
    return "\n".join(f"- {value}" for value in values)


def main() -> int:
    parser = argparse.ArgumentParser(description="Update long-term run memory at session close.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--summary", required=True, help="One concise sentence describing this session.")
    parser.add_argument("--changed", action="append", default=[], help="What changed; repeatable.")
    parser.add_argument("--decision", action="append", default=[], help="Important session decision or tradeoff; repeatable.")
    parser.add_argument("--next-action", action="append", default=[], help="Next action; repeatable.")
    parser.add_argument("--experiment", action="append", default=[], help="Experiment result note; repeatable.")
    parser.add_argument("--actor", default="codex")
    args = parser.parse_args()

    root = repo_root(Path.cwd())
    run_dir = root / ".agent-collab" / "runs" / args.run_id
    if not run_dir.exists():
        raise SystemExit(f"Run not found: {run_dir}")

    report = collect(args.run_id, stale_lock_hours=6.0)
    (run_dir / "SUMMARY.md").write_text(render_summary(report), encoding="utf-8")

    now = utc_now()
    journal_entry = f"""
## {now} Session

### Summary

{args.summary}

### What Changed

{bullet_lines(args.changed)}

### Decisions / Tradeoffs

{bullet_lines(args.decision)}

### Experiment Notes

{bullet_lines(args.experiment)}

### Current State

- Valid protocol state: {report["valid"]}
- Task counts: {report["counts"]}
- Ready for review: {", ".join(task["task_id"] for task in report["ready_for_review"]) or "None"}
- Blocked: {", ".join(task["task_id"] for task in report["blocked"]) or "None"}

### Next Actions

{bullet_lines(args.next_action)}
"""
    with (run_dir / "JOURNAL.md").open("a", encoding="utf-8") as handle:
        handle.write(journal_entry)

    append_event(
        run_dir,
        {
            "at": now,
            "actor": args.actor,
            "event": "session_closed",
            "run_id": args.run_id,
            "summary": args.summary,
            "valid": report["valid"],
            "counts": report["counts"],
        },
    )

    print(f"Updated SUMMARY.md, JOURNAL.md, and EVENTS.ndjson for {args.run_id}")
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
