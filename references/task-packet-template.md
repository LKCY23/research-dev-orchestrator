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
  `strategy_required == (profile == full)` binding.

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
