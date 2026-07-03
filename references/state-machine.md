# State Machine

`state-machine.json` is the authoritative machine-readable FSM. This file explains the protocol for humans.

Task state models task progress only. Worker/process lifecycle belongs in `ATTEMPT.json`; see `attempt-lifecycle.md`.

## States

- `pending`: task packet exists and no worker has started.
- `running`: dispatch or a worker owns execution in a task worktree.
- `blocked`: worker cannot continue without Codex decision, user input, environment repair, budget decision, or failure triage.
- `review`: worker claims implementation is ready and has written evidence/handoff artifacts.
- `changes_requested`: Codex reviewed the task and requires fixes before approval.
- `approved`: Codex reviewed the diff and evidence, verified mergeability against the target branch, and passed required integration smoke tests. The task is ready to merge but has not yet been merged.
- `merged`: the approved branch has been merged into the target branch, post-merge status is recorded, post-merge smoke test result is recorded if required by `ACCEPTANCE.md`, and result/final artifacts are updated when applicable.
- `failed`: Codex determines the task should stop under current requirements.

## Writer Boundaries

- `create_task.py` only creates `pending`.
- `dispatch_claude.sh` may perform `pending -> running`, `blocked -> running`, or `changes_requested -> running`.
- Claude Code workers may perform only `running -> review` or `running -> blocked`.
- Codex review may perform `review -> approved`, `review -> changes_requested`, `review -> failed`, `blocked -> failed`, and `approved -> merged`.
- `collect_status.py` is read-only and must never mutate state.

Claude Code must not write `approved`, `merged`, `failed`, or `changes_requested`. If a worker believes failure is irrecoverable, it must write `blocked` with `blocker_type: "irrecoverable"` and a concrete `blocking_reason`.

## Attempt Invariants

Do not add worker/process failure states to the task FSM. A worker that exits without valid handoff should set `ATTEMPT.state = invalid_handoff`; `collect_status.py` reports this as a protocol violation while the task remains available for Codex triage.

`running`, `review`, and `blocked` have cross-file invariants defined in `attempt-lifecycle.md`.

## Changes Requested

Use a new attempt in the same task when the fix is small and acceptance criteria are unchanged.

Create a new task such as `T001R1-*` when scope, acceptance criteria, design, or ownership changes.

## Approval Gate

Do not mark `approved` because code appears reasonable. Before `review -> approved`, Codex must verify diff quality, acceptance evidence, allowed/forbidden paths, mergeability, integration smoke tests, and lock/blocker state.
