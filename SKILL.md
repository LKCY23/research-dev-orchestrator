---
name: research-dev-orchestrator
description: Coordinate research proposal, experiment design, reproducible experiment implementation, and open-source contribution workflows with a role-neutral coordinator and CLI-based coding agents as workers. Use when an agent needs to turn research or experiment goals into requirements, design decisions, task packets, worker dispatch, evidence-based review, and iterative implementation, or resume this workflow from existing materials at any stage, using repo-local filesystem protocols and Git worktree isolation.
---

# Research Dev Orchestrator

Use this skill to coordinate research and experiment-driven development with a coordinator backend such as Codex or Claude Code and CLI coding agents as execution workers.

Do not treat this as a server, RPC, queue, or daemon architecture. Use repo-local files as the protocol and Git branches/worktrees as execution isolation.

## Core Rules

- The coordinator owns intent: requirements, experiment design, architecture decisions, task decomposition, acceptance criteria, review, and merge decisions.
- Workers own execution: implement only assigned task packets and write evidence plus a `HANDOFF.json` transition request. Workers must not edit `STATUS.json` terminal state.
- Filesystem is the protocol: exchange state through `.agent-collab/runs/<run-id>/...`.
- Git is the isolation boundary: use one branch/worktree per task; workers never merge.
- FSM is a hard protocol: read `references/state-machine.json` before any state mutation.
- `SUMMARY.md`, `dashboard.html`, and `diagnostics/` are derived monitor artifacts, not sources of truth.
- `EVENTS.ndjson` and `JOURNAL.md` are required long-term memory artifacts. Use them to preserve cross-session history without adding a heavier decision database.
- Task FSM stays about task progress only. `ATTEMPT.json` owns worker execution lifecycle. `collect_status.py` validates invariants across status, attempt, lock, events, evidence, and handoff files.
- `scripts/protocol.py` is the script-internal source for protocol constants and low-level helpers. `scripts/validation.py` owns shared protocol validation rules used by online dispatch gates and offline status audit. Neither file is a user interface or public SDK.
- `.agent-collab/rdo.toml` and `scripts/config.py` own operational defaults only. They must not configure protocol states, schema fields, events, blocker types, or protocol version.
- Worker runtime backend is an execution detail. Default to `plain`; use `tmux` only when the user wants attachable long-running worker observation. Backend choice must not change protocol truth sources.
- Coordinator intents are structured natural-language requests for human control. They are not Codex slash commands and must still follow all protocol invariants.
- Do not destructively overwrite or reinitialize audit-bearing artifacts. Use a new run, new attempt, or revision task.

## Default Progression and Stage Entry

Follow the Standard Workflow by default unless the user explicitly requests a different process. Do not treat an RDO invocation as permission to continue substantive work in ad hoc documents outside the workflow.

On every activation or resumption, perform a read-only phase audit before substantive work or protocol state mutation:

1. Resolve the intended project root and, when present, the Git repository root.
2. Inventory canonical RDO artifacts and relevant existing materials supplied by the user or found in the project.
3. Classify each prerequisite as `satisfied`, `satisfied_by_existing_material`, `needs_normalization`, `missing`, or `blocked`.
4. Infer the current workflow stage and identify the next required gate.
5. Briefly report the inferred stage, usable artifacts, and blocking gaps before proceeding.

When the user does not specify an entry stage, continue from the earliest unmet required gate. Preserve completed work; do not restart requirements or design merely because the material uses non-RDO filenames.

When the user explicitly requests entry at a later stage, treat that request as an entrypoint, not as permission to bypass prerequisites:

- Accept semantically sufficient existing materials regardless of filename.
- Create or update thin canonical RDO artifacts that reference and normalize those materials when downstream protocol steps require them.
- Fill only the prerequisite gaps that block the requested stage.
- Do not redo completed research, design, implementation, or review work.
- Proceed to the requested stage as soon as its prerequisites are satisfied.

An explicit request to skip or alter the workflow may waive soft planning ceremony, but record the waiver and its consequences in the available audit trail. It never waives Git isolation, FSM validity, immutable strategy approval, attempt supervision, handoff validation, review gates, or merge gates.

Synchronize approved decisions to canonical artifacts before continuing:

- requirements, scope, constraints, non-goals, and acceptance criteria -> `REQUIREMENTS.md`;
- design-method and architecture choices -> `DESIGN_METHOD_SELECTION.md`, `DESIGN_BRIEF.md`, or relevant `ADR/*`;
- hypotheses, baselines, datasets, metrics, and evaluation protocol -> `EXPERIMENT_PLAN.md`;
- environment, versions, seeds, commands, artifacts, and expected outputs -> `REPRODUCIBILITY.md`;
- implementation decomposition and task acceptance -> task packets and `ACCEPTANCE.md`;
- experiment outcomes and claim support -> `RESULT_LEDGER.md`.

If no Git repository exists, remain in pre-run planning: canonical planning artifacts may be created or normalized, but do not initialize RDO runs, create worktrees, dispatch workers, or claim execution readiness.

## Standard Workflow

This is the canonical default progression. A stage-aware entry may begin later only after the phase audit above confirms or normalizes its prerequisites.

1. Clarify requirements with the user and create/update `REQUIREMENTS.md`.
2. Before design, select the design method and architecture style in `DESIGN_METHOD_SELECTION.md`.
3. Produce `DESIGN_BRIEF.md`, relevant `ADR/*`, `EXPERIMENT_PLAN.md`, and `REPRODUCIBILITY.md`.
4. Decompose work into task packets using `references/task-packet-template.md`.
5. Create a run with `scripts/init_run.py` if no run exists.
6. Create tasks with `scripts/create_task.py`.
7. Dispatch worker tasks with `scripts/dispatch_agent.sh` only when task states allow dispatch. `scripts/dispatch_claude.sh` remains a compatibility entrypoint.
8. Collect state with `scripts/collect_status.py`; use `--json` for machine consumers and `--write-summary` for `SUMMARY.md`. Use `scripts/render_dashboard.py` when the user wants a visual run monitor.
9. Review tasks manually using `references/review-rubric.md`.
10. Only mark `approved` after diff review, evidence review, mergeability verification, and required integration smoke tests pass.
11. Merge only approved tasks, then record post-merge smoke results when required by `ACCEPTANCE.md`.
12. Update `RESULT_LEDGER.md` for experiment outcomes and claim support.
13. At the end of every working session, run `scripts/close_session.py` to update `SUMMARY.md`, append `JOURNAL.md`, and append a `session_closed` event.

## References

Read references only when they are needed:

- `references/requirements-template.md`: use during user requirement and research goal clarification.
- `references/design-method-selection.md`: use before writing system or experiment design.
- `references/adr-template.md`: use when recording architecture decisions.
- `references/experiment-plan-template.md`: use for hypotheses, baselines, datasets, metrics, and ablations.
- `references/reproducibility-template.md`: use for environment, seed, data, command, and expected-output contracts.
- `references/result-ledger-template.md`: use to record experiment results and claim support.
- `references/task-packet-template.md`: use before creating or editing task packets.
- `references/state-machine.md`: use for human-readable FSM semantics.
- `references/state-machine.json`: use as the authoritative machine-readable FSM.
- `references/status-schema.md`: use before writing or reviewing `STATUS.json`.
- `references/attempt-lifecycle.md`: use before dispatching workers or auditing running/review/blocked invariants.
- `references/configuration.md`: use when changing `.agent-collab/rdo.toml`, config defaults, env overrides, stale thresholds, or task branch/worktree defaults.
- `references/runtime-backends.md`: use before enabling `RDO_RUNTIME_BACKEND=tmux` or auditing backend-specific attempt metadata.
- `references/agent-backends.md`: use before changing worker backend registry definitions or backend-specific command contracts.
- `references/protocol-constants.md`: use when changing script constants, exit codes, blocker types, or event types.
- `references/command-surface.md`: use when the user invokes coordinator intent phrases such as `$research-dev-orchestrator dispatch ...`.
- `references/lock-recovery.md`: use when `.dispatch-lock` is stale, mismatched, or present outside `running`.
- `references/review-rubric.md`: use before Codex review and merge decisions.
- `references/summary-template.md`: use when updating or auditing `SUMMARY.md`.
- `references/events-schema.md`: use when appending or auditing `EVENTS.ndjson`.
- `references/journal-template.md`: use when closing a session or auditing `JOURNAL.md`.

## Scripts

Run scripts from the target repository root, but call the scripts by absolute path from this skill directory. If needed, set `RESEARCH_DEV_ORCHESTRATOR_HOME` to the directory containing this `SKILL.md`.

```bash
export RESEARCH_DEV_ORCHESTRATOR_HOME=/absolute/path/to/research-dev-orchestrator
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/init_run.py" --project-slug <slug> --objective "<objective>" --target-branch <branch>
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/create_task.py" --run-id <run-id> --task-id T001-name --goal "<goal>" --allowed-paths path1 path2
"$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/dispatch_agent.sh" <run-id> <task-id>
RDO_WORKER_BACKEND=opencode RDO_RUNTIME_BACKEND=tmux "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/dispatch_agent.sh" <run-id> <task-id>
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id>
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --json
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --write-summary
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --write-diagnostics
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/render_dashboard.py" --run-id <run-id>
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/config_cli.py" validate
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/remove_dispatch_lock.py" --run-id <run-id> --task-id <task-id> --reason "<approved reason>" --confirmed
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/close_session.py" --run-id <run-id> --summary "<session summary>" --changed "<change>" --next-action "<next>"
"$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/run_smoke_tests.sh"
```

## Coordinator Intent Surface

Users may invoke the skill explicitly with `$research-dev-orchestrator` or select it through `/skills`, then provide structured intent text. Treat these as natural-language intents for Codex, not shell commands or registered slash commands:

```text
$research-dev-orchestrator init project=<slug> objective="<text>" [target=<branch>]
$research-dev-orchestrator plan run=<run-id> [scope=requirements|design|experiment|all]
$research-dev-orchestrator create-task run=<run-id> task=<task-id> goal="<text>" allowed=<path,path> [forbidden=<path,path>]
$research-dev-orchestrator dispatch run=<run-id> task=<task-id> [backend=plain|tmux] [timeout=<seconds>]
$research-dev-orchestrator status run=<run-id> [json] [summary] [dashboard] [diagnostics]
$research-dev-orchestrator review run=<run-id> task=<task-id>
$research-dev-orchestrator recover-lock run=<run-id> task=<task-id>
$research-dev-orchestrator close run=<run-id> summary="<text>" [changed="<text>"] [next="<text>"]
```

Read `references/command-surface.md` before acting on these intents. `review` does not automatically approve; it produces findings and recommendations, and mutates state only with explicit user instruction and valid review gates.

`init_run.py` scaffolds only. It must not make substantive research, design, or architecture decisions.

`create_task.py` creates `pending` tasks only. It must not overwrite existing tasks, dispatch, create locks, or merge.

`protocol.py` is used by scripts for constants, template rendering, JSON helpers, and event append. Users should not call it directly.

`validation.py` contains shared protocol validation rules, starting with worker handoff validation. `protocol_cli.py validate-handoff` and `collect_status.py` must reuse it so online gate checks and offline audit do not drift.

`config.py` loads operational defaults from `.agent-collab/rdo.toml` and environment variables. It must not define or mutate protocol truth. CLI flags, explicit coordinator intent arguments, and explicit env vars still override config.

`agent_backends/` defines supported worker backend adapters: `claude-code`, `codex`, `opencode`, and `kimi-code`. `scripts/agent_backend_cli.py` validates adapters and renders backend command lines.

`protocol_cli.py` is a narrow internal bridge for dispatch scripts. It performs mechanical protocol operations such as attempt creation, transition to running, event append, handoff validation, and diagnostics writing. It must not implement coordinator-only decisions such as approve, merge, auto-review, or auto-recover.

`dispatch_assets.py` renders attempt-local worker assets such as `prompt.md` and tmux `run-worker.sh`. It must not mutate protocol state; dispatch remains responsible for locks, worktrees, process supervision, and handoff validation.

`dispatch_agent.sh` is the generic worker dispatch entrypoint. It may transition `pending|blocked|changes_requested -> running`, atomically acquire `.dispatch-lock`, write `LOCK` ownership metadata, create an attempt, call a configured worker CLI, and verify whether the worker wrote a valid `HANDOFF.json` terminal request. It loads operational defaults from config before any protocol mutation, but explicit env vars still win. It gives the worker absolute protocol file paths because the worker runs inside a task worktree while `.agent-collab` lives in the target repository root. It must update `ATTEMPT.json` lifecycle fields and applies validated `running -> review|blocked` terminal transitions. A `review` request requires worker `exit_code = 0`; `blocked` may have a nonzero exit code if blocker metadata and handoff are valid. Invalid handoff becomes `blocked` with `blocker_type = needs_coordinator`.

Worker backend configuration:

```bash
RDO_WORKER_BACKEND=claude-code|codex|opencode|kimi-code
RDO_RUNTIME_BACKEND=plain|tmux
RDO_IO_MODE=machine|human
RDO_PERMISSION_MODE=default|auto|yolo
RDO_TMUX_KEEP_SESSION=0|1
RDO_TMUX_WAIT_TIMEOUT_SECONDS=0
```

`tmux` backend is attachable execution, not detached orchestration. Dispatch still waits for the attempt-local `exit_code` file and validates handoff. If tmux wait times out before `exit_code` appears, dispatch exits `5`, keeps `.dispatch-lock`, leaves `ATTEMPT.state=running`, writes diagnostics, and requires Lock Recovery Review.

`collect_status.py` is read-only by default. It must not modify `STATUS.json`, delete locks, change FSM state, or repair violations. `--write-summary` may update only `SUMMARY.md`; `--write-diagnostics` may write only diagnostics files.

`render_dashboard.py` writes only derived `dashboard.html` by reading the same status report as `collect_status.py`. It must not mutate protocol truth.

`remove_dispatch_lock.py` is a user-approved mechanical recovery tool. Use it only after a Lock Recovery Review and explicit user confirmation. It snapshots `.dispatch-lock`, removes only `.dispatch-lock`, and appends `dispatch_lock_removed`; it must not modify `STATUS.json`, `ATTEMPT.json`, `LOCK`, `HANDOFF.md`, `EVIDENCE.md`, or FSM state.

`close_session.py` is the standard session closeout command. It updates derived `SUMMARY.md`, appends a human-readable `JOURNAL.md` entry, and appends a `session_closed` event to `EVENTS.ndjson`.

`templates/` is the scaffold content source for `init_run.py` and `create_task.py`. `references/` remains the protocol, schema, rubric, and workflow explanation layer.

## Long-Term Memory

Use these files to recover context after days or weeks:

- `SUMMARY.md`: current dashboard; derived and regenerable.
- `dashboard.html`: visual run monitor; derived and regenerable.
- `EVENTS.ndjson`: append-only machine-readable timeline; required.
- `JOURNAL.md`: append-only human-readable session memory; required.
- `RESULT_LEDGER.md`: experiment results and claim support.
- `ADR/*`: durable architecture/design decisions only.
- `reviews/*`: Codex review records.
- `tasks/*/attempts/*`: worker execution records.

`HANDOFF.json` is the machine-readable worker handoff request. It does not replace `HANDOFF.md`; dispatch validates it and applies terminal task state transitions.

Do not force a separate `DECISIONS.md` in the first version. Put non-architecture session decisions and tradeoffs in `JOURNAL.md`; add ADRs only when a decision should be durable architecture/design record.

## Review Gate

Before changing `review -> approved`, verify all of the following:

- The diff stays within `allowed_paths` and avoids `forbidden_paths`.
- `EVIDENCE.md`, logs, and `STATUS.json.evidence` are consistent enough to support `ACCEPTANCE.md`.
- Required commands and metrics passed, or failures are explicitly scoped and acceptable.
- `ACCEPTANCE.md` lists review gate recipes: required commands, smoke tests, expected outputs, metrics or thresholds, merge preconditions, and failure handoff conditions.
- The task branch is mergeable into the target branch, preferably with a dry-run or temporary integration worktree.
- Required integration smoke tests pass.
- No unresolved blocker, stale lock ambiguity, or protocol violation remains.

If any review gate fails, use `review -> changes_requested` or `review -> failed` as defined by the FSM.
