# Acceptance

```json rdo-acceptance-contract
{
  "schema_version": 2,
  "required_commands": [
    {
      "id": "visible_tests",
      "argv": ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
      "cwd": ".",
      "timeout_seconds": 30
    }
  ],
  "required_outputs": [
    "src/miniqueue/model.py",
    "src/miniqueue/queue.py"
  ],
  "pre_merge_commands": [],
  "post_merge_commands": []
}
```

## Behavioral Checks

- Cancelling queued work produces a terminal cancelled Job, completion time,
  stable default reason, and no attempt increment.
- A non-empty supplied reason is trimmed; blank reasons are rejected.
- Repeating cancellation is idempotent, while leased, succeeded, and dead jobs
  reject cancellation without mutation.
- Cancelled state survives JsonStore round trips, is counted separately, is
  included in total, and is never leased or dispatched.

## Merge Preconditions

- All required tests pass and source changes remain within model and queue.

## Blocked Conditions

- The feature requires a new durable schema field or changing active-lease
  semantics.

## Pre-Merge Checks

- Confirm tests and frozen design sources were not modified.

## Post-Merge Checks

- None.
