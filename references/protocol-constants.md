# Protocol Constants

This file collects user-facing constants that scripts and references should keep aligned.

The script-level source for these constants is `scripts/protocol.py`. This reference is the human-readable explanation, not an import target.

## Worker Backends

```text
claude-code
codex
opencode
kimi-code
```

## Runtime Backends

```text
plain
tmux
```

## IO Modes

```text
machine
human
```

## Permission Modes

```text
default
auto
yolo
```

## Dispatch Exit Codes

```text
0  success
2  usage or configuration error
3  active dispatch lock already exists
4  worker exited or completed but did not produce a valid handoff
5  tmux wait timeout before attempt-local exit_code file appeared
```

Exit code `5` belongs to `dispatch_claude.sh`, not the worker. It must not be written to `ATTEMPT.exit_code`.

## Blocker Types

```text
needs_coordinator
needs_user
environment
budget
irrecoverable
```

## Attempt States

```text
created
running
completed
invalid_handoff
```

## Execution Profiles

```text
direct
delegated
full
```

## Execution Modes

```text
start
resume
replace
```

## Task States

```text
pending
planning
strategy_review
running
blocked
verified
review
changes_requested
approved
merged
failed
```

## Core Event Types

```text
run_created
requirements_updated
design_method_selected
adr_added
task_created
task_dispatched
worker_process_started
prompt_dispatched
worker_started
worker_waiting_for_user
worker_startup_failed
strategy_submitted
strategy_reviewed
strategy_review_ready
strategy_revision_requested
workflow_started
workflow_heartbeat
workflow_completed
workflow_carried_forward
workflow_timed_out
worker_instruction_submitted
worker_interrupted
worker_terminated
attempt_timed_out
worker_blocked
worker_review_ready
worker_verified
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
