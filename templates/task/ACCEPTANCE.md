# Acceptance

## Required Commands

Commands that must be run before handoff. Include exact commands, working directory assumptions, and any required env vars.

## Expected Outputs

Files, logs, metrics, or artifacts that must exist after the task completes.

## Smoke Tests

Fast checks that prove the main behavior works. Prefer deterministic, cheap commands.

## Metrics Or Thresholds

Research or experiment thresholds required for review, including baselines, seeds, datasets, and metric names.

## Merge Preconditions

Conditions Codex must verify before approving or merging, such as allowed paths, dry-run merge, integration smoke tests, or review signoff.

## Failure Handoff Conditions

Conditions under which the worker should stop and hand off as `blocked` instead of continuing.

## Post-Merge Smoke Test

Checks to run after merge if this task reaches `merged`.
