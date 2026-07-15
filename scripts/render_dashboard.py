#!/usr/bin/env python3
"""Render a static human monitor dashboard for an orchestration run."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from collect_status import collect
from config import load_config
from protocol import repo_root


STATE_CLASS = {
    "pending": "muted",
    "running": "active",
    "review": "review",
    "blocked": "blocked",
    "changes_requested": "blocked",
    "approved": "ok",
    "merged": "ok",
    "failed": "bad",
}


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def rel_link(run_dir: Path, path: str | Path, label: str) -> str:
    target = Path(path)
    if target.is_absolute():
        try:
            target = target.relative_to(run_dir)
        except ValueError:
            pass
    return f'<a href="{esc(target.as_posix())}">{esc(label)}</a>'


def render_task_card(run_dir: Path, task: dict[str, Any]) -> str:
    task_id = str(task.get("task_id") or "")
    state = task.get("state") or "unknown"
    css = STATE_CLASS.get(str(state), "muted")
    task_dir = run_dir / "tasks" / task_id
    handoff = task.get("handoff_index")
    handoff_summary = ""
    handoff_meta = ""
    if isinstance(handoff, dict) and not handoff.get("template"):
        handoff_summary = str(handoff.get("summary") or "")
        requested_state = handoff.get("requested_state") or ""
        if requested_state:
            handoff_meta = f"<span>requested: {esc(requested_state)}</span>"

    blocker = ""
    if task.get("blocker_type") or task.get("blocking_reason"):
        blocker = (
            '<div class="blocker">'
            f"<strong>{esc(task.get('blocker_type') or 'blocked')}</strong>"
            f"<p>{esc(task.get('blocking_reason') or '')}</p>"
            "</div>"
        )

    artifact_resolution = task.get("artifact_resolution")
    protocol_label = "legacy-v0.5"
    artifact_links = ""
    artifact_error = ""
    if isinstance(artifact_resolution, dict):
        protocol_label = str(
            artifact_resolution.get("protocol")
            or artifact_resolution.get("artifact_protocol_version")
            or "unknown"
        )
        if artifact_resolution.get("valid") is False:
            artifact_error = (
                '<div class="blocker"><strong>artifact invalid</strong>'
                f"<p>{esc(artifact_resolution.get('error') or '')}</p></div>"
            )
        refs = artifact_resolution.get("artifact_refs")
        if artifact_resolution.get("valid") is True and isinstance(refs, dict):
            labels = (
                ("attempt", "ATTEMPT"),
                ("task_inputs", "TASK_INPUTS"),
                ("evidence", "EVIDENCE.json"),
                ("handoff", "HANDOFF.json"),
                ("handoff_ready", "HANDOFF_READY"),
                ("commands", "COMMANDS"),
                ("evidence_markdown", "EVIDENCE"),
                ("handoff_markdown", "HANDOFF"),
                ("handoff_json", "HANDOFF.json"),
            )
            artifact_links = "\n".join(
                rel_link(run_dir, refs[key], label)
                for key, label in labels
                if isinstance(refs.get(key), str)
            )
    if not artifact_links and protocol_label.startswith("legacy"):
        artifact_links = "\n".join(
            (
                rel_link(run_dir, task_dir / "EVIDENCE.md", "EVIDENCE"),
                rel_link(run_dir, task_dir / "HANDOFF.md", "HANDOFF"),
                rel_link(run_dir, task_dir / "HANDOFF.json", "HANDOFF.json"),
            )
        )

    return f"""
      <article class="task-card">
        <div class="task-head">
          <h3>{rel_link(run_dir, task_dir / "TASK.md", task_id)}</h3>
          <span class="pill {css}">{esc(state)}</span>
        </div>
        <p class="summary">{esc(task.get("summary") or handoff_summary or "No summary yet.")}</p>
        <div class="meta">
          <span>attempt: {esc(task.get("current_attempt_id") or "-")}</span>
          <span>owner: {esc(task.get("owner") or "-")}</span>
          <span>artifacts: {esc(protocol_label)}</span>
          {handoff_meta}
        </div>
        {blocker}
        {artifact_error}
        <div class="links">
          {rel_link(run_dir, task_dir / "STATUS.json", "STATUS")}
          {rel_link(run_dir, task_dir / "ACCEPTANCE.md", "ACCEPTANCE")}
          {artifact_links}
        </div>
      </article>
    """


def render_list(items: list[Any], empty: str) -> str:
    if not items:
        return f"<p class=\"empty\">{esc(empty)}</p>"
    return "<ul>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ul>"


def render_event(event: dict[str, Any]) -> str:
    return (
        "<li>"
        f"<span>{esc(event.get('at', ''))}</span> "
        f"<strong>{esc(event.get('event', ''))}</strong> "
        f"{esc(event.get('task_id', ''))}"
        "</li>"
    )


def render_dashboard(report: dict[str, Any]) -> str:
    run_dir = Path(report["run_dir"])
    counts = report.get("counts", {})
    cards = "\n".join(render_task_card(run_dir, task) for task in report.get("tasks", []))
    count_items = "".join(
        f'<div class="count"><span>{esc(state)}</span><strong>{esc(count)}</strong></div>'
        for state, count in sorted(counts.items())
    )
    ready_items = [
        f"{task.get('task_id')} ({task.get('current_attempt_id') or 'no attempt'})"
        for task in report.get("ready_for_review", [])
    ]
    blocked_items = [
        f"{task.get('task_id')}: {task.get('blocker_type') or ''} {task.get('blocking_reason') or ''}".strip()
        for task in report.get("blocked", [])
    ]
    recent_events = "".join(render_event(event) for event in report.get("recent_events", [])[-10:])
    raw_json = esc(json.dumps(report, indent=2))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Run Dashboard - {esc(report.get("run_id"))}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f8fafc;
      --panel: #ffffff;
      --text: #0f172a;
      --muted: #64748b;
      --border: #d8dee8;
      --blue: #2563eb;
      --green: #059669;
      --amber: #d97706;
      --red: #e11d48;
      --slate: #475569;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: flex-start;
      margin-bottom: 24px;
    }}
    h1, h2, h3 {{ margin: 0; }}
    h1 {{ font-size: 30px; letter-spacing: 0; }}
    h2 {{ font-size: 18px; margin-bottom: 12px; }}
    a {{ color: var(--blue); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .subtle {{ color: var(--muted); margin: 6px 0 0; }}
    .valid {{
      padding: 8px 12px;
      border-radius: 8px;
      background: #ecfdf5;
      color: #064e3b;
      border: 1px solid #a7f3d0;
      font-weight: 700;
    }}
    .invalid {{
      padding: 8px 12px;
      border-radius: 8px;
      background: #fff1f2;
      color: #881337;
      border: 1px solid #fecdd3;
      font-weight: 700;
    }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px;
      margin: 16px 0;
    }}
    .counts {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 10px;
    }}
    .count {{
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      background: #f8fafc;
    }}
    .count span {{ display: block; color: var(--muted); font-size: 13px; }}
    .count strong {{ font-size: 24px; }}
    .task-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 14px;
    }}
    .task-card {{
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      background: #fff;
      min-width: 0;
    }}
    .task-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 8px;
    }}
    .task-head h3 {{ font-size: 16px; overflow-wrap: anywhere; }}
    .summary {{ color: #334155; min-height: 42px; }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      color: var(--muted);
      font-size: 13px;
      margin: 10px 0;
    }}
    .links {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      font-size: 13px;
      border-top: 1px solid #e2e8f0;
      padding-top: 10px;
      margin-top: 10px;
    }}
    .pill {{
      border-radius: 999px;
      padding: 3px 9px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid currentColor;
      white-space: nowrap;
    }}
    .pill.muted {{ color: var(--slate); background: #f8fafc; }}
    .pill.active {{ color: #1d4ed8; background: #eff6ff; }}
    .pill.review {{ color: #7c3aed; background: #f5f3ff; }}
    .pill.blocked {{ color: var(--amber); background: #fffbeb; }}
    .pill.ok {{ color: var(--green); background: #ecfdf5; }}
    .pill.bad {{ color: var(--red); background: #fff1f2; }}
    .blocker {{
      border-left: 3px solid var(--amber);
      padding: 8px 10px;
      background: #fffbeb;
      margin: 10px 0;
    }}
    .blocker p {{ margin: 4px 0 0; }}
    ul {{ margin: 0; padding-left: 20px; }}
    li + li {{ margin-top: 4px; }}
    details pre {{
      overflow: auto;
      max-height: 420px;
      background: #0f172a;
      color: #e2e8f0;
      padding: 14px;
      border-radius: 8px;
    }}
    .empty {{ color: var(--muted); margin: 0; }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Run Dashboard</h1>
        <p class="subtle">{esc(report.get("run_id"))}</p>
        <p class="subtle">Collected {esc(report.get("collected_at"))}</p>
      </div>
      <div class="{ "valid" if report.get("valid") else "invalid" }">{ "VALID" if report.get("valid") else "NEEDS ATTENTION" }</div>
    </header>

    <section class="section">
      <h2>Counts</h2>
      <div class="counts">{count_items or '<p class="empty">No tasks</p>'}</div>
    </section>

    <section class="section">
      <h2>Task Board</h2>
      <div class="task-grid">{cards or '<p class="empty">No tasks</p>'}</div>
    </section>

    <section class="section">
      <h2>Ready For Review</h2>
      {render_list(ready_items, "No tasks ready for review.")}
    </section>

    <section class="section">
      <h2>Active Blockers</h2>
      {render_list(blocked_items, "No active blockers.")}
    </section>

    <section class="section">
      <h2>Protocol Violations</h2>
      {render_list(report.get("protocol_violations", []), "No protocol violations.")}
    </section>

    <section class="section">
      <h2>Protocol Warnings</h2>
      {render_list(report.get("protocol_warnings", []), "No protocol warnings.")}
    </section>

    <section class="section">
      <h2>Recent Events</h2>
      <ul>{recent_events or '<li class="empty">No events.</li>'}</ul>
    </section>

    <section class="section">
      <h2>Links</h2>
      <p>
        {rel_link(run_dir, run_dir / "SUMMARY.md", "SUMMARY.md")} ·
        {rel_link(run_dir, run_dir / "EVENTS.ndjson", "EVENTS.ndjson")} ·
        {rel_link(run_dir, run_dir / "JOURNAL.md", "JOURNAL.md")} ·
        {rel_link(run_dir, run_dir / "RESULT_LEDGER.md", "RESULT_LEDGER.md")}
      </p>
    </section>

    <section class="section">
      <details>
        <summary>Raw collect_status report</summary>
        <pre>{raw_json}</pre>
      </details>
    </section>
  </main>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a static run dashboard HTML file.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", default="", help="Output path. Defaults to <run-dir>/dashboard.html.")
    parser.add_argument("--stale-lock-hours", type=float, default=None)
    parser.add_argument("--stale-created-minutes", type=float, default=None)
    args = parser.parse_args()

    root = repo_root(Path.cwd())
    config_result = load_config(root)
    config = config_result.config
    report = collect(
        args.run_id,
        args.stale_lock_hours if args.stale_lock_hours is not None else config.stale_lock_hours,
        args.stale_created_minutes if args.stale_created_minutes is not None else config.stale_created_minutes,
        config.tmux_exit_code_grace_seconds,
        config_result.warnings,
        config_result.errors,
    )
    output = Path(args.output) if args.output else Path(report["run_dir"]) / "dashboard.html"
    output.write_text(render_dashboard(report), encoding="utf-8")
    print(output)
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
