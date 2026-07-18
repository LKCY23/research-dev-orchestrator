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

- A084: cancelled work is never leased or dispatched.
- A085: an omitted reason stores `cancelled`.
- A086: a supplied non-empty reason is trimmed.
- A087: repeated cancellation returns the unchanged cancelled Job.
- A088, A089, A090: cancelling leased, succeeded, or dead work raises
  `InvalidStateTransitionError` without mutation.
- A091: cancelled state survives a JsonStore round trip.
- A092, A093: statistics count cancelled separately and include it in total.
- A094, A095: cancellation does not increment attempts and records the Queue
  clock as `completed_at`.
- A096: a blank reason raises `InvalidJobError` and leaves the job queued.

## Merge Preconditions

- All required tests pass and source changes remain within model and queue.

## Blocked Conditions

- The feature requires a new durable schema field or changing active-lease
  semantics.

## Pre-Merge Checks

- Confirm tests and frozen design sources were not modified.

## Post-Merge Checks

- None.
