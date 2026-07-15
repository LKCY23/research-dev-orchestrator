# State Machine

`state-machine.json` is the authoritative machine-readable FSM. This file explains the protocol for humans.

Task state models task progress only. Worker/process lifecycle belongs in `ATTEMPT.json`; see `attempt-lifecycle.md`.

## States

- `pending`: task packet exists and no worker has started.
- `planning`: a read-only planning attempt is producing the next immutable execution strategy revision.
- `strategy_review`: dispatch validated a strategy revision and the coordinator must approve it or request changes before execution.
- `running`: dispatch or a worker owns execution in a task worktree.
- `blocked`: dispatch determined the task cannot continue without coordinator decision, user input, environment repair, budget decision, or failure triage. This may come from a valid worker `blocked` request or an invalid handoff that needs coordinator triage.
- `verified`: a Direct worker completed implementation, tests, and structured self-review; only mechanical merge gates remain.
- `review`: dispatch validated the worker's `review` request and evidence/handoff artifacts.
- `changes_requested`: the coordinator reviewed the task and requires fixes before approval.
- `approved`: the coordinator reviewed the diff and evidence, verified mergeability against the target branch, and passed required integration smoke tests. The task is ready to merge but has not yet been merged.
- `merged`: the approved branch commit is contained by the target branch and
  the post-merge result is recorded. This is an irreversible Git fact, not an
  assertion that post-merge verification passed; a failed v2 verification is
  exposed to dependencies as `merged_unverified`.
- `failed`: the coordinator determines the task should stop under current requirements.

## Writer Boundaries

- `create_task.py` only creates `pending`.
- Full-profile planning dispatch may perform `pending -> planning`, or `blocked|changes_requested -> planning` only when the approved strategy is absent or invalidated.
- Direct and Delegated dispatch perform `pending -> running` without a strategy ceremony.
- Planning handoff may perform `planning -> strategy_review`.
- Full execution dispatch performs `strategy_review -> running` only for an approved strategy digest. `blocked|changes_requested -> running` resumes execution when that strategy remains valid.
- Execution may request `running -> strategy_review` with a checkpoint and valid new strategy revision.
- Workers must not mutate `STATUS.json` terminal state. Direct workers request `verified`; Delegated and Full workers request `review`; any profile may request `blocked`.
- Dispatch applies validated `running -> verified|review|blocked` transitions after worker exit.
- Coordinator review may perform `review -> approved`, `review -> changes_requested`,
  `review -> failed`, and `blocked -> failed`. The coordinator-owned merge gate
  performs `verified|approved -> merged`.
- `collect_status.py` is read-only and must never mutate state.

Workers must not write `STATUS.json` states. Planning workers request strategy review through immutable strategy artifacts; execution workers request `review`, `blocked`, or a strategy revision through validated handoff artifacts. If a worker believes failure is irrecoverable, it must request `blocked` with a concrete reason.

## Attempt Invariants

Do not add worker/process failure states to the task FSM. A worker that exits without valid handoff causes dispatch to set `ATTEMPT.state = invalid_handoff` and move the task to `blocked` with `blocker_type = needs_coordinator` for triage.

`running`, `review`, and `blocked` have cross-file invariants defined in `attempt-lifecycle.md`, including `.dispatch-lock` execution mutex semantics and worker `exit_code` handoff rules.

## Changes Requested

Resume the assigned worker in a new execution attempt after ordinary implementation feedback. Return to planning and create a strategy revision only when scope, design, backend binding, workflow kind, or budget changes invalidate the approved strategy.

Create a new task such as `T001R1-*` when scope, acceptance criteria, design, or ownership changes.

## Approval Gate

Do not mark `approved` because code appears reasonable. Before `review -> approved`, Codex must verify diff quality, acceptance evidence, allowed/forbidden paths, mergeability, integration smoke tests, and lock/blocker state.

For v2, the immutable task review decision binds the exact approved task commit
and the reviewed task-input/evidence/handoff/READY digests. Direct `verified`
binds the same attempt-local closure plus its worker self-review. `rdo task
merge` is the public coordinator surface for `approved|verified -> merged`; it
performs only a fast-forward merge and reconciles a prior Git merge by ancestry
instead of relying on an additional merge transaction artifact.
