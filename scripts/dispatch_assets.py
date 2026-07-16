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
from pathlib import Path


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").rstrip()


def render_strategy_template(task_dir: Path, worker_backend: str) -> str:
    """Render a policy-bounded strategy skeleton so planners need no RDO source inspection."""
    status = json.loads((task_dir / "STATUS.json").read_text(encoding="utf-8"))
    policy = json.loads((task_dir / "EXECUTION_POLICY.json").read_text(encoding="utf-8"))
    existing = sorted((task_dir / "strategy").glob("STRATEGY-v*.json"))
    revision = len(existing) + 1
    supersedes = None
    if existing:
        previous = json.loads(existing[-1].read_text(encoding="utf-8"))
        supersedes = previous["strategy_id"]
    command_seconds = min(policy["default_command_seconds"], policy["attempt_wall_seconds"])
    template = {
        "schema_version": 2,
        "backend_id": worker_backend,
        "strategy_id": f"{status['task_id']}-S{revision:03d}",
        "task_id": status["task_id"],
        "revision": revision,
        "supersedes": supersedes,
        "objective": "Replace with the task-specific execution objective",
        "global_budget": {
            "wall_seconds": policy["attempt_wall_seconds"],
            "max_workflows": policy["max_workflows"],
            "max_workflow_instances": policy["max_workflow_instances"],
            "max_parallel_workflows": policy["max_parallel_workflows"],
            "max_subagents": policy["max_subagents"],
            "max_parallel_subagents": policy["max_parallel_subagents"],
        },
        "workflows": [
            {
                "workflow_id": "WF-implementation",
                "kind": "implementation",
                "purpose": "Replace with a bounded task-specific workflow",
                "depends_on": [],
                "required": True,
                "executor": {
                    "mode": "primary_worker",
                    "write_access": True,
                    "max_agents": 0,
                    "max_parallel": 0,
                    "allowed_paths": policy["allowed_paths"],
                },
                "budget": {
                    "wall_seconds": policy["attempt_wall_seconds"],
                    "command_seconds": command_seconds,
                    "max_enumerated_cases": policy["max_enumerated_cases"],
                    "max_instances": 1,
                },
                "completion": {"evidence": "Replace with concrete completion evidence"},
                "on_timeout": "block",
            }
        ],
        "runtime_change_policy": {
            "allow_new_instances_of_approved_workflows": True,
            "require_revision_for_new_workflow_kind": True,
            "require_revision_for_budget_increase": True,
            "allow_unbounded_search": policy["allow_unbounded_search"],
        },
        "completion_gate": {
            "required_workflows_complete": True,
            "acceptance_commands_pass": True,
            "optional_workflows_may_timeout": True,
        },
    }
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
) -> str:
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    profile = status.get("profile", "full")
    artifact_v2 = status.get("artifact_protocol_version") == 2
    read_policy_path = attempt_dir / "runtime" / "READ_POLICY.json"
    context_broker = Path(__file__).resolve().parent / "context_broker.py"
    prompt_strategy = load_prompt_strategy(strategy_path)
    strategy_block = ""
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
        phase_rules = [
            "## Planning Phase",
            "",
            "- Inspect the task and worktree read-only. Do not edit, commit, or run implementation workflows.",
            "- Design all anticipated workflows, subagents, permissions, dependencies, budgets, and completion gates.",
            "- Assign each required acceptance command to one workflow and run it once through rdo check; do not duplicate the same acceptance argv through rdo exec.",
            "- On revision > 1, explicitly preserve compatible prior work with workflow.resume = {from_attempt, from_workflow, mode}; use mode=reuse only when no rerun is needed and mode=revalidate when outputs remain useful but checks must run again.",
            f"- Set strategy.backend_id to {worker_backend!r}; an approved strategy cannot execute through another backend.",
            f"- Write the strategy JSON outside the worktree, then run: python3 {Path(__file__).resolve().parent / 'rdo.py'} strategy {strategy_action} --task-dir {task_dir} --file <strategy-file>.",
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
            "- Context Broker retrieval is deterministic and bounded. Do not launch a model or subagent merely to extract a document section.",
            "",
            *phase_rules,
            "",
            strategy_block,
            "",
            strategy_feedback,
            coordinator_feedback,
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
