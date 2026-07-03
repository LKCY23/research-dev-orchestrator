# Attempt Lifecycle

`ATTEMPT.json` is the worker execution source of truth for one task attempt. Keep task progress in `STATUS.json`; keep worker/process lifecycle in `ATTEMPT.json`.

## Principles

```text
Task FSM stays about task progress only.
ATTEMPT.json owns worker execution lifecycle.
collect_status.py validates invariants across STATUS, ATTEMPT, LOCK, EVENTS, EVIDENCE, and HANDOFF.
No destructive overwrite; use a new run, new attempt, or revision task.
```

## ATTEMPT.json Schema

```json
{
  "attempt_id": "A001-claude-x4p9a",
  "task_id": "T001-name",
  "agent": "claude-code",
  "agent_name": "claude-worker-1",
  "session_id": "s8d21",
  "state": "completed",
  "handoff_valid": true,
  "handoff_state": "review",
  "started_at": "2026-07-03T12:10:00Z",
  "ended_at": "2026-07-03T12:20:00Z",
  "exit_code": 0,
  "runtime": {
    "model": null,
    "cli": "claude",
    "command": "claude ...",
    "cwd": "/path/to/worktree"
  }
}
```

## Attempt States

- `created`: `ATTEMPT.json` exists, but the worker has not been launched yet. This should be brief; stale `created` attempts should be reported as warnings.
- `running`: the worker process is executing.
- `completed`: the worker exited and made a valid protocol handoff to `review` or `blocked`.
- `invalid_handoff`: the worker exited but did not produce a legal status/evidence/handoff.

Do not use attempt state to represent task success. `completed` means the attempt completed protocol handoff, not that the task is approved or merged.

## Handoff Fields

`handoff_valid` must be:

```text
true   when the worker legally moved STATUS.state to review or blocked and wrote required artifacts
false  when the worker exited without a legal handoff
null   before handoff validation
```

`handoff_state` must be:

```text
review
blocked
null
```

## Task State Invariants

`STATUS.state = running` requires:

```text
current_attempt_id exists
attempts/<current_attempt_id>/ATTEMPT.json exists
ATTEMPT.state in [created, running]
LOCK exists
LOCK.attempt_id == current_attempt_id
```

`STATUS.state = review` requires:

```text
ATTEMPT.state = completed
ATTEMPT.handoff_valid = true
ATTEMPT.handoff_state = review
STATUS.state_history ends with running -> review by actor claude-code
EVIDENCE.md has substantive content
HANDOFF.md has substantive content
```

`STATUS.state = blocked` requires:

```text
ATTEMPT.state = completed
ATTEMPT.handoff_valid = true
ATTEMPT.handoff_state = blocked
STATUS.state_history ends with running -> blocked by actor claude-code
HANDOFF.md has substantive content
blocker_type valid
blocking_reason non-empty
```

If `STATUS.state = running` and `ATTEMPT.state` is `completed` or `invalid_handoff`, report a protocol violation. Codex must inspect the attempt and decide whether to re-dispatch, request changes, mark blocked, or fail the task.

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

Use a new attempt for implementation retries. Use a revision task such as `T001R1-*` when task scope, acceptance criteria, or design changes.
