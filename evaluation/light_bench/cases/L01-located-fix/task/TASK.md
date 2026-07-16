# Task T901

## Objective

Restore the documented one-based exponential retry delay calculation in
`src/miniqueue/retry.py`.

## Deliverables

- Correct `RetryPolicy.delay_for_attempt` so the first failed attempt waits the
  configured base delay and later attempts grow from that value.
- Preserve deterministic spread, maximum-delay capping, and input validation.

## Invariants

- The package remains standard-library only and compatible with Python 3.10.
- No public API or serialized representation changes.
- All visible tests pass.

## Non-goals

- Do not redesign retry policy or modify queue and scheduler behavior.
- Do not change tests or documentation.

## Dependencies

```json rdo-task-dependencies
{
  "schema_version": 2,
  "dependencies": []
}
```
