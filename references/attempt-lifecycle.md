# Attempt Lifecycle

`ATTEMPT.json` is the worker execution source of truth for one task attempt. Keep task progress in `STATUS.json`; keep worker/process lifecycle in `ATTEMPT.json`.

An attempt is one bounded supervision and audit slice, not a worker identity. Ordinary retries and coordinator feedback create a new attempt while reusing the task's logical worker, worktree, and native backend session. See `execution-profiles.md`.

Session continuity and work continuity are independent. Backend replacement starts a new native session, but an approved Full strategy may still carry compatible workflow checkpoints from a terminal prior attempt. Dispatch records those mappings in `runtime/RESUME_CONTEXT.json` and `workflow_carried_forward` events.

For a Full task in `blocked` or `changes_requested`, automatic dispatch uses an
approved strategy only when the current strategy still validates under the
installed protocol. A missing or invalid strategy routes to a new read-only
planning attempt; it must not fail before attempt creation merely because an
older `CURRENT.json` pointer exists.

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
Dispatch also maintains a dispatcher-owned recovery snapshot in
`runtime/DISPATCH_ATTEMPT.json`. Every trusted dispatcher mutation refreshes the
snapshot, including session capture and resume fallback, so reconciliation may
restore the latest known metadata from a missing or corrupt mutable
`ATTEMPT.json`, quarantine corrupt bytes, and then record the terminal outcome.
collect_status.py validates invariants across task status, the current attempt,
locks, events, and the version-routed artifact publication.
No destructive overwrite; use a new run, new attempt, or revision task.
```

## Dispatch Locking

```text
.dispatch-lock = active dispatch/worker execution mutex
LOCK           = human-readable ownership metadata
```

`dispatch_agent.sh` must acquire `.dispatch-lock` atomically with `mkdir` before starting a worker. The directory contains `attempt_id`, `pid`, and owner metadata so cleanup only removes a lock owned by the current dispatch.

Release `.dispatch-lock` after the worker process exits and handoff validation finishes, including invalid handoff. Keep `LOCK` for audit until Codex review or triage.

If dispatcher shutdown cannot verify that the worker process tree was
terminated, record the deterministic result in `runtime/CLEANUP.json`, copy it
to `ATTEMPT.json.cleanup_failure`, move the task out of its active state, and
retain `.dispatch-lock` for explicit coordinator recovery. Surviving PIDs are an
`irrecoverable` blocker; missing cleanup supervision evidence is an
`environment` blocker.

Except for the narrowly validated cleanup-failure state above, if
`.dispatch-lock` exists while `STATUS.state` is neither `planning` nor
`running`, report a protocol violation. The exception requires a blocked task,
a terminal failed attempt, matching tmux/attempt lock metadata, and the exact
blocker implied by `cleanup_failure`. Any other stale execution mutex can block
future dispatch even when the task appears ready for review or triage.

## ATTEMPT.json Schema

```json
{
  "schema_version": 2,
  "artifact_protocol_version": 2,
  "attempt_id": "A001-claude-x4p9a",
  "task_id": "T001-name",
  "task_inputs_ref": "TASK_INPUTS.json",
  "task_inputs_sha256": "...",
  "role": "worker",
  "phase": "execution",
  "strategy_id": "T001-S001",
  "strategy_sha256": "...",
  "backend_profile_sha256": "...",
  "backend_settings_sha256": "...",
  "read_policy_sha256": "...",
  "backend_id": "claude-code",
  "agent": "claude-code",
  "agent_name": "claude-worker-1",
  "worker_id": "W-claude-code-T001-name",
  "parent_attempt_id": null,
  "backend_session_id": "s8d21",
  "session_id": "s8d21",
  "execution_mode": "start",
  "requested_execution_mode": "resume",
  "requested_session_id": "s8d21",
  "resume_fallback_reason": "session_missing",
  "resume_context_sha256": "...",
  "carried_forward_workflows": ["WF-implementation"],
  "remaining_workflows": ["WF-acceptance"],
  "permission_mode": "auto",
  "state": "completed",
  "outcome": "completed",
  "handoff_valid": true,
  "handoff_state": "review",
  "verified_commit": null,
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
requested_execution_mode: originally requested start|resume|replace mode
requested_session_id: native session requested before preflight; may be empty
resume_fallback_reason: null unless resume deterministically fell back to a full-context start
resume_context_sha256: digest of derived runtime/RESUME_CONTEXT.json when Full execution materializes resume context
carried_forward_workflows/remaining_workflows: workflow ID lists compiled by dispatch; empty/absent outside Full execution
state: created|running|completed|invalid_handoff
outcome: null while active; startup_failed|execution_failed|timed_out_unfinalized|finalization_timed_out|finalization_failed|invalid_handoff|completed when terminal
phase: planning|execution
strategy_id/strategy_sha256: required for Full execution; null for planning and Direct/Delegated execution
backend_profile_sha256: digest of the pure compiled backend profile
backend_settings_sha256: digest of generated native settings when the backend uses them
read_policy_sha256: digest of runtime/READ_POLICY.json; handoff fails if it drifts
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
verified_commit: exact finalize-time Git HEAD for a completed Direct verified attempt; absent otherwise
```

## Attempt States

- `created`: `ATTEMPT.json` exists, but the worker has not been launched yet. This should be brief; stale `created` attempts should be reported as warnings.
- `running`: the worker process is executing.
- `completed`: dispatch validated the handoff and durably recorded the terminal
  attempt result. It normally applies the corresponding task transition
  immediately afterward. A crash between those writes may temporarily leave
  `STATUS.json` active while the attempt is completed; replay must revalidate
  the same publication and complete only the missing transition.
- `invalid_handoff`: the worker exited but did not produce a legal handoff request, evidence bundle, or process result.

`state` remains the compatibility lifecycle envelope. `outcome` supplies the
precise terminal cause without adding worker/process states to the task FSM:

```text
startup_failed
  No valid backend startup event was reached. Examples include authentication,
  permission confirmation, missing native session, and invalid CLI arguments.

execution_failed
  Startup succeeded, but execution exited without a candidate handoff.

timed_out_unfinalized
  The execution deadline expired before finalization began.

finalization_timed_out
  Finalization began before the execution deadline, but its independent grace
  (the interval from the execution deadline to the fixed final deadline)
  expired before a valid handoff was published.

finalization_failed
  Finalization began, but the worker exited without a candidate handoff before
  the grace deadline.

invalid_handoff
  Candidate publication or handoff bytes existed but failed deterministic
  protocol validation.

completed
  Dispatch validated and applied the handoff.
```

If a tmux runner writes an empty or non-integer `exit_code` file, classify the attempt as `invalid_handoff`, set `ended_at`, leave `exit_code = null`, append `worker_exit_without_valid_status`, and release `.dispatch-lock` after diagnostics.

If tmux dispatch reaches its configured wait timeout before the `exit_code`
file exists, dispatch cancels the tmux session, records
`outcome = timed_out_unfinalized`, moves the task to `blocked`, and releases
`.dispatch-lock` only after reconciliation succeeds. It must not return while
leaving the task permanently `running`.

Do not use attempt state to represent task success. `completed` means the attempt completed protocol handoff, not that the task is approved or merged.

## Handoff Fields

`handoff_valid` must be:

```text
true   when dispatch validated and persisted the handoff; the matching STATUS transition normally follows immediately, except during the recoverable inter-write crash window
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

## Interactive Handoff Publication

Artifact Protocol v2 uses
`attempts/<attempt-id>/runtime/HANDOFF_READY.json` only to end a supervised
worker that may otherwise remain at its input prompt:

```json
{
  "schema_version": 2,
  "artifact_protocol_version": 2,
  "publication": "handoff_ready",
  "task_id": "T001-name",
  "attempt_id": "A001-claude-x4p9a",
  "attempt_ref": "ATTEMPT.json",
  "attempt_binding_sha256": "...",
  "task_inputs_ref": "TASK_INPUTS.json",
  "task_inputs_sha256": "...",
  "handoff_ref": "HANDOFF.json",
  "handoff_sha256": "...",
  "evidence_ref": "EVIDENCE.json",
  "evidence_sha256": "...",
  "requested_state": "review",
  "source_commit": "...",
  "source_commit_sha256": "..."
}
```

It is create-once and written last by `rdo strategy submit|revise` or `rdo
finalize`. It is not task state, approval, or final handoff validation. A valid
marker lets the attempt supervisor stop the process; dispatch still owns
worktree comparison, bundle and acceptance validation, `ATTEMPT.json`
completion, source-commit comparison, and the task FSM transition. A previous
attempt's marker cannot complete a newer attempt. Recognized legacy-v0.5/v1 attempts
retain their historical `COMPLETION.json` decoder.

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

The only v2 recovery exception is a completed, valid, publication-matching
attempt during the bounded dispatcher inter-write grace period. It still
requires the matching live dispatch PID and both locks; audit emits a warning
until replay completes the missing task transition. After the grace period, or
when any identity/binding check fails, the same shape is a violation.

`STATUS.state = planning` has the same active-attempt and lock invariants, but
requires `profile = full` and `ATTEMPT.phase = planning`. A planning attempt may
not mutate the task worktree.

`STATUS.state = strategy_review` requires no active `.dispatch-lock`, a completed planning or revision-request attempt, and a valid immutable strategy revision awaiting or holding coordinator review.

`STATUS.state = review` requires:

```text
ATTEMPT.state = completed
ATTEMPT.handoff_valid = true
ATTEMPT.handoff_state = review
HANDOFF.json exists in the current attempt with requested_state=review
STATUS.state_history ends with running -> review by actor dispatch
STATUS.previous_state = running
worker exit_code = 0
the current attempt has a valid immutable EVIDENCE.json/HANDOFF.json/READY bundle
```

`STATUS.state = verified` requires a Direct task, the same
completed/valid/zero-exit invariants, `ATTEMPT.handoff_state = verified`, the
current attempt's `HANDOFF.json.requested_state = verified`, and
`HANDOFF.json.direct_self_review.performed/passed = true`.

`STATUS.state = blocked` requires:

```text
Either:
  ATTEMPT.state = completed
  ATTEMPT.handoff_valid = true
  ATTEMPT.handoff_state = blocked
  current attempt has a valid immutable EVIDENCE.json/HANDOFF.json/READY bundle
  HANDOFF.json.requested_state = blocked
  HANDOFF.json.conditional_blocker states the concrete condition
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

Monitoring labels any candidate handoff bytes from an `invalid_handoff`
attempt as `publication_state = rejected`. They remain available for audit,
but the resolved bundle is null and review, approval, merge, and dependency
consumers must reject them.

If `STATUS.state` is active while `ATTEMPT.state = completed` and
`handoff_valid = true`, first treat it as the recoverable dispatcher
inter-write window: replay the same validation and apply only the missing
transition. If replay fails, identities differ, or no matching publication
exists, report a protocol violation. An active task with
`ATTEMPT.state = invalid_handoff` remains a protocol violation.

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
EXECUTION_POLICY.json
attempts/*
EVENTS.ndjson
JOURNAL.md
reviews/*
```

Within a v2 attempt, `TASK_INPUTS.json`, `EVIDENCE.json`, `HANDOFF.json`, and
`runtime/HANDOFF_READY.json` are create-once immutable. Historical task-root
`EVIDENCE.md`/`HANDOFF.md` are audit-bearing only for recognized legacy-v0.5/v1
tasks; they are not v2 protocol files.

Use a new attempt for implementation retries, normally with `execution_mode=resume` and the same worker/session. Use a revision task such as `T001R1-*` when task scope, acceptance criteria, profile, or design changes.
