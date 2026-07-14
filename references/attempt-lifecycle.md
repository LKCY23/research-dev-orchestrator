# Attempt Lifecycle

`ATTEMPT.json` is the worker execution source of truth for one task attempt. Keep task progress in `STATUS.json`; keep worker/process lifecycle in `ATTEMPT.json`.

An attempt is one bounded supervision and audit slice, not a worker identity. Ordinary retries and coordinator feedback create a new attempt while reusing the task's logical worker, worktree, and native backend session. See `execution-profiles.md`.

Session continuity and work continuity are independent. Backend replacement starts a new native session, but an approved Full strategy may still carry compatible workflow checkpoints from a terminal prior attempt. Dispatch records those mappings in `runtime/RESUME_CONTEXT.json` and `workflow_carried_forward` events.

## Principles

```text
Task FSM stays about task progress only.
ATTEMPT.json owns worker execution lifecycle.
Strategy revisions own reviewed execution intent; runtime workflow events own activity inside an execution attempt.

Each non-dry-run attempt also writes `runtime/STARTUP.json`. Machine startup
progresses through `process_started -> prompt_dispatched -> worker_started`, or
terminates as `worker_startup_failed`. Tmux human startup records
`tui_process_started -> prompt_submitted`, with `tui_startup_failed` when the
best-effort submission path fails. Startup evidence is separate from handoff
evidence and does not grant a task terminal state.
collect_status.py validates invariants across STATUS, ATTEMPT, LOCK, EVENTS, EVIDENCE, and HANDOFF.
No destructive overwrite; use a new run, new attempt, or revision task.
```

## Dispatch Locking

```text
.dispatch-lock = active dispatch/worker execution mutex
LOCK           = human-readable ownership metadata
```

`dispatch_agent.sh` must acquire `.dispatch-lock` atomically with `mkdir` before starting a worker. The directory contains `attempt_id`, `pid`, and owner metadata so cleanup only removes a lock owned by the current dispatch.

Release `.dispatch-lock` after the worker process exits and handoff validation finishes, including invalid handoff. Keep `LOCK` for audit until Codex review or triage.

If `.dispatch-lock` exists while `STATUS.state` is neither `planning` nor `running`, report a protocol violation. A stale execution mutex can block future dispatch even when the task appears ready for review or triage.

## ATTEMPT.json Schema

```json
{
  "attempt_id": "A001-claude-x4p9a",
  "task_id": "T001-name",
  "role": "worker",
  "phase": "execution",
  "strategy_id": "T001-S001",
  "strategy_sha256": "...",
  "backend_profile_sha256": "...",
  "backend_settings_sha256": "...",
  "backend_id": "claude-code",
  "agent": "claude-code",
  "agent_name": "claude-worker-1",
  "worker_id": "W-claude-code-T001-name",
  "parent_attempt_id": null,
  "backend_session_id": "s8d21",
  "session_id": "s8d21",
  "execution_mode": "start",
  "resume_context_sha256": "...",
  "carried_forward_workflows": ["WF-implementation"],
  "remaining_workflows": ["WF-acceptance"],
  "permission_mode": "auto",
  "state": "completed",
  "handoff_valid": true,
  "handoff_state": "review",
  "started_at": "2026-07-03T12:10:00Z",
  "ended_at": "2026-07-03T12:20:00Z",
  "exit_code": 0,
  "runtime": {
    "backend": "plain",
    "runtime_backend": "plain",
    "io_mode": "machine",
    "model": null,
    "cli": "claude",
    "command": "claude ...",
    "cwd": "/path/to/worktree"
  }
}
```

Schema constraints:

```text
attempt_id: non-empty string
task_id: non-empty string
backend_id: non-empty string, one of supported worker backends
agent: legacy backend alias; non-empty string
agent_name: non-empty string
worker_id: stable logical worker identifier for ordinary attempts on the task
parent_attempt_id: previous attempt ID for resume/replace lineage; null for the first attempt
session_id: string; may be empty only if runtime cannot provide one
execution_mode: start|resume|replace
resume_context_sha256: digest of derived runtime/RESUME_CONTEXT.json when Full execution materializes resume context
carried_forward_workflows/remaining_workflows: workflow ID lists compiled by dispatch; empty/absent outside Full execution
state: created|running|completed|invalid_handoff
phase: planning|execution
strategy_id/strategy_sha256: required for Full execution; null for planning and Direct/Delegated execution
backend_profile_sha256: digest of the pure compiled backend profile
backend_settings_sha256: digest of generated native settings when the backend uses them
started_at: non-empty valid ISO timestamp
ended_at: null for created/running; valid ISO timestamp for completed/invalid_handoff
exit_code: null for created/running; integer for completed; integer or null for invalid_handoff
runtime: object
runtime.backend/runtime_backend: plain|tmux
runtime.io_mode: machine|human
runtime.cli: non-empty string
runtime.command: non-empty string
runtime.cwd: non-empty string
runtime.model: optional/null
runtime.tmux_session: required when runtime.backend = tmux
runtime.attach_command: required when runtime.backend = tmux
```

## Attempt States

- `created`: `ATTEMPT.json` exists, but the worker has not been launched yet. This should be brief; stale `created` attempts should be reported as warnings.
- `running`: the worker process is executing.
- `completed`: the worker exited, wrote a valid `HANDOFF.json` request, and dispatch applied the requested terminal task state.
- `invalid_handoff`: the worker exited but did not produce a legal handoff request, evidence bundle, or process result.

If a tmux runner writes an empty or non-integer `exit_code` file, classify the attempt as `invalid_handoff`, set `ended_at`, leave `exit_code = null`, append `worker_exit_without_valid_status`, and release `.dispatch-lock` after diagnostics.

If tmux dispatch times out before the `exit_code` file exists, do not mark the attempt ended. Leave `ATTEMPT.state = running`, `ended_at = null`, and `exit_code = null`; keep `.dispatch-lock` and require Lock Recovery Review.

Do not use attempt state to represent task success. `completed` means the attempt completed protocol handoff, not that the task is approved or merged.

## Handoff Fields

`handoff_valid` must be:

```text
true   when dispatch validated HANDOFF.json and applied STATUS.state to strategy_review, verified, review, or blocked
false  when the worker exited without a legal handoff
null   before handoff validation
```

`handoff_state` must be:

```text
strategy_review
verified
review
blocked
null
```

## Interactive Completion Signal

`COMPLETION.json` is an attempt-local commit marker used only to end a
`tmux + human` worker that may otherwise remain at its TUI input prompt:

```json
{
  "schema_version": 1,
  "task_id": "T001-name",
  "attempt_id": "A001-claude-x4p9a",
  "phase": "planning",
  "requested_state": "strategy_review",
  "handoff_sha256": "...",
  "strategy_sha256": "...",
  "completed_at": "2026-07-14T00:00:00Z"
}
```

It is written atomically and last by `rdo strategy submit|revise` or `rdo
handoff`. It is not task state, approval, or final handoff validation. A valid
signal lets the attempt supervisor stop the interactive process; dispatch still
owns worktree checks, `HANDOFF.json` validation, `ATTEMPT.json` completion, and
the task FSM transition. A previous attempt's signal cannot complete a newer
attempt.

## Task State Invariants

`STATUS.state = running` requires:

```text
current_attempt_id exists
attempts/<current_attempt_id>/ATTEMPT.json exists
ATTEMPT.state in [created, running]
LOCK exists
LOCK.attempt_id == current_attempt_id
.dispatch-lock exists and matches current_attempt_id
.dispatch-lock pid exists, is an integer, and is alive
the current attempt phase is execution
for Full tasks, the referenced strategy revision has an approved matching SHA-256 review
```

`STATUS.state = planning` has the same active-attempt and lock invariants, but requires `ATTEMPT.phase = planning`. A planning attempt may not mutate the task worktree.

`STATUS.state = strategy_review` requires no active `.dispatch-lock`, a completed planning or revision-request attempt, and a valid immutable strategy revision awaiting or holding coordinator review.

`STATUS.state = review` requires:

```text
ATTEMPT.state = completed
ATTEMPT.handoff_valid = true
ATTEMPT.handoff_state = review
HANDOFF.json exists with _template=false and requested_state=review
STATUS.state_history ends with running -> review by actor dispatch
STATUS.previous_state = running
worker exit_code = 0
EVIDENCE.md has substantive content
HANDOFF.md has substantive content
```

`STATUS.state = verified` requires a Direct task, the same completed/valid/zero-exit invariants, `ATTEMPT.handoff_state = verified`, `HANDOFF.json.requested_state = verified`, and `HANDOFF.json.self_review.passed = true`.

`STATUS.state = blocked` requires:

```text
Either:
  ATTEMPT.state = completed
  ATTEMPT.handoff_valid = true
  ATTEMPT.handoff_state = blocked
  HANDOFF.json exists with _template=false and requested_state=blocked
  HANDOFF.md has substantive content
  worker exit_code may be zero or nonzero
or:
  ATTEMPT.state = invalid_handoff
  ATTEMPT.handoff_valid = false
  blocker_type = needs_coordinator
  blocking_reason explains the invalid handoff
STATUS.state_history ends with planning|running -> blocked by actor dispatch
STATUS.previous_state = planning|running
blocker_type in [needs_coordinator, needs_user, environment, budget, irrecoverable]
blocking_reason non-empty
```

If a worker mutates `STATUS.json` directly, dispatch must not trust that terminal state. It should mark the attempt `invalid_handoff`, move the task to `blocked` with `blocker_type = needs_coordinator`, and leave evidence for coordinator triage.

If `STATUS.state = running` and `ATTEMPT.state` is `completed` or `invalid_handoff`, report a protocol violation. Dispatch normally moves terminal attempts to `review` or `blocked`; a running task with a terminal attempt means supervision or protocol mutation failed.

For `runtime.backend = tmux`, if `attempts/<current_attempt_id>/exit_code` exists while `STATUS.state = running` and `ATTEMPT.state = running`, classify it by supervision evidence:

```text
dispatch pid alive and exit_code age <= grace period:
  report a warning; handoff validation may be in progress

dispatch pid dead, missing, invalid, or exit_code age > grace period:
  report a protocol violation
```

The violation means the tmux runner produced a completion artifact after dispatch supervision was lost or dispatch failed to update `ATTEMPT.json`; Codex must perform Lock Recovery Review.

## No Destructive Overwrite

No command may destructively overwrite or reinitialize audit-bearing artifacts. Updates must be append-only where applicable, or legal state/protocol transitions where mutable.

Audit-bearing artifacts include:

```text
STATUS.json
TASK.md
CONTEXT.md
ACCEPTANCE.md
EVIDENCE.md
HANDOFF.md
attempts/*
EVENTS.ndjson
JOURNAL.md
reviews/*
```

Use a new attempt for implementation retries, normally with `execution_mode=resume` and the same worker/session. Use a revision task such as `T001R1-*` when task scope, acceptance criteria, profile, or design changes.
