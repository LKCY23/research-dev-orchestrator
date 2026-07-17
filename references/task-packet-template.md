# Task Packet Contract

Artifact Protocol v2 gives each task-root input one responsibility. All four
files are required and dispatch validates them before allocating an attempt,
acquiring locks, creating a worktree, or mutating task execution state.

## Authoring gate

Select the task profile explicitly only after checking the task-size rules in
[execution-profiles.md](execution-profiles.md). A dispatchable task should name
one primary trust boundary, one independently acceptable outcome, and a
cohesive deliverable group. Split the task when parts can independently pass,
fail, ship, roll back, or require different permission/platform mechanisms.

Use the canonical sections to make that boundary visible:

- `Objective`: state the one independently acceptable outcome.
- `Deliverables`: include only outputs needed to establish that outcome.
- `Invariants`: state the trust property and compatibility conditions that must
  remain true.
- `Non-goals`: name adjacent trust boundaries intentionally assigned to other
  tasks.
- `ACCEPTANCE.md`: contain one cohesive acceptance group for the task boundary,
  even when it has several commands.

Task size, risk, and review needs require coordinator judgment. Do not implement
keyword, file-count, path-count, or duration heuristics that infer a profile or
silently convert a task to Full.

## Canonical inputs

- `TASK.md` contains exactly Objective, Deliverables, Invariants, Non-goals,
  and Dependencies. Dependencies live in the single
  `json rdo-task-dependencies` block and resolve to merged task commits.
- `CONTEXT.md` is non-normative. It contains Frozen Decisions, Required
  Interfaces, Local Code Map, and Necessary Background. It cannot add task
  obligations or read-policy paths.
- `ACCEPTANCE.md` contains one `json rdo-acceptance-contract` block with exact
  required commands, required outputs, and pre/post-merge commands. Its prose
  sections carry behavioral and coordinator judgment.
- `EXECUTION_POLICY.json` owns execution limits, the explicit `allowed_paths`,
  `read_paths`, `forbidden_paths`, and `context_sources`, plus the deterministic
  `strategy_required == (profile == full)` binding. Protocol v2 may also set
  `task_budget` to a non-empty subset of `max_attempts`,
  `max_execution_seconds`, and `max_cost_usd`; each value is a positive hard
  cumulative limit. `null` or an omitted field preserves existing behavior.

Task cumulative limits are distinct from a Full strategy's attempt-local
`resource_budget`. Attempts are counted after `ATTEMPT.json` is created,
including startup and execution failures. A backend preflight failure before
attempt creation is free, and a same-attempt runtime fallback is still one
attempt. Execution time stops when finalization begins, so the independent T1
finalization grace is never charged to or shortened by the task budget. Cost
is accepted only from an observable backend usage stream; missing historical
cost evidence blocks admission instead of being treated as zero.
For an enabled metered dimension, terminal handoff also requires the current
attempt's bound execution/cost receipt; a task cannot reach review or verified
with an unobservable cumulative total.

`STATUS.json` owns task state, profile, branch, worktree, and current attempt.
Those controls do not belong in `TASK.md`.

`create_task.py` intentionally leaves visible `RDO_TEMPLATE_INCOMPLETE`
markers in fields that require coordinator authorship. A task with any marker
is not dispatchable.

## Derived attempt inputs

After a successful readiness check, dispatch derives immutable
`attempts/<attempt-id>/TASK_INPUTS.json`. It binds the four input digests, task
base commit, resolved dependency commits, and a stable contract digest.
`ATTEMPT.json` references this file by path and exact digest. A later attempt
with a different stable contract is rejected and requires a revision task.

When `task_budget` is enabled, dispatch also derives immutable
`runtime/TASK_BUDGET.json` while holding the task dispatch lock. It records the
source attempt digests, consumed and remaining amounts, the effective next
attempt wall/cost caps, and the admission decision. `ATTEMPT.json` binds this
snapshot and its assessment digest. It is an audit snapshot, not a mutable
counter; every later decision is recomputed from frozen attempt evidence.

## Attempt outputs

Workers never author task-root handoff or evidence files. Required acceptance
commands run through:

```text
rdo check --attempt-dir <attempt-dir> --check-id <id>
```

Finalization publishes, in order:

```text
attempts/<attempt-id>/EVIDENCE.json
attempts/<attempt-id>/HANDOFF.json
attempts/<attempt-id>/runtime/HANDOFF_READY.json
```

The first two files are create-once and immutable; the READY marker binds their
digests and is written last. `HANDOFF.json` is only a transition request.
`EVIDENCE.json` is a frozen index over raw command, log, review, commit, and
worktree facts.

See [artifact-protocol-v2.md](artifact-protocol-v2.md) for the complete schemas,
ownership rules, publication order, and explicit legacy compatibility route.
