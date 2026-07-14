# Task Packet Template

Each task packet lives under `.agent-collab/runs/<run-id>/tasks/<task-id>/`.

## Required Files

```text
TASK.md
STATUS.json
HANDOFF.md
HANDOFF.json
EVIDENCE.md
logs/
attempts/
```

`TASK.md` and `STATUS.json` are canonical. `CONTEXT.md` and `ACCEPTANCE.md` are optional normalization files when that information is not already complete in `TASK.md`. Handoff prose and evidence views support humans; `HANDOFF.json` remains the transition request.

`LOCK` is human-readable ownership metadata. `.dispatch-lock/` is present only while an active planning or execution dispatch is held. `create_task.py` must not create either file.

## TASK.md

```yaml
task_id:
profile: direct|delegated|full
goal:
allowed_paths:
forbidden_paths:
dependencies:
branch:
worktree:
non_goals:
```

Keep `allowed_paths` narrow. If two tasks have overlapping critical paths, do not dispatch them in parallel.

## CONTEXT.md

Include only task-relevant context:

- requirement/design links
- related ADRs
- current interfaces and expected data flow
- constraints that the worker must not rediscover

Do not paste the entire conversation.

## ACCEPTANCE.md

Include:

```text
Required commands
Expected outputs
Smoke tests
Metrics or thresholds
Merge preconditions
Failure handoff conditions
Post-merge smoke test, if required
```

Treat this file as the review gate recipe. It should answer what the worker must run, what artifacts must exist, what thresholds matter, and what Codex must verify before approval or merge.

## EVIDENCE.md

`rdo finalize` generates this human-readable evidence view from workflow and command records:

```text
Commands Run
Tests Passed
Metrics / Outputs
Logs
Known Limitations
```

## HANDOFF.md

`rdo finalize` generates this human-readable summary from the worker's final summary and recorded evidence:

```text
What changed
What failed
Evidence
Decision needed
Suggested next action
```

## HANDOFF.json

`HANDOFF.json` is the canonical machine-readable transition request. `HANDOFF.md` is its generated human-readable companion.

Workers call `rdo finalize`; it sets `_template=false` and writes the request atomically. Direct requests `verified|blocked`; Delegated requests `review|blocked`; Full may request `strategy_review|review|blocked`.

Before a final `verified` or `review` handoff, execution workers commit all task changes on the assigned branch and leave the task worktree clean. Finalization derives `files_changed` from the task's first pre-execution fingerprint rather than from unstaged Git diff alone.

```json
{
  "_template": false,
  "requested_state": "review",
  "summary": "Implemented loader and added tests.",
  "commands_run": ["pytest -q tests/test_loader.py"],
  "files_changed": ["src/loader.py", "tests/test_loader.py"],
  "known_limitations": [],
  "needs_coordinator": false,
  "blocker_type": "",
  "blocking_reason": ""
}
```

Direct handoff additionally sets `self_review.passed = true` and records findings/fixes after inspecting the final diff.

Dispatch validates this request and applies `STATUS.json` terminal transitions. Workers must not edit `STATUS.json` directly. `collect_status.py` may also display this index for dashboards and summaries.

## Fix Routing

Use a new attempt in the same task for a small fix with the same acceptance criteria. Resume the same logical worker and native backend session unless there is a recorded reason to replace it.

Create a new task such as `T001R1-*` when scope, acceptance criteria, design, ownership, or allowed paths change.
