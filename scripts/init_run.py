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

from protocol import PACKAGE_VERSION, PROTOCOL_VERSION, append_event, render_template, repo_root, run_git, utc_now


LOCAL_STATE_DIRECTORIES = (".agent-collab", ".agent-worktrees")
LOCAL_EXCLUDE_RULES = tuple(f"/{directory}/" for directory in LOCAL_STATE_DIRECTORIES)
LOCAL_EXCLUDE_MARKER = "# research-dev-orchestrator local state"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "run"


def write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def run_git_checked(args: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "unknown Git error"
        raise SystemExit(f"Git command failed ({' '.join(args)}): {detail}")
    return process


def ensure_local_state_ignored(root: Path) -> Path:
    tracked = run_git_checked(
        ["ls-files", "--", *LOCAL_STATE_DIRECTORIES],
        root,
    ).stdout.splitlines()
    if tracked:
        preview = "\n".join(f"  - {path}" for path in tracked[:10])
        remainder = len(tracked) - 10
        if remainder > 0:
            preview += f"\n  - ... and {remainder} more"
        raise SystemExit(
            "RDO local state is already tracked by Git:\n"
            f"{preview}\n"
            "Remove it from the index before initializing, for example:\n"
            "  git rm -r --cached --ignore-unmatch .agent-collab .agent-worktrees"
        )

    git_path = run_git_checked(
        ["rev-parse", "--git-path", "info/exclude"],
        root,
    ).stdout.strip()
    if not git_path:
        raise SystemExit("Git did not resolve info/exclude for the target repository")
    exclude_path = Path(git_path)
    if not exclude_path.is_absolute():
        exclude_path = root / exclude_path
    exclude_path.parent.mkdir(parents=True, exist_ok=True)

    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    existing_lines = existing.splitlines()
    missing_rules = [rule for rule in LOCAL_EXCLUDE_RULES if rule not in existing_lines]
    if missing_rules:
        additions: list[str] = []
        if LOCAL_EXCLUDE_MARKER not in existing_lines:
            additions.append(LOCAL_EXCLUDE_MARKER)
        additions.extend(missing_rules)
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude_path.write_text(
            existing + separator + "\n".join(additions) + "\n",
            encoding="utf-8",
        )

    for directory in LOCAL_STATE_DIRECTORIES:
        probe = f"{directory}/.rdo-ignore-probe"
        verification = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", "--", probe],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        if verification.returncode != 0:
            raise SystemExit(
                f"RDO local-state ignore verification failed for {directory}. "
                f"Check {exclude_path} and higher-precedence .gitignore rules."
            )
    return exclude_path


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
    ensure_local_state_ignored(root)
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
