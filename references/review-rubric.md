# Review Rubric

Use this rubric before Codex changes a task from `review` to `approved`, `changes_requested`, or `failed`.

## Required Checks

1. Inspect `TASK.md`, `ACCEPTANCE.md`, `STATUS.json`, `EVIDENCE.md`, `HANDOFF.md`, and the current attempt.
2. Inspect the task diff against the target branch.
3. Verify the diff stays within `allowed_paths` and avoids `forbidden_paths`.
4. Verify evidence supports each acceptance item.
5. Verify required logs exist and are referenced.
6. Verify `STATUS.json.evidence` is consistent with `EVIDENCE.md` and logs.
7. Verify no unresolved blocker or lock ambiguity remains.
8. Verify mergeability against the target branch.
9. Run required integration smoke tests before `approved`.
10. If merged, record post-merge smoke results when required by `ACCEPTANCE.md`.

## Outcomes

- `approved`: all review gates pass and the task is ready to merge.
- `changes_requested`: implementation is salvageable under the same task contract.
- `failed`: Codex determines the task should stop under current requirements.
- New task: scope, acceptance, ownership, or design changed enough that a new task packet is cleaner.

Do not approve based only on plausible code. Approval requires evidence and mergeability.
