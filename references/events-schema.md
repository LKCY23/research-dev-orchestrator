# EVENTS.ndjson Schema

`EVENTS.ndjson` is the append-only machine-readable timeline for a run. It is required for long-running, cross-session work.

Each line is one JSON object. Do not rewrite old lines during normal operation.

## Required Fields

```json
{
  "at": "2026-07-03T12:00:00Z",
  "actor": "codex",
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
worker_blocked
worker_review_ready
worker_exit_without_valid_status
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

## Validation

Malformed JSON, missing required fields, and wrong `run_id` are protocol violations.

Unknown event types are warnings, not fatal errors. This allows future extension without breaking older runs.

Task events should include `task_id`; attempt events should include `attempt_id`.
