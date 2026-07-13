# Task Packet Template

Each task packet lives under `.agent-collab/runs/<run-id>/tasks/<task-id>/`.

## Required Files

```text
TASK.md
CONTEXT.md
ACCEPTANCE.md
STATUS.json
HANDOFF.md
HANDOFF.json
EVIDENCE.md
logs/
attempts/
```

`LOCK` is human-readable ownership metadata. `.dispatch-lock/` is present only while an active planning or execution dispatch is held. `create_task.py` must not create either file.

## TASK.md

```yaml
task_id:
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

Workers must remove `<!-- RDO_TEMPLATE: EVIDENCE -->` and write:

```text
Commands Run
Tests Passed
Metrics / Outputs
Logs
Known Limitations
```

## HANDOFF.md

Workers must remove `<!-- RDO_TEMPLATE: HANDOFF -->` and write enough for Codex review or unblock:

```text
What changed
What failed
Evidence
Decision needed
Suggested next action
```

## HANDOFF.json

`HANDOFF.json` is the machine-readable transition request. It does not replace `HANDOFF.md`, but it is required for worker terminal handoff in protocol v0.2.

Workers must set `_template` to `false`, choose `requested_state = review|blocked`, and keep fields concise:

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

Dispatch validates this request and applies `STATUS.json` terminal transitions. Workers must not edit `STATUS.json` directly. `collect_status.py` may also display this index for dashboards and summaries.

## Fix Routing

Use a new attempt in the same task for a small fix with the same acceptance criteria.

Create a new task such as `T001R1-*` when scope, acceptance criteria, design, ownership, or allowed paths change.
