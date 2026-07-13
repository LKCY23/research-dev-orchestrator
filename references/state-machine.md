# State Machine

`state-machine.json` is the authoritative machine-readable FSM. This file explains the protocol for humans.

Task state models task progress only. Worker/process lifecycle belongs in `ATTEMPT.json`; see `attempt-lifecycle.md`.

## States

- `pending`: task packet exists and no worker has started.
- `planning`: a read-only planning attempt is producing the next immutable execution strategy revision.
- `strategy_review`: dispatch validated a strategy revision and the coordinator must approve it or request changes before execution.
- `running`: dispatch or a worker owns execution in a task worktree.
- `blocked`: dispatch determined the task cannot continue without coordinator decision, user input, environment repair, budget decision, or failure triage. This may come from a valid worker `blocked` request or an invalid handoff that needs coordinator triage.
- `review`: dispatch validated the worker's `review` request and evidence/handoff artifacts.
- `changes_requested`: the coordinator reviewed the task and requires fixes before approval.
- `approved`: the coordinator reviewed the diff and evidence, verified mergeability against the target branch, and passed required integration smoke tests. The task is ready to merge but has not yet been merged.
- `merged`: the approved branch has been merged into the target branch, post-merge status is recorded, post-merge smoke test result is recorded if required by `ACCEPTANCE.md`, and result/final artifacts are updated when applicable.
- `failed`: the coordinator determines the task should stop under current requirements.

## Writer Boundaries

- `create_task.py` only creates `pending`.
- Planning dispatch may perform `pending|blocked|changes_requested -> planning`.
- Planning handoff may perform `planning -> strategy_review`.
- Execution dispatch may perform `strategy_review -> running` only for an approved strategy digest. It may also perform an explicit `blocked -> running` retry when the coordinator chooses to reuse the still-approved strategy; automatic dispatch defaults blocked tasks to a new planning attempt.
- Execution may request `running -> strategy_review` with a checkpoint and valid new strategy revision.
- Workers must not mutate `STATUS.json` terminal state. They request `review` or `blocked` by writing `HANDOFF.json`, `HANDOFF.md`, and `EVIDENCE.md`.
- Dispatch applies validated `running -> review` or `running -> blocked` transitions after worker exit.
- Coordinator review may perform `review -> approved`, `review -> changes_requested`, `review -> failed`, `blocked -> failed`, and `approved -> merged`.
- `collect_status.py` is read-only and must never mutate state.

Workers must not write `STATUS.json` states. Planning workers request strategy review through immutable strategy artifacts; execution workers request `review`, `blocked`, or a strategy revision through validated handoff artifacts. If a worker believes failure is irrecoverable, it must request `blocked` with a concrete reason.

## Attempt Invariants

Do not add worker/process failure states to the task FSM. A worker that exits without valid handoff causes dispatch to set `ATTEMPT.state = invalid_handoff` and move the task to `blocked` with `blocker_type = needs_coordinator` for triage.

`running`, `review`, and `blocked` have cross-file invariants defined in `attempt-lifecycle.md`, including `.dispatch-lock` execution mutex semantics and worker `exit_code` handoff rules.

## Changes Requested

Use a new planning attempt and strategy revision after `changes_requested`. Use a new execution attempt after strategy approval.

Create a new task such as `T001R1-*` when scope, acceptance criteria, design, or ownership changes.

## Approval Gate

Do not mark `approved` because code appears reasonable. Before `review -> approved`, Codex must verify diff quality, acceptance evidence, allowed/forbidden paths, mergeability, integration smoke tests, and lock/blocker state.
