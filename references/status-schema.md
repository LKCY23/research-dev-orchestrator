# STATUS.json Schema

`STATUS.json` is the task state source of truth. Keep it valid JSON.

## Shape

```json
{
  "task_id": "T001-name",
  "state": "review",
  "previous_state": "running",
  "owner": "claude-code",
  "branch": "agent/T001-name",
  "worktree": ".agent-worktrees/T001-name",
  "updated_at": "2026-07-03T12:00:00Z",
  "needs_coordinator": false,
  "summary": "",
  "blocking_reason": "",
  "blocker_type": "",
  "current_attempt_id": "A001-claude-x4p9a",
  "assigned_worker": {
    "agent": "claude-code",
    "agent_name": "claude-worker-1",
    "session_id": "s8d21",
    "role": "worker"
  },
  "evidence": {
    "commands_run": [],
    "logs": [],
    "passed": null
  },
  "state_history": [
    {
      "from": "pending",
      "to": "running",
      "actor": "dispatch",
      "at": "2026-07-03T12:00:00Z"
    }
  ]
}
```

## Required Fields

Always include `task_id`, `state`, `previous_state`, `owner`, `branch`, `worktree`, `updated_at`, `needs_coordinator`, `summary`, `blocking_reason`, `blocker_type`, `current_attempt_id`, `assigned_worker`, `evidence`, and `state_history`.

For `pending`, `previous_state`, `current_attempt_id`, and `assigned_worker` may be `null`.

For `blocked`, `blocker_type` is required and must be one of:

```text
needs_coordinator
needs_user
environment
budget
irrecoverable
```

Meanings:

```text
needs_coordinator
  Needs coordinator judgment, task split, design decision, review, merge/conflict handling, or acceptance clarification.

needs_user
  Needs user input, authorization, preference, data access, or research decision.

environment
  Blocked by dependency, data, hardware, service, permission, filesystem, or local/remote runtime condition.

budget
  Continuing would exceed or has exceeded time, token, compute, cost, or context budget.

irrecoverable
  Worker believes the task cannot be completed under current requirements; coordinator must decide failed, revision task, or scope change.
```

## Evidence Summary

`STATUS.json.evidence` is only an index and summary. The evidence sources of truth are:

```text
EVIDENCE.md
logs/*
attempts/*/result.md
```

If the summary conflicts with evidence files, report a protocol violation and do not auto-repair it.

Template-only `EVIDENCE.md` or `HANDOFF.md` files with `RDO_TEMPLATE` markers are not valid evidence or handoff content.

## Attempt Invariants

`current_attempt_id` points to the current `attempts/<attempt-id>/ATTEMPT.json`.

`STATUS.state = running` requires matching `LOCK` metadata, an active `.dispatch-lock`, and an attempt whose state is `created` or `running`.

For tmux dispatch timeout before the attempt-local `exit_code` file appears, `STATUS.state` remains `running`, `ATTEMPT.state` remains `running`, and `.dispatch-lock` remains in place until Lock Recovery Review.

`STATUS.state = review` requires `previous_state = running`, a completed attempt with `handoff_valid = true`, `handoff_state = review`, worker `exit_code = 0`, and substantive `EVIDENCE.md` and `HANDOFF.md`.

`STATUS.state = blocked` requires `previous_state = running`, a completed attempt with `handoff_valid = true`, `handoff_state = blocked`, substantive `HANDOFF.md`, valid `blocker_type`, and non-empty `blocking_reason`. Blocked handoff may have a zero or nonzero worker `exit_code`.
