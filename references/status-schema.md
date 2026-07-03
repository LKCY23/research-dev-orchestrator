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
  "needs_codex": false,
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

Always include `task_id`, `state`, `previous_state`, `owner`, `branch`, `worktree`, `updated_at`, `needs_codex`, `summary`, `blocking_reason`, `blocker_type`, `current_attempt_id`, `assigned_worker`, `evidence`, and `state_history`.

For `pending`, `previous_state`, `current_attempt_id`, and `assigned_worker` may be `null`.

For `blocked`, `blocker_type` is required and must be one of:

```text
needs_codex
needs_user
environment
budget
irrecoverable
```

## Evidence Summary

`STATUS.json.evidence` is only an index and summary. The evidence sources of truth are:

```text
EVIDENCE.md
logs/*
attempts/*/result.md
```

If the summary conflicts with evidence files, report a protocol violation and do not auto-repair it.
