#!/usr/bin/env python3
"""Render dispatch-time worker assets.

This module intentionally does not mutate protocol state. It only renders
attempt-local files used by dispatch_claude.sh.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Mapping

from dependency_context import (
    DEPENDENCY_CONTEXT_REF,
    load_frozen_dependency_context,
    render_dependency_prompt_manifest,
    validate_dependency_context,
)
from task_contract import TaskContractError, parse_acceptance_markdown
from strategy import build_strategy_scaffold


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").rstrip()


def render_strategy_template(task_dir: Path, worker_backend: str) -> str:
    """Render a policy-bounded strategy skeleton so planners need no RDO source inspection."""
    template = build_strategy_scaffold(task_dir, worker_backend)
    return json.dumps(template, ensure_ascii=True, indent=2)


def load_prompt_strategy(strategy_path: str) -> dict[str, object] | None:
    """Load the exact approved strategy when rendering a Full execution prompt."""
    if not strategy_path:
        return None
    path = Path(strategy_path)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def render_full_execution_protocol(
    *, attempt_dir: Path, strategy_path: str, strategy: dict[str, object] | None
) -> tuple[list[str], str]:
    """Render an input-complete Full execution protocol without external reads."""
    rdo = Path(__file__).resolve().parent / "rdo.py"
    if strategy is None:
        return (
            [
                f"- Execute only the approved strategy at: {strategy_path}",
                f"- Use python3 {rdo} workflow start|heartbeat|complete with --attempt-dir, --workflow-id, and --instance-id.",
            ],
            "",
        )

    workflows = [
        item for item in strategy.get("workflows", [])
        if isinstance(item, dict) and isinstance(item.get("workflow_id"), str)
    ]
    carried_forward = sorted(
        str(item["workflow_id"])
        for item in workflows
        if isinstance(item.get("resume"), dict)
        and item["resume"].get("mode") == "reuse"
    )
    carried_set = set(carried_forward)
    remaining = [
        str(item["workflow_id"])
        for item in workflows
        if item["workflow_id"] not in carried_set
    ]
    lines = [
        f"- The exact approved strategy is embedded below. Do not read {strategy_path} separately.",
        "- Dispatch validates resume checkpoints before worker launch. The worker-facing resume summary is complete:",
        f"  - carried_forward_workflows = {json.dumps(carried_forward)}",
        f"  - remaining_workflows = {json.dumps(remaining)}",
        f"- Do not read {attempt_dir / 'runtime' / 'RESUME_CONTEXT.json'}; it is a dispatcher audit artifact, not an additional input.",
        "- Execute only remaining_workflows. Do not rerun carried_forward_workflows.",
        "- The workflow command forms below are complete; do not call workflow --help.",
    ]
    for item in workflows:
        workflow_id = str(item["workflow_id"])
        if workflow_id in carried_set:
            continue
        instance_id = f"{workflow_id}-I001"
        lines.extend([
            f"- {workflow_id} instance {instance_id}:",
            f"  - start: python3 {rdo} workflow start --attempt-dir {attempt_dir} --workflow-id {workflow_id} --instance-id {instance_id}",
            f"  - complete: python3 {rdo} workflow complete --attempt-dir {attempt_dir} --workflow-id {workflow_id} --instance-id {instance_id}",
        ])
    lines.extend([
        "- workflow heartbeat is optional. Use the same workflow/instance arguments only for genuinely long-running work; omit it for short workflows.",
        f"- Use python3 {rdo} exec --attempt-dir {attempt_dir} --workflow-id <id> --instance-id <id> --timeout <seconds> -- <command> only for non-acceptance workflow commands.",
        f"- Run each required acceptance command exactly once through: python3 {rdo} check --attempt-dir {attempt_dir} --check-id <id> [--workflow-id <id> --instance-id <id>]. Do not run the same acceptance argv earlier through rdo exec.",
        "- If workflow completion reports a missing acceptance record, run the missing rdo check with the same active instance, then retry complete; do not start a new instance.",
    ])
    block = "\n".join([
        "## Approved Strategy (embedded, exact)",
        "",
        "```json",
        json.dumps(strategy, ensure_ascii=True, indent=2),
        "```",
    ])
    return lines, block


def _markdown_section(text: str, heading: str, *, max_lines: int = 5) -> list[str]:
    """Return a small, deterministic excerpt from one level-two section."""

    lines = text.splitlines()
    start = next(
        (index + 1 for index, line in enumerate(lines) if line.strip() == f"## {heading}"),
        None,
    )
    if start is None:
        return []
    excerpt: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        value = line.strip()
        if not value or value.startswith("```"):
            continue
        excerpt.append(value[:500])
        if len(excerpt) == max_lines:
            break
    return excerpt


def _critical_proof_obligations(task_dir: Path) -> list[str]:
    acceptance_path = task_dir / "ACCEPTANCE.md"
    if not acceptance_path.exists():
        return ["- Complete the acceptance criteria frozen in the original task packet."]
    acceptance = acceptance_path.read_text(encoding="utf-8")
    lines = _markdown_section(acceptance, "Critical Proof Obligations")
    if not lines:
        lines = _markdown_section(acceptance, "Behavioral Checks")
    obligations = [line if line.startswith(("-", "*")) else f"- {line}" for line in lines]
    try:
        parsed = parse_acceptance_markdown(acceptance)
    except TaskContractError:
        parsed = None
    if parsed is not None:
        check_ids = [
            command["id"] for command in parsed["contract"]["required_commands"]
        ]
        obligations.append(f"- Required acceptance check IDs: {json.dumps(check_ids)}")
    return obligations or [
        "- Complete the acceptance criteria frozen in the original task packet."
    ]


def _current_source_state(worktree_path: str) -> list[str]:
    worktree = Path(worktree_path)
    if not worktree.is_dir():
        return ["- Worktree is not materialized yet; inspect it after dispatch creates it."]

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=False,
        )

    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1")
    if head.returncode != 0 or status.returncode != 0:
        return ["- Current Git state is unavailable; inspect the assigned worktree directly."]
    raw_status = status.stdout.rstrip("\n")
    entries = raw_status.splitlines() if raw_status else []
    digest = hashlib.sha256(raw_status.encode("utf-8")).hexdigest()
    result = [
        f"- HEAD: {head.stdout.strip()}",
        f"- Worktree status: {'clean' if not entries else f'{len(entries)} changed path(s)'}",
        f"- Status SHA-256: {digest}",
    ]
    result.extend(f"  - {entry[:500]}" for entry in entries[:40])
    if len(entries) > 40:
        result.append(f"  - ... {len(entries) - 40} additional path(s) omitted")
    return result


def _resume_work_summary(task_dir: Path, status: dict[str, object]) -> list[str]:
    parent_id = status.get("current_attempt_id")
    result = [
        f"- Task state at dispatch: {status.get('state') or 'unknown'}",
        f"- Parent attempt: {parent_id or 'none'}",
    ]
    safe_parent = (
        isinstance(parent_id, str)
        and parent_id not in {".", ".."}
        and Path(parent_id).name == parent_id
    )
    if safe_parent:
        assert isinstance(parent_id, str)
        attempt_path = task_dir / "attempts" / parent_id / "ATTEMPT.json"
        if attempt_path.exists():
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            result.extend(
                [
                    f"- Parent outcome: {attempt.get('outcome') or attempt.get('state') or 'unknown'}",
                    f"- Parent handoff state: {attempt.get('handoff_state') or 'none'}",
                ]
            )
        handoff_path = task_dir / "attempts" / parent_id / "HANDOFF.json"
        if handoff_path.exists():
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
            summary = str(handoff.get("summary") or "").strip()
            if summary:
                result.append(f"- Previous handoff summary: {summary[:1000]}")
    elif parent_id:
        result.append("- Parent attempt metadata was not read because its identifier is unsafe.")
    summary = str(status.get("summary") or "").strip()
    blocker = str(status.get("blocking_reason") or "").strip()
    if summary:
        result.append(f"- Coordinator-visible summary: {summary[:1000]}")
    if blocker:
        result.append(f"- Blocking reason to resolve: {blocker[:1000]}")
    result.extend(
        [
            "- Preserve compatible work already present in the worktree; do not redo completed work merely to recreate evidence.",
            "- Resolve only the current feedback or unfinished work; follow the phase instructions below for this attempt's closeout.",
        ]
    )
    return result


def render_worker_prompt(
    *,
    worktree_path: str,
    task_dir: Path,
    status_path: Path,
    attempt_dir: Path,
    worker_backend: str = "claude-code",
    agent_name: str = "",
    phase: str = "execution",
    strategy_path: str = "",
    prompt_mode: str = "full",
    prompt_mode_reason: str = "new_or_replacement_session",
    dependency_context_payload: Mapping[str, Any] | None = None,
) -> str:
    if prompt_mode not in {"full", "compact_resume"}:
        raise ValueError("prompt_mode must be full or compact_resume")
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    profile = status.get("profile", "full")
    artifact_v2 = status.get("artifact_protocol_version") == 2
    read_policy_path = attempt_dir / "runtime" / "READ_POLICY.json"
    context_broker = Path(__file__).resolve().parent / "context_broker.py"
    prompt_strategy = load_prompt_strategy(strategy_path)
    strategy_block = ""
    dependency_block = ""
    dependency_path = attempt_dir / DEPENDENCY_CONTEXT_REF
    if prompt_mode == "full" and dependency_context_payload is not None:
        dependency_payload = validate_dependency_context(
            dict(dependency_context_payload)
        )
        dependency_block = render_dependency_prompt_manifest(dependency_payload)
    elif prompt_mode == "full" and dependency_path.exists():
        dependency_payload = load_frozen_dependency_context(attempt_dir)
        if dependency_payload is None:
            raise ValueError(
                "dependency context exists without a frozen TASK_INPUTS binding"
            )
        dependency_block = render_dependency_prompt_manifest(dependency_payload)
    coordinator_feedback = ""
    strategy_feedback = ""
    review_pointer = task_dir / "reviews" / "CURRENT_TASK_REVIEW.json"
    if review_pointer.exists():
        pointer = json.loads(review_pointer.read_text(encoding="utf-8"))
        decision_path = (task_dir / str(pointer["decision_path"])).resolve()
        if task_dir.resolve() not in decision_path.parents:
            raise ValueError("task review decision path escapes the task directory")
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        if decision.get("decision") == "changes_requested":
            findings_path = (task_dir / str(decision["findings_path"])).resolve()
            if task_dir.resolve() not in findings_path.parents:
                raise ValueError("task review findings path escapes the task directory")
            findings = findings_path.read_text(encoding="utf-8")
            digest = hashlib.sha256(findings.encode("utf-8")).hexdigest()
            if digest != decision.get("findings_sha256"):
                raise ValueError("task review findings digest does not match the decision")
            coordinator_feedback = "\n".join(
                [
                    "## Coordinator Feedback",
                    "",
                    f"Decision revision: {decision.get('revision')}",
                    f"Reviewer: {decision.get('reviewer')}",
                    "",
                    findings,
                    "",
                ]
            )
    if phase == "planning":
        strategy_reviews = sorted((task_dir / "strategy").glob("REVIEW-v*.json"))
        if strategy_reviews:
            strategy_review = json.loads(
                strategy_reviews[-1].read_text(encoding="utf-8")
            )
            if strategy_review.get("decision") == "changes_requested":
                revision = int(strategy_reviews[-1].stem.removeprefix("REVIEW-v"))
                reviewed_strategy_path = (
                    task_dir / "strategy" / f"STRATEGY-v{revision:03d}.json"
                )
                reviewed_strategy = json.loads(
                    reviewed_strategy_path.read_text(encoding="utf-8")
                )
                canonical = json.dumps(
                    reviewed_strategy,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
                digest = hashlib.sha256(canonical).hexdigest()
                if digest != strategy_review.get("strategy_sha256"):
                    raise ValueError(
                        "strategy review digest does not match the reviewed strategy"
                    )
                notes = strategy_review.get("notes") or []
                strategy_feedback = "\n".join(
                    [
                        "## Strategy Revision Feedback",
                        "",
                        f"Rejected revision: {revision}",
                        f"Strategy: {strategy_review.get('strategy_id')}",
                        f"Reviewer: {strategy_review.get('reviewer')}",
                        "",
                        *[f"- {note}" for note in notes],
                        "",
                    ]
                )
        strategy_action = "revise" if any((task_dir / "strategy").glob("STRATEGY-v*.json")) else "submit"
        rdo_path = Path(__file__).resolve().parent / "rdo.py"
        phase_rules = [
            "## Planning Phase",
            "",
            "- Inspect the task and worktree read-only. Do not edit, commit, or run implementation workflows.",
            "- Design all anticipated workflows, subagents, permissions, dependencies, budgets, and completion gates.",
            "- Assign each required acceptance command to one workflow and run it once through rdo check; do not duplicate the same acceptance argv through rdo exec.",
            "- On revision > 1, explicitly preserve compatible prior work with workflow.resume = {from_attempt, from_workflow, mode}; use mode=reuse only when no rerun is needed and mode=revalidate when outputs remain useful but checks must run again.",
            f"- Set strategy.backend_id to {worker_backend!r}; an approved strategy cannot execute through another backend.",
            f"- Use the embedded skeleton below, or regenerate the same policy-bounded JSON with: python3 {rdo_path} strategy scaffold --attempt-dir {attempt_dir}.",
            f"- Send the completed JSON on stdin to the attempt-local draft channel: python3 {rdo_path} strategy draft --attempt-dir {attempt_dir} --file -. This runs the exact deterministic strategy-payload preflight and stores only a valid draft.",
            f"- After a successful draft, submit it once with: python3 {rdo_path} strategy {strategy_action} --task-dir {task_dir} --draft.",
            f"- If a stored draft needs a read-only recheck, run: python3 {rdo_path} strategy preflight --attempt-dir {attempt_dir} --draft. Do not create strategy drafts in /tmp or another arbitrary path.",
            "- The complete minimal schema is embedded below. Adapt it to the task; do not inspect RDO source code or tests to rediscover the protocol.",
            "- Exit immediately after strategy submission; the coordinator reviews it in a separate step.",
            *(
                [
                    f"- If planning is blocked, publish the conditional request with: python3 {Path(__file__).resolve().parent / 'rdo.py'} finalize --attempt-dir {attempt_dir} --state blocked --summary <summary> --blocker-type <type> --blocking-reason <reason>."
                ]
                if artifact_v2
                else []
            ),
            "",
            "### Minimal Valid Strategy Skeleton",
            "",
            "```json",
            render_strategy_template(task_dir, worker_backend),
            "```",
        ]
    else:
        phase_rules = [
            "## Execution Phase",
            "",
        ]
        if profile == "full":
            full_protocol, strategy_block = render_full_execution_protocol(
                attempt_dir=attempt_dir,
                strategy_path=strategy_path,
                strategy=prompt_strategy,
            )
            phase_rules.extend([
                *full_protocol,
                f"- For an independent review workflow, each declared native reviewer writes a non-empty artifact under {attempt_dir / 'runtime' / 'reviews'}; complete it with one --review-evidence REVIEWER_ID=ARTIFACT_PATH per reviewer. Reviewer IDs must match observed backend agent instances.",
                "- Finish every implementation and remediation change before completing the last required workflow; that completion freezes the source tree for finalize-only closeout.",
                "- Commit all task worktree changes on the assigned task branch before final handoff; the worktree must be clean.",
                f"- After every required workflow and acceptance check completes, finish once with: python3 {Path(__file__).resolve().parent / 'rdo.py'} finalize {'--attempt-dir ' + str(attempt_dir) if artifact_v2 else '--task-dir ' + str(task_dir)} --state review --summary <summary>.",
                "- A new workflow kind, larger budget, wider permission, or exhaustive search requires a strategy revision and checkpoint.",
            ])
        elif profile == "direct":
            phase_rules.extend([
                "- Implement the task, run ordinary tests, inspect the complete diff, and fix every self-review finding.",
                f"- Execute every required acceptance command exactly through: python3 {Path(__file__).resolve().parent / 'rdo.py'} check --attempt-dir {attempt_dir} --check-id <id>.",
                f"- Once the source is final, or a deadline reminder requires closeout, freeze it once with: python3 {Path(__file__).resolve().parent / 'rdo.py'} finalization begin --attempt-dir {attempt_dir}. After this point a failed check requires a new attempt; do not edit production files.",
                "- Commit all task worktree changes on the assigned task branch before final handoff; the worktree must be clean.",
                "- You own the final review. The coordinator will enforce only mechanical merge gates.",
                f"- Finish once with: python3 {Path(__file__).resolve().parent / 'rdo.py'} finalize {'--attempt-dir ' + str(attempt_dir) if artifact_v2 else '--task-dir ' + str(task_dir)} --state verified --self-review-passed --summary <summary>.",
                "- If independent judgment is needed, hand off blocked and request escalation to delegated instead of self-approving.",
            ])
        else:
            phase_rules.extend([
                "- Implement the task, run ordinary tests, and self-review the diff before handoff.",
                f"- Execute every required acceptance command exactly through: python3 {Path(__file__).resolve().parent / 'rdo.py'} check --attempt-dir {attempt_dir} --check-id <id>.",
                f"- Once the source is final, or a deadline reminder requires closeout, freeze it once with: python3 {Path(__file__).resolve().parent / 'rdo.py'} finalization begin --attempt-dir {attempt_dir}. After this point a failed check requires a new attempt; do not edit production files.",
                "- Commit all task worktree changes on the assigned task branch before final handoff; the worktree must be clean.",
                "- The coordinator owns the independent code review and merge decision.",
                f"- Finish once with: python3 {Path(__file__).resolve().parent / 'rdo.py'} finalize {'--attempt-dir ' + str(attempt_dir) if artifact_v2 else '--task-dir ' + str(task_dir)} --state review --summary <summary>.",
            ])
        if artifact_v2:
            phase_rules.append(
                f"- If blocked, publish only the conditional request with: python3 {Path(__file__).resolve().parent / 'rdo.py'} finalize --attempt-dir {attempt_dir} --state blocked --summary <summary> --blocker-type <type> --blocking-reason <reason>."
            )
    if artifact_v2:
        protocol_paths = [
            f"- TASK_DIR: {task_dir}",
            f"- ATTEMPT_DIR: {attempt_dir}",
        ]
        artifact_reminder = (
            "- Do not hand-edit EVIDENCE.json, HANDOFF.json, HANDOFF_READY.json, "
            "TASK_INPUTS.json, or COMMANDS.ndjson; rdo check/finalize publish them."
        )
    else:
        protocol_paths = [
            f"- TASK_DIR: {task_dir}",
            f"- STATUS_PATH: {status_path}",
            f"- EVIDENCE_PATH: {task_dir / 'EVIDENCE.md'}",
            f"- HANDOFF_PATH: {task_dir / 'HANDOFF.md'}",
            f"- HANDOFF_JSON_PATH: {task_dir / 'HANDOFF.json'}",
            f"- ATTEMPT_DIR: {attempt_dir}",
            f"- LOGS_DIR: {task_dir / 'logs'}",
        ]
        artifact_reminder = (
            "- Do not hand-edit EVIDENCE.md, HANDOFF.md, HANDOFF.json, or "
            "COMPLETION.json; rdo strategy/finalize writes them atomically."
        )
    if prompt_mode == "compact_resume":
        compact_strategy = strategy_block if profile == "full" and phase == "execution" else ""
        return "\n".join(
            [
                "# Worker Resume Prompt",
                "",
                f"You are resuming the existing {worker_backend} session for this task.",
                f"Agent name: {agent_name or worker_backend}.",
                f"Execution profile: {profile}.",
                f"Prompt mode reason: {prompt_mode_reason}.",
                "The original frozen task packet is already in this native session. This delta supersedes stale runtime details; it does not replace the task contract.",
                "",
                "## Protocol File Paths",
                "",
                f"- WORKTREE_PATH: {worktree_path}",
                *protocol_paths,
                "",
                "## Protocol Reminders",
                "",
                "- Do not edit STATUS.json. Dispatch owns task state transitions.",
                artifact_reminder,
                "- Keep code changes inside the allowed paths from the original frozen task packet.",
                f"- Current machine read policy: {read_policy_path}",
                f"- Deterministic Context Broker: python3 {context_broker} --policy {read_policy_path} <index|search|get> ...",
                "",
                "## Current Source State",
                "",
                *_current_source_state(worktree_path),
                "",
                "## Remaining Work",
                "",
                *_resume_work_summary(task_dir, status),
                "",
                "## Critical Proof Obligations",
                "",
                *_critical_proof_obligations(task_dir),
                "",
                *phase_rules,
                "",
                compact_strategy,
                "",
                strategy_feedback,
                coordinator_feedback,
            ]
        )
    return "\n".join(
        [
            "# Worker Task Prompt",
            "",
            f"You are a {worker_backend} worker. Execute only this task packet.",
            f"Agent name: {agent_name or worker_backend}.",
            f"Execution profile: {profile}.",
            "",
            "## Protocol File Paths",
            "",
            "You are running in this worktree:",
            "",
            f"- WORKTREE_PATH: {worktree_path}",
            "",
            "The orchestration protocol files are outside the worktree. Use these absolute paths:",
            "",
            *protocol_paths,
            "",
            "These paths are CLI arguments, not discovery inputs. Do not Read or inspect task/attempt protocol files.",
            "Do not create alternate STATUS/EVIDENCE/HANDOFF files inside the worktree.",
            "",
            "## Protocol Reminders",
            "",
            "- Do not edit STATUS.json. Dispatch owns task state transitions.",
            "- Use the provided rdo command for strategy submission or final handoff; do not hand-edit task state.",
            "- If blocked, blocker_type must be one of: needs_coordinator, needs_user, environment, budget, irrecoverable.",
            artifact_reminder,
            "- Call the final strategy submission or finalize command once, after its prerequisites pass.",
            "- Keep code changes inside allowed_paths.",
            "",
            "## Context Access",
            "",
            "- TASK.md, CONTEXT.md, ACCEPTANCE.md, and EXECUTION_POLICY.json are frozen and fully embedded below. Use the embedded copies; do not re-read task-dir copies or TASK_INPUTS.json.",
            "- Treat the embedded CONTEXT.md as the decision capsule; do not rediscover its decisions from broad repository reading.",
            "- Search narrowly with the backend's native search/Glob/Grep or rg before opening files.",
            "- Do not read another task's worktree. Large Markdown outside the write scope must be read with offset/limit.",
            "- If a native read is denied, do not bypass the policy with Bash, Python, cat, or another indirect reader. Use the embedded inputs, an allowed worktree path, or the Context Broker.",
            f"- Machine policy: {read_policy_path}",
            f"- List indexed headings: python3 {context_broker} --policy {read_policy_path} index [--source <path>]",
            f"- Search indexed sources: python3 {context_broker} --policy {read_policy_path} search --query <pattern> [--source <path>]",
            f"- Retrieve one section: python3 {context_broker} --policy {read_policy_path} get --source <path> --section <heading> --question <specific-question>",
            *(
                [
                    "- Merged predecessor details use virtual sources such as dependency:T001; index the alias first, then retrieve one exact field.",
                    f"- Dependency example: python3 {context_broker} --policy {read_policy_path} get --source dependency:T001 --section required_interfaces --question <specific-question>",
                ]
                if dependency_block
                else []
            ),
            "- Context Broker retrieval is deterministic and bounded. Do not launch a model or subagent merely to extract a document section.",
            "",
            *phase_rules,
            "",
            strategy_block,
            "",
            strategy_feedback,
            coordinator_feedback,
            dependency_block,
            "",
            "## TASK.md",
            read_text(task_dir / "TASK.md"),
            "",
            "## CONTEXT.md",
            read_text(task_dir / "CONTEXT.md") if (task_dir / "CONTEXT.md").exists() else "Context is included in TASK.md.",
            "",
            "## ACCEPTANCE.md",
            read_text(task_dir / "ACCEPTANCE.md") if (task_dir / "ACCEPTANCE.md").exists() else "Acceptance criteria are included in TASK.md.",
            "",
            "## EXECUTION_POLICY.json",
            read_text(task_dir / "EXECUTION_POLICY.json") if (task_dir / "EXECUTION_POLICY.json").exists() else "{}",
            "",
        ]
    )


def render_tmux_runner(
    *,
    worktree_path: str,
    command: str,
    prompt_path: str,
    transcript_path: str,
    exit_code_file: str,
    done_signal: str,
    keep_session: str,
    prompt_transport: str,
    submit_key: str,
    post_paste_delay_ms: str,
    startup_path: str = "",
    startup_timeout_seconds: str = "45",
    backend_id: str = "",
) -> str:
    delay_seconds = "0"
    try:
        delay_seconds = str(max(0, int(post_paste_delay_ms)) / 1000)
    except ValueError:
        delay_seconds = "0"
    return f"""#!/usr/bin/env bash
set +e
WORKTREE_PATH={shlex.quote(worktree_path)}
WORKER_COMMAND={shlex.quote(command)}
PROMPT_PATH={shlex.quote(prompt_path)}
TRANSCRIPT_PATH={shlex.quote(transcript_path)}
EXIT_CODE_FILE={shlex.quote(exit_code_file)}
DONE_SIGNAL={shlex.quote(done_signal)}
KEEP_SESSION={shlex.quote(keep_session)}
PROMPT_TRANSPORT={shlex.quote(prompt_transport)}
SUBMIT_KEY={shlex.quote(submit_key)}
POST_PASTE_DELAY_SECONDS={shlex.quote(delay_seconds)}
STARTUP_PATH={shlex.quote(startup_path)}
STARTUP_TIMEOUT_SECONDS={shlex.quote(startup_timeout_seconds)}
BACKEND_ID={shlex.quote(backend_id)}

startup_state() {{
  local state="$1"
  local evidence="${{2:-}}"
  [[ -n "${{STARTUP_PATH}}" ]] || return 0
  python3 - "${{STARTUP_PATH}}" "${{BACKEND_ID}}" "${{PROMPT_TRANSPORT}}" "${{STARTUP_TIMEOUT_SECONDS}}" "${{state}}" "${{evidence}}" <<'PY'
import hashlib, json, os, pathlib, sys
path, backend, transport, timeout, state, evidence = sys.argv[1:]
payload = {{
    "mode": "human",
    "state": state,
    "backend_id": backend,
    "prompt_transport": transport,
    "startup_timeout_seconds": int(timeout),
    "startup_evidence": {{"event": evidence}} if evidence else None,
    "failure": (
        {{"code": state, "message": evidence or "human worker startup failed"}}
        if state == "tui_startup_failed" else None
    ),
}}
temporary = pathlib.Path(path + ".tmp")
temporary.write_text(json.dumps(payload, indent=2) + "\\n", encoding="utf-8")
os.replace(temporary, path)
PY
}}

finish() {{
  local rc="$?"
  local tmp="${{EXIT_CODE_FILE}}.tmp"
  echo "${{rc}}" > "${{tmp}}"
  mv "${{tmp}}" "${{EXIT_CODE_FILE}}"
  tmux wait-for -S "${{DONE_SIGNAL}}" 2>/dev/null || true
  if [[ "${{KEEP_SESSION}}" == "1" ]]; then
    echo
    echo "Worker finished with exit code ${{rc}}."
    echo "Press Ctrl-D or run exit to close this tmux session."
    exec bash -l
  fi
  exit "${{rc}}"
}}
trap finish EXIT

cd "${{WORKTREE_PATH}}" || exit 127
set -o pipefail
startup_state "tui_process_started" "tmux_session_created"
if [[ "${{PROMPT_TRANSPORT}}" == "tmux_send_keys" ]]; then
  (
    deadline=$((SECONDS + STARTUP_TIMEOUT_SECONDS))
    while [[ "${{SECONDS}}" -lt "${{deadline}}" ]]; do
      pane="$(tmux capture-pane -p -t "${{TMUX_PANE}}" 2>/dev/null || true)"
      [[ -n "${{pane//[[:space:]]/}}" ]] && break
      sleep 0.25
    done
    if [[ -s "${{PROMPT_PATH}}" ]]; then
      if ! tmux load-buffer -b rdo-worker-prompt "${{PROMPT_PATH}}" 2>/dev/null || \
         ! tmux paste-buffer -b rdo-worker-prompt -t "${{TMUX_PANE}}" 2>/dev/null; then
        startup_state "tui_startup_failed" "prompt_paste_failed"
        exit 126
      fi
      sleep "${{POST_PASTE_DELAY_SECONDS}}"
      if [[ -n "${{SUBMIT_KEY}}" ]]; then
        if ! tmux send-keys -t "${{TMUX_PANE}}" "${{SUBMIT_KEY}}" 2>/dev/null; then
          startup_state "tui_startup_failed" "prompt_submit_failed"
          exit 126
        fi
      fi
      startup_state "prompt_submitted" "tmux_send_keys"
    fi
  ) &
  eval "${{WORKER_COMMAND}}"
  exit "$?"
fi
if [[ "${{PROMPT_TRANSPORT}}" == "arg" ]]; then
  startup_state "prompt_submitted" "argv"
  eval "${{WORKER_COMMAND}}"
  exit "$?"
fi
eval "${{WORKER_COMMAND}}" < "${{PROMPT_PATH}}" 2>&1 | tee "${{TRANSCRIPT_PATH}}"
exit "${{PIPESTATUS[0]}}"
"""


def cmd_render_prompt(args: argparse.Namespace) -> int:
    output = Path(args.output)
    output.write_text(
        render_worker_prompt(
            worktree_path=args.worktree_path,
            task_dir=Path(args.task_dir),
            status_path=Path(args.status_path),
            attempt_dir=Path(args.attempt_dir),
            worker_backend=args.worker_backend,
            agent_name=args.agent_name,
            phase=args.phase,
            strategy_path=args.strategy_path,
            prompt_mode=args.prompt_mode,
            prompt_mode_reason=args.prompt_mode_reason,
        ),
        encoding="utf-8",
    )
    return 0


def cmd_render_tmux_runner(args: argparse.Namespace) -> int:
    output = Path(args.output)
    output.write_text(
        render_tmux_runner(
            worktree_path=args.worktree_path,
            command=args.command,
            prompt_path=args.prompt_path,
            transcript_path=args.transcript_path,
            exit_code_file=args.exit_code_file,
            done_signal=args.done_signal,
            keep_session=args.keep_session,
            prompt_transport=args.prompt_transport,
            submit_key=args.submit_key,
            post_paste_delay_ms=args.post_paste_delay_ms,
            startup_path=args.startup_path,
            startup_timeout_seconds=args.startup_timeout_seconds,
            backend_id=args.backend_id,
        ),
        encoding="utf-8",
    )
    os.chmod(output, 0o755)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render dispatch worker assets.")
    sub = parser.add_subparsers(dest="command", required=True)

    prompt = sub.add_parser("render-prompt")
    prompt.add_argument("--output", required=True)
    prompt.add_argument("--worktree-path", required=True)
    prompt.add_argument("--task-dir", required=True)
    prompt.add_argument("--status-path", required=True)
    prompt.add_argument("--attempt-dir", required=True)
    prompt.add_argument("--worker-backend", default="claude-code")
    prompt.add_argument("--agent-name", default="")
    prompt.add_argument("--phase", choices=["planning", "execution"], required=True)
    prompt.add_argument("--strategy-path", default="")
    prompt.add_argument("--prompt-mode", choices=["full", "compact_resume"], default="full")
    prompt.add_argument("--prompt-mode-reason", default="new_or_replacement_session")
    prompt.set_defaults(func=cmd_render_prompt)

    runner = sub.add_parser("render-tmux-runner")
    runner.add_argument("--output", required=True)
    runner.add_argument("--worktree-path", required=True)
    runner.add_argument("--command", required=True)
    runner.add_argument("--prompt-path", required=True)
    runner.add_argument("--transcript-path", required=True)
    runner.add_argument("--exit-code-file", required=True)
    runner.add_argument("--done-signal", required=True)
    runner.add_argument("--keep-session", required=True)
    runner.add_argument("--prompt-transport", default="stdin")
    runner.add_argument("--submit-key", default="")
    runner.add_argument("--post-paste-delay-ms", default="0")
    runner.add_argument("--startup-path", default="")
    runner.add_argument("--startup-timeout-seconds", default="45")
    runner.add_argument("--backend-id", default="")
    runner.set_defaults(func=cmd_render_tmux_runner)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
