---
name: research-dev-orchestrator
description: Coordinate research proposal, experiment design, reproducible experiment implementation, and open-source contribution workflows with Codex as coordinator and CLI-based coding agents as workers. Use when Codex needs to turn research or experiment goals into requirements, design decisions, task packets, worker dispatch, evidence-based review, and iterative implementation using repo-local filesystem protocols and Git worktree isolation.
---

# Research Dev Orchestrator

Use this skill to coordinate research and experiment-driven development with Codex as the coordinator and CLI coding agents, such as Claude Code, as execution workers.

Do not treat this as a server, RPC, queue, or daemon architecture. Use repo-local files as the protocol and Git branches/worktrees as execution isolation.

## Core Rules

- Codex owns intent: requirements, experiment design, architecture decisions, task decomposition, acceptance criteria, review, and merge decisions.
- Workers own execution: implement only assigned task packets, write evidence, and transition tasks only from `running` to `review` or `blocked`.
- Filesystem is the protocol: exchange state through `.agent-collab/runs/<run-id>/...`.
- Git is the isolation boundary: use one branch/worktree per task; workers never merge.
- FSM is a hard protocol: read `references/state-machine.json` before any state mutation.
- `SUMMARY.md` and `diagnostics/` are derived monitor artifacts, not sources of truth.
- `EVENTS.ndjson` and `JOURNAL.md` are required long-term memory artifacts. Use them to preserve cross-session history without adding a heavier decision database.
- Task FSM stays about task progress only. `ATTEMPT.json` owns worker execution lifecycle. `collect_status.py` validates invariants across status, attempt, lock, events, evidence, and handoff files.
- `scripts/protocol.py` is the script-internal source for protocol constants and low-level helpers. `scripts/validation.py` owns shared protocol validation rules used by online dispatch gates and offline status audit. Neither file is a user interface or public SDK.
- `.agent-collab/rdo.toml` and `scripts/config.py` own operational defaults only. They must not configure protocol states, schema fields, events, blocker types, or protocol version.
- Worker runtime backend is an execution detail. Default to `plain`; use `tmux` only when the user wants attachable long-running worker observation. Backend choice must not change protocol truth sources.
- `/rdo ...` commands are Codex-facing intent grammar for human control. They are not executable shell slash commands and must still follow all protocol invariants.
- Do not destructively overwrite or reinitialize audit-bearing artifacts. Use a new run, new attempt, or revision task.

## Standard Workflow

1. Clarify requirements with the user and create/update `REQUIREMENTS.md`.
2. Before design, select the design method and architecture style in `DESIGN_METHOD_SELECTION.md`.
3. Produce `DESIGN_BRIEF.md`, relevant `ADR/*`, `EXPERIMENT_PLAN.md`, and `REPRODUCIBILITY.md`.
4. Decompose work into task packets using `references/task-packet-template.md`.
5. Create a run with `scripts/init_run.py` if no run exists.
6. Create tasks with `scripts/create_task.py`.
7. Dispatch worker tasks with `scripts/dispatch_claude.sh` only when task states allow dispatch.
8. Collect state with `scripts/collect_status.py`; use `--json` for machine consumers and `--write-summary` for `SUMMARY.md`.
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
- `references/runtime-backends.md`: use before enabling `RDO_WORKER_BACKEND=tmux` or auditing backend-specific attempt metadata.
- `references/protocol-constants.md`: use when changing script constants, exit codes, blocker types, or event types.
- `references/command-surface.md`: use when the user invokes `/rdo ...` command-like intents.
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
"$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/dispatch_claude.sh" <run-id> <task-id>
RDO_WORKER_BACKEND=tmux "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/dispatch_claude.sh" <run-id> <task-id>
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id>
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --json
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --write-summary
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --write-diagnostics
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/config_cli.py" validate
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/remove_dispatch_lock.py" --run-id <run-id> --task-id <task-id> --reason "<approved reason>" --confirmed
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/close_session.py" --run-id <run-id> --summary "<session summary>" --changed "<change>" --next-action "<next>"
"$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/run_smoke_tests.sh"
```

## Command Surface

Users may invoke command-like intents. Treat these as structured natural language for Codex, not shell commands:

```text
/rdo init project=<slug> objective="<text>" [target=<branch>]
/rdo plan run=<run-id> [scope=requirements|design|experiment|all]
/rdo create-task run=<run-id> task=<task-id> goal="<text>" allowed=<path,path> [forbidden=<path,path>]
/rdo dispatch run=<run-id> task=<task-id> [backend=plain|tmux] [timeout=<seconds>]
/rdo status run=<run-id> [json] [summary] [diagnostics]
/rdo review run=<run-id> task=<task-id>
/rdo recover-lock run=<run-id> task=<task-id>
/rdo close run=<run-id> summary="<text>" [changed="<text>"] [next="<text>"]
```

Read `references/command-surface.md` before acting on `/rdo ...`. `/rdo review` does not automatically approve; it produces findings and recommendations, and mutates state only with explicit user instruction and valid review gates.

`init_run.py` scaffolds only. It must not make substantive research, design, or architecture decisions.

`create_task.py` creates `pending` tasks only. It must not overwrite existing tasks, dispatch, create locks, or merge.

`protocol.py` is used by scripts for constants, template rendering, JSON helpers, and event append. Users should not call it directly.

`validation.py` contains shared protocol validation rules, starting with worker handoff validation. `protocol_cli.py validate-handoff` and `collect_status.py` must reuse it so online gate checks and offline audit do not drift.

`config.py` loads operational defaults from `.agent-collab/rdo.toml` and environment variables. It must not define or mutate protocol truth. CLI flags and `/rdo` one-off arguments still override config.

`protocol_cli.py` is a narrow internal bridge for `dispatch_claude.sh`. It performs mechanical protocol operations such as attempt creation, transition to running, event append, handoff validation, and diagnostics writing. It must not implement coordinator-only decisions such as approve, merge, auto-review, or auto-recover.

`dispatch_claude.sh` may transition `pending|blocked|changes_requested -> running`, atomically acquire `.dispatch-lock`, write `LOCK` ownership metadata, create an attempt, call a configured worker CLI, and verify whether the worker wrote a valid terminal handoff state. It gives the worker absolute protocol file paths because the worker runs inside a task worktree while `.agent-collab` lives in the target repository root. It must update `ATTEMPT.json` lifecycle fields and must not synthesize `review` or `blocked` for the worker. A `review` handoff requires worker `exit_code = 0`; `blocked` may have a nonzero exit code if blocker metadata and handoff are valid.

Worker backend configuration:

```bash
RDO_WORKER_BACKEND=plain|tmux
RDO_TMUX_KEEP_SESSION=0|1
RDO_TMUX_WAIT_TIMEOUT_SECONDS=0
```

`tmux` backend is attachable execution, not detached orchestration. Dispatch still waits for the attempt-local `exit_code` file and validates handoff. If tmux wait times out before `exit_code` appears, dispatch exits `5`, keeps `.dispatch-lock`, leaves `ATTEMPT.state=running`, writes diagnostics, and requires Lock Recovery Review.

`collect_status.py` is read-only by default. It must not modify `STATUS.json`, delete locks, change FSM state, or repair violations. `--write-summary` may update only `SUMMARY.md`; `--write-diagnostics` may write only diagnostics files.

`remove_dispatch_lock.py` is a user-approved mechanical recovery tool. Use it only after a Lock Recovery Review and explicit user confirmation. It snapshots `.dispatch-lock`, removes only `.dispatch-lock`, and appends `dispatch_lock_removed`; it must not modify `STATUS.json`, `ATTEMPT.json`, `LOCK`, `HANDOFF.md`, `EVIDENCE.md`, or FSM state.

`close_session.py` is the standard session closeout command. It updates derived `SUMMARY.md`, appends a human-readable `JOURNAL.md` entry, and appends a `session_closed` event to `EVENTS.ndjson`.

`templates/` is the scaffold content source for `init_run.py` and `create_task.py`. `references/` remains the protocol, schema, rubric, and workflow explanation layer.

## Long-Term Memory

Use these files to recover context after days or weeks:

- `SUMMARY.md`: current dashboard; derived and regenerable.
- `EVENTS.ndjson`: append-only machine-readable timeline; required.
- `JOURNAL.md`: append-only human-readable session memory; required.
- `RESULT_LEDGER.md`: experiment results and claim support.
- `ADR/*`: durable architecture/design decisions only.
- `reviews/*`: Codex review records.
- `tasks/*/attempts/*`: worker execution records.

Do not force a separate `DECISIONS.md` in the first version. Put non-architecture session decisions and tradeoffs in `JOURNAL.md`; add ADRs only when a decision should be durable architecture/design record.

## Review Gate

Before changing `review -> approved`, verify all of the following:

- The diff stays within `allowed_paths` and avoids `forbidden_paths`.
- `EVIDENCE.md`, logs, and `STATUS.json.evidence` are consistent enough to support `ACCEPTANCE.md`.
- Required commands and metrics passed, or failures are explicitly scoped and acceptable.
- The task branch is mergeable into the target branch, preferably with a dry-run or temporary integration worktree.
- Required integration smoke tests pass.
- No unresolved blocker, stale lock ambiguity, or protocol violation remains.

If any review gate fails, use `review -> changes_requested` or `review -> failed` as defined by the FSM.
