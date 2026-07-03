#!/usr/bin/env python3
"""Initialize a research-dev-orchestrator run scaffold."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PROTOCOL_VERSION = "research-dev-orchestrator/v0.1"


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


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "run"


def write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def append_event(run_dir: Path, event: dict) -> None:
    events_path = run_dir / "EVENTS.ndjson"
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an orchestration run scaffold.")
    parser.add_argument("--project-slug", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--target-branch", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--coordinator-session-id", default="")
    parser.add_argument("--force", action="store_true", help="Allow using an existing run directory.")
    args = parser.parse_args()

    root = repo_root(Path.cwd())
    project_slug = slugify(args.project_slug)
    created_at = utc_now()
    shortid = secrets.token_hex(3)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = args.run_id or f"{timestamp}-{project_slug}-{shortid}"

    target_branch = args.target_branch or run_git(["branch", "--show-current"], root, "main")
    base_commit = run_git(["rev-parse", "HEAD"], root, "")

    run_dir = root / ".agent-collab" / "runs" / run_id
    if run_dir.exists() and not args.force:
        raise SystemExit(f"Run already exists: {run_dir}")

    for directory in [
        run_dir,
        run_dir / "ADR",
        run_dir / "tasks",
        run_dir / "reviews",
        run_dir / "final",
        run_dir / "diagnostics",
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    session_id = args.coordinator_session_id or f"codex-{secrets.token_hex(3)}"
    run_json = {
        "run_id": run_id,
        "protocol_version": PROTOCOL_VERSION,
        "created_at": created_at,
        "project_slug": project_slug,
        "objective": args.objective,
        "target_branch": target_branch,
        "base_commit": base_commit,
        "coordinator_sessions": [
            {
                "agent": "codex",
                "role": "coordinator",
                "session_id": session_id,
                "started_at": created_at,
            }
        ],
    }
    (run_dir / "RUN.json").write_text(json.dumps(run_json, indent=2) + "\n", encoding="utf-8")
    write_if_missing(run_dir / "EVENTS.ndjson", "")
    append_event(
        run_dir,
        {
            "at": created_at,
            "actor": "codex",
            "event": "run_created",
            "run_id": run_id,
            "project_slug": project_slug,
            "target_branch": target_branch,
            "base_commit": base_commit,
        },
    )

    write_if_missing(
        run_dir / "SUMMARY.md",
        f"""# Run Summary

## Objective

{args.objective}

## Current Status

Initialized. This file is derived and may be regenerated.

## Task Board

| Task | State | Owner | Attempt | Blocker | Review |
|---|---|---|---|---|---|

## Active Blockers

## Ready For Codex Review

## Protocol Warnings

## Recent Decisions

## Recent Events

## Experiment Results

## Next Actions
""",
    )

    scaffolds = {
        "REQUIREMENTS.md": "# Requirements\n\n## Objective\n\n## Research Questions\n\n## Hypotheses\n\n## In Scope\n\n## Out of Scope\n\n## Datasets / Inputs\n\n## Baselines\n\n## Metrics\n\n## Constraints\n\n## Acceptance Criteria\n\n## Open Questions\n",
        "DESIGN_METHOD_SELECTION.md": "# Design Method Selection\n\n## Problem Type\n\n## Candidate Styles\n\n## Selected Style\n\n## Decomposition Strategy\n\n## Data Flow Style\n\n## Interface Style\n\n## Testing Strategy\n\n## Experiment Tracking Strategy\n\n## Alternatives Considered\n\n## Decision\n\n## Risks\n",
        "DESIGN_BRIEF.md": "# Design Brief\n\n## Overview\n\n## Architecture\n\n## Data Flow\n\n## Interfaces\n\n## Testing Strategy\n\n## Open Questions\n",
        "EXPERIMENT_PLAN.md": "# Experiment Plan\n\n## Hypotheses\n\n## Claims To Support\n\n## Datasets\n\n## Data Splits\n\n## Baselines\n\n## Methods / Variants\n\n## Metrics\n\n## Ablations\n\n## Expected Outputs\n\n## Minimum Viable Smoke Test\n\n## Success Criteria\n\n## Risks And Confounders\n",
        "REPRODUCIBILITY.md": "# Reproducibility\n\n## Environment\n\n## Dependencies\n\n## Hardware Notes\n\n## Random Seeds\n\n## Data Versions\n\n## Commands\n\n## Expected Outputs\n\n## Log Locations\n\n## Known Sources Of Nondeterminism\n\n## Reproduction Checklist\n",
        "RESULT_LEDGER.md": "# Result Ledger\n\n| Time | Task | Attempt | Command | Metric | Result | Supports Claim | Logs | Notes |\n|---|---|---|---|---|---|---|---|---|\n",
        "TASKS.md": "# Tasks\n\n| Task | Goal | State | Dependencies | Notes |\n|---|---|---|---|---|\n",
        "JOURNAL.md": "# Journal\n\nAppend one concise entry at the end of every working session.\n",
    }
    for filename, content in scaffolds.items():
        write_if_missing(run_dir / filename, content)

    print(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
