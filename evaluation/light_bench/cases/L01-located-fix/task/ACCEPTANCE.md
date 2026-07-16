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
  "required_outputs": ["src/miniqueue/retry.py"],
  "pre_merge_commands": [],
  "post_merge_commands": []
}
```

## Behavioral Checks

- Attempts one through four with base 3, multiplier 2, and cap 10 yield
  `3, 6, 10, 10`.
- Deterministic spread still produces repeatable values.

## Merge Preconditions

- The task branch contains only the scoped retry implementation change and all
  required commands pass.

## Blocked Conditions

- The focused retry behavior cannot be fixed without changing the public API.

## Pre-Merge Checks

- Confirm no tests or documentation were modified.

## Post-Merge Checks

- None.
