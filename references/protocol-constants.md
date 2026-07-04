# Protocol Constants

This file collects user-facing constants that scripts and references should keep aligned.

The script-level source for these constants is `scripts/protocol.py`. This reference is the human-readable explanation, not an import target.

## Worker Backends

```text
plain
tmux
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

## Task States

```text
pending
running
blocked
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
worker_blocked
worker_review_ready
worker_exit_without_valid_status
dispatch_lock_removed
codex_reviewed
changes_requested
task_approved
task_merged
task_failed
experiment_recorded
scope_changed
session_closed
```
