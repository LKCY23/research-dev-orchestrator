#!/usr/bin/env python3
"""Initialize a research-dev-orchestrator run scaffold."""

from __future__ import annotations

import argparse
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from protocol import PACKAGE_VERSION, PROTOCOL_VERSION, append_event, render_template, repo_root, run_git, utc_now


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "run"


def write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an orchestration run scaffold.")
    parser.add_argument("--project-slug", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--target-branch", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--coordinator-backend", default="codex")
    parser.add_argument("--coordinator-agent-name", default="codex-main")
    parser.add_argument("--coordinator-session-id", default="")
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
    if run_dir.exists():
        raise SystemExit(f"Run already exists: {run_dir}")

    collab_dir = root / ".agent-collab"
    collab_dir.mkdir(parents=True, exist_ok=True)
    write_if_missing(collab_dir / "rdo.toml", render_template("run/rdo.toml"))

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
        "package_version": PACKAGE_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "created_at": created_at,
        "project_slug": project_slug,
        "objective": args.objective,
        "target_branch": target_branch,
        "base_commit": base_commit,
        "coordinator_sessions": [
            {
                "role": "coordinator",
                "backend_id": args.coordinator_backend,
                "agent_name": args.coordinator_agent_name,
                "backend_session_id": session_id,
                "session_id": session_id,
                "notification_mode": "none",
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
            "actor": "coordinator",
            "event": "run_created",
            "run_id": run_id,
            "backend_id": args.coordinator_backend,
            "project_slug": project_slug,
            "target_branch": target_branch,
            "base_commit": base_commit,
        },
    )

    run_templates = [
        "SUMMARY.md",
        "REQUIREMENTS.md",
        "DESIGN_METHOD_SELECTION.md",
        "DESIGN_BRIEF.md",
        "EXPERIMENT_PLAN.md",
        "REPRODUCIBILITY.md",
        "RESULT_LEDGER.md",
        "TASKS.md",
        "JOURNAL.md",
    ]
    for filename in run_templates:
        write_if_missing(run_dir / filename, render_template(f"run/{filename}", {"OBJECTIVE": args.objective}))

    print(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
