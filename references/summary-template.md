# SUMMARY.md Template

`SUMMARY.md` is a derived human-readable monitor. It is not a source of truth and may be deleted and regenerated.

Sources of truth remain:

```text
RUN.json
tasks/*/STATUS.json
references/state-machine.json
tasks/*/attempts/<current-attempt>/ATTEMPT.json
tasks/*/attempts/<current-attempt>/TASK_INPUTS.json
tasks/*/attempts/<current-attempt>/EVIDENCE.json
tasks/*/attempts/<current-attempt>/HANDOFF.json
tasks/*/attempts/<current-attempt>/runtime/HANDOFF_READY.json
EVENTS.ndjson
RESULT_LEDGER.md
```

The current attempt is resolved through `STATUS.current_attempt_id`, and the
complete v2 publication must validate before its fields are rendered. A
recognized legacy-v0.5/v1 task is rendered through its explicit legacy resolver;
the summary never turns legacy task-root artifacts into v2 truth.

Template:

```markdown
# Run Summary

## Objective

## Current Status

## Task Board

## Active Blockers

## Ready For Codex Review

## Protocol Warnings

## Protocol Non-Fatal Warnings

## Recent Decisions

## Recent Events

## Experiment Results

## Next Actions
```
