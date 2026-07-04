# Task Packet Template

Each task packet lives under `.agent-collab/runs/<run-id>/tasks/<task-id>/`.

## Required Files

```text
TASK.md
CONTEXT.md
ACCEPTANCE.md
STATUS.json
HANDOFF.md
EVIDENCE.md
logs/
attempts/
```

`LOCK` is human-readable ownership metadata. `.dispatch-lock/` is present only while active dispatch/worker execution is held. `create_task.py` must not create either file.

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
Metrics or thresholds
Smoke test
Failure handoff condition
Post-merge smoke test, if required
```

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

## Fix Routing

Use a new attempt in the same task for a small fix with the same acceptance criteria.

Create a new task such as `T001R1-*` when scope, acceptance criteria, design, ownership, or allowed paths change.
