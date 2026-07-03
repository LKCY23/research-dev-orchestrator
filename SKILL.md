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
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id>
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --json
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --write-summary
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --write-diagnostics
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/close_session.py" --run-id <run-id> --summary "<session summary>" --changed "<change>" --next-action "<next>"
```

`init_run.py` scaffolds only. It must not make substantive research, design, or architecture decisions.

`create_task.py` creates `pending` tasks only. It must not overwrite existing tasks, dispatch, create locks, or merge.

`dispatch_claude.sh` may transition `pending|blocked|changes_requested -> running`, create a lock, create an attempt, call a configured worker CLI, and verify whether the worker wrote a valid terminal handoff state. It gives the worker absolute protocol file paths because the worker runs inside a task worktree while `.agent-collab` lives in the target repository root. It must update `ATTEMPT.json` lifecycle fields and must not synthesize `review` or `blocked` for the worker.

`collect_status.py` is read-only by default. It must not modify `STATUS.json`, delete locks, change FSM state, or repair violations. `--write-summary` may update only `SUMMARY.md`; `--write-diagnostics` may write only diagnostics files.

`close_session.py` is the standard session closeout command. It updates derived `SUMMARY.md`, appends a human-readable `JOURNAL.md` entry, and appends a `session_closed` event to `EVENTS.ndjson`.

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
