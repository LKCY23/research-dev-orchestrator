# Dispatch Lock Recovery

Use this reference when `collect_status.py` reports `.dispatch-lock` anomalies. Recovery is a review workflow, not automatic repair.

## Roles

```text
collect_status.py  detects protocol anomalies only
coordinator        reviews evidence and recommends action
user               approves or rejects cleanup
remove_dispatch_lock.py performs the approved minimal mutation
EVENTS + diagnostics preserve the audit trail
```

Never add `--fix` behavior to `collect_status.py`.

## Lock Roles

```text
.dispatch-lock = active dispatch/worker execution mutex
LOCK           = human-readable ownership/audit metadata
```

`.dispatch-lock` answers whether execution is currently owned. `LOCK` records who ran what and may remain after execution for review or triage.

## Review Triggers

Run a Lock Recovery Review when any of these appear:

```text
STATUS.state != running but .dispatch-lock exists
STATUS.state = running but .dispatch-lock/attempt_id does not match current_attempt_id
STATUS.state = running but ATTEMPT.state is completed or invalid_handoff
.dispatch-lock age exceeds the stale threshold
.dispatch-lock/pid is missing
.dispatch-lock/pid is not alive
```

These are triggers for judgment, not automatic deletion rules.

## Review Checklist

Inspect:

```text
STATUS.json
ATTEMPT.json
LOCK
.dispatch-lock/*
attempts/<attempt>/transcript.log
attempts/<attempt>/result.md
recent EVENTS.ndjson entries
HANDOFF.md
EVIDENCE.md
git worktree and branch state
.dispatch-lock/pid liveness
whether transcript.log is still growing
```

Classify the lock:

```text
active
  Worker or dispatch may still be running. Do not remove.

stale
  Worker or dispatch clearly ended. Recommend removing only .dispatch-lock.

ambiguous
  Evidence is insufficient. Continue observing or ask the user.
```

## User-Facing Review Format

Before cleanup, report:

```text
Finding
Evidence
Risk
Recommendation
Proposed mutation
```

`Proposed mutation` must be explicit:

```text
Will:
  snapshot tasks/<task>/.dispatch-lock -> diagnostics/
  write recovery-operation.json in the snapshot
  remove tasks/<task>/.dispatch-lock
  append dispatch_lock_removed to EVENTS.ndjson

Will not:
  modify STATUS.json
  modify ATTEMPT.json
  modify HANDOFF.md
  modify EVIDENCE.md
  remove LOCK
  change FSM state
```

## Mechanical Cleanup

Use `scripts/remove_dispatch_lock.py` only after user approval:

```bash
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/remove_dispatch_lock.py" \
  --run-id <run-id> \
  --task-id <task-id> \
  --reason "stale after completed review handoff" \
  --confirmed
```

Without `--confirmed`, the script is a dry run and must not modify files.

The script must:

```text
read STATUS.current_attempt_id
snapshot .dispatch-lock to diagnostics/dispatch-lock-removed-<task>-<timestamp>/
write recovery-operation.json in the snapshot before removal
remove .dispatch-lock
append dispatch_lock_removed event after removal succeeds
```

If appending `dispatch_lock_removed` fails after removal, write `recovery-event-append-failed.json` in the snapshot and exit nonzero. The snapshot must contain enough information to audit the mutation even when the timeline append failed.

The script must not decide whether a lock is stale.
