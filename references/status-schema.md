# STATUS.json Schema

`STATUS.json` is the coordinator-owned task-state source of truth. It is not a
worker result, command log, or handoff artifact. Artifact Protocol v2 is the
default for new tasks; recognized historical tasks are read only through the
explicit legacy-v0.5 or legacy-v1 compatibility path selected by their
discriminator.

## V2 shape

```json
{
  "task_id": "T001-name",
  "artifact_protocol_version": 2,
  "profile": "delegated",
  "state": "review",
  "previous_state": "running",
  "owner": "worker",
  "branch": "agent/T001-name",
  "worktree": ".agent-worktrees/T001-name",
  "updated_at": "2026-07-03T12:00:00Z",
  "needs_coordinator": false,
  "summary": "Implementation is ready for coordinator review.",
  "blocking_reason": "",
  "blocker_type": "",
  "current_attempt_id": "A001-claude-x4p9a",
  "assigned_worker": {
    "backend_id": "claude-code",
    "agent": "claude-code",
    "agent_name": "claude-worker-1",
    "worker_id": "W-claude-code-T001-name",
    "first_attempt_id": "A001-claude-x4p9a",
    "latest_attempt_id": "A001-claude-x4p9a",
    "backend_session_id": "s8d21",
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
      "from": "running",
      "to": "review",
      "actor": "dispatch",
      "at": "2026-07-03T12:00:00Z"
    }
  ]
}
```

V2 requires `artifact_protocol_version = 2`. Always include `task_id`,
`profile`, `state`, `previous_state`, `owner`, `branch`, `worktree`,
`updated_at`, `needs_coordinator`, `summary`, `blocking_reason`,
`blocker_type`, `current_attempt_id`, `assigned_worker`, `evidence`, and
`state_history`. For `pending`, `previous_state`, `current_attempt_id`, and
`assigned_worker` may be `null`.

Artifact Protocol v2 never treats a missing `profile` as Full. The profile must
be one explicit value from `direct|delegated|full`; only a recognized legacy
decoder may apply its historical Full fallback. Readiness and status audit bind
the value to the task's unique `task_created` event, so changing
`STATUS.profile` requires a revision task rather than an in-place edit.

The `evidence` object remains in the shared status shape for compatibility and
compact display. It is not a v2 evidence index and cannot satisfy a gate. V2
consumers resolve the current attempt and validate its exact
`TASK_INPUTS.json`, `EVIDENCE.json`, `HANDOFF.json`, and
`runtime/HANDOFF_READY.json` bindings. They never fall back to task-root
handoff/evidence files.

Optional cumulative task budgets do not add fields to mutable `STATUS.json` or
new FSM states. `rdo status` and `collect_status.py` derive a `task_budget`
projection from frozen attempt/deadline/usage evidence. It reports limits,
consumed, remaining, observation gaps, and an admission object. A denied
admission uses `blocker_type = budget` in that projection even when the durable
task state remains `pending`, `blocked`, or `changes_requested`; dispatch is
the enforcing gate and will not create another attempt.

## Blocker types

For `blocked`, `blocker_type` is required and must be one of:

```text
needs_coordinator  coordinator judgment, review, merge, split, or clarification
needs_user         user input, authorization, preference, data, or research decision
environment        dependency, data, hardware, service, permission, or runtime condition
budget             time, token, compute, cost, or context boundary
irrecoverable      cannot complete under the current task contract
```

## Attempt and state invariants

`current_attempt_id` points to
`attempts/<current_attempt_id>/ATTEMPT.json`. That attempt references its
attempt-local `TASK_INPUTS.json` by exact path and digest; it does not duplicate
the four canonical input digests.

`STATUS.state = planning|running` requires matching `LOCK` metadata, an active
`.dispatch-lock`, and an attempt whose state is `created` or `running`. The
attempt phase must match task state. The sole v2 exception is the short
dispatcher inter-write recovery window: `ATTEMPT.state = completed` with a
matching valid publication may coexist temporarily with active `STATUS` only
while the matching dispatch PID is alive and the configured grace period has
not elapsed. `ATTEMPT.ended_at` must not be in the future; future timestamps do
not extend or reset the recovery window. Audit reports the bounded case as a
warning; otherwise it is a protocol violation. A tmux wait timeout before the
attempt-local `exit_code` appears
leaves both task and attempt running and retains the lock until Lock Recovery
Review.

`STATUS.state = strategy_review` requires a completed Full planning or
execution-revision attempt, a valid immutable submitted strategy revision bound
to the handoff, and no active dispatch lock.

`STATUS.state = review` requires all of the following:

```text
profile = delegated|full
previous_state = running
current ATTEMPT.state = completed
ATTEMPT.handoff_valid = true
ATTEMPT.handoff_state = review
worker exit_code = 0
current attempt has a complete, digest-valid v2 publication bundle
HANDOFF.json.requested_state = review
HANDOFF/EVIDENCE source_commit equals the clean task-worktree HEAD frozen by finalize
required rdo check records and required outputs satisfy frozen ACCEPTANCE.md
final running -> review transition was written by dispatch
```

`STATUS.state = verified` is Direct-only. It requires the same completed,
zero-exit, digest-valid publication and source-commit boundary, plus
`HANDOFF.json.requested_state = verified` and a substantive
`direct_self_review` with `performed = true` and `passed = true`.

`STATUS.state = blocked` requires `previous_state = planning|running`, a valid
`blocker_type`, and a non-empty `blocking_reason`. A normal blocked request has
a completed attempt, a valid attempt-local publication with
`HANDOFF.json.requested_state = blocked`, and a concrete
`conditional_blocker`. An invalid or missing publication instead produces the
compatibility envelope `ATTEMPT.state = invalid_handoff`,
`handoff_valid = false`, plus a precise `ATTEMPT.outcome`. Startup failures may
require `environment` or `needs_user`, timeouts use `budget`, and execution or
handoff failures require coordinator triage. Dispatch, never the worker, writes
the final task transition.
Monitoring reports candidate bytes from this invalid-handoff case as
`publication_state = rejected`; the bundle remains null and strict consumers
must not treat those bytes as published evidence.

`STATUS.state = approved` requires an immutable coordinator review decision.
For v2, that decision binds the exact clean source commit and the current
attempt's task-input, evidence, handoff, and READY artifact digests.

`STATUS.state = merged` requires a matching `task_merged` event whose exact
commit is contained by `RUN.json.target_branch`. Delegated/Full merge revalidates
the approved review binding. Direct merge revalidates the completed verified
attempt's exact source commit and artifact bundle. A content fingerprint is an
additional check, never a substitute for the Git commit boundary. Every v2
merge event contains `verification`; when `verification.passed = false`, the
irreversible Git fact remains `merged` but dependency resolution exposes
`merged_unverified`, which cannot satisfy another task's
`required_state = merged` readiness gate.

## Legacy boundary

Recognized legacy-v0.5/v1 tasks retain their historical task-root `HANDOFF.md`,
`HANDOFF.json`, `EVIDENCE.md`, status evidence summary, and attempt
`COMPLETION.json` rules. Those artifacts are valid only for the legacy decoder;
they must not be copied into, inferred as, or used to satisfy a v2 task.
