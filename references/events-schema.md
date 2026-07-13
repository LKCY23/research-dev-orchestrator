# EVENTS.ndjson Schema

`EVENTS.ndjson` is the append-only machine-readable timeline for a run. It is required for long-running, cross-session work.

Each line is one JSON object. Do not rewrite old lines during normal operation.

## Required Fields

```json
{
  "at": "2026-07-03T12:00:00Z",
  "actor": "coordinator",
  "event": "task_created",
  "run_id": "20260703T120405Z-rag-benchmark-a7f3c2"
}
```

Task events should include `task_id`. Attempt events should include `attempt_id`.

## Core Event Types

```text
run_created
requirements_updated
design_method_selected
adr_added
task_created
task_dispatched
strategy_submitted
strategy_reviewed
strategy_review_ready
strategy_revision_requested
workflow_started
workflow_heartbeat
workflow_completed
workflow_timed_out
worker_instruction_submitted
worker_interrupted
worker_terminated
attempt_timed_out
worker_blocked
worker_review_ready
worker_exit_without_valid_status
dispatch_lock_removed
coordinator_reviewed
codex_reviewed
changes_requested
task_approved
task_merged
task_failed
experiment_recorded
scope_changed
session_closed
```

Do not record every small edit. Record events needed to reconstruct the history of requirements, design, dispatch, review, merge, experiments, blockers, and session closeout.

`strategy_review_ready`, `worker_review_ready`, `worker_blocked`, and `worker_exit_without_valid_status` describe worker outcomes, but the event actor is `dispatch` because dispatch validates handoff and applies the task transition. Workers do not write terminal `STATUS.json` transitions directly.

`dispatch_lock_removed` records a user-approved recovery action that removed a stale `.dispatch-lock`. It must include `task_id`, should include `attempt_id` when known, and should include `reason` plus a diagnostics `snapshot` path.

## Validation

Malformed JSON, missing required fields, and wrong `run_id` are protocol violations.

Unknown event types are warnings, not fatal errors. This allows future extension without breaking older runs.

Task events should include `task_id`; attempt events should include `attempt_id`.
