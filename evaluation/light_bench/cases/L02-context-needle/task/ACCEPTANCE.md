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
  "required_outputs": ["src/miniqueue/queue.py"],
  "pre_merge_commands": [],
  "post_merge_commands": []
}
```

## Behavioral Checks

- Omitted duration follows the normative rule for acquisition and renewal,
  including non-default QueueConfig values.
- Explicit positive duration still takes precedence and booleans, zero, and
  negative values remain invalid.

## Merge Preconditions

- Required tests pass and only the scoped queue implementation changed.

## Blocked Conditions

- The design source and public API require contradictory behavior.

## Pre-Merge Checks

- Confirm the large design source and visible tests were not modified.

## Post-Merge Checks

- None.
