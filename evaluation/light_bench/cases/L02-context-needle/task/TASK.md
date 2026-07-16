# Task T902

## Objective

Resolve the observed divergence between omitted `ttl_seconds` behavior under a
non-default QueueConfig and MiniQueue's frozen lease-duration contract.

## Deliverables

- Make lease acquisition and renewal comply with the resolution semantics in
  the frozen design source.
- Keep explicit positive durations and invalid-input behavior unchanged.
- Pass the complete visible test suite.

## Invariants

- `docs/DESIGN.md` is normative and must not be modified.
- No public signature, snapshot schema, or dependency changes.
- Invalid duration input must not mutate queue state.

## Non-goals

- Do not redesign lease ownership, expiry, retry, or scheduling.
- Do not add tests or broaden the task beyond the localized implementation.

## Dependencies

```json rdo-task-dependencies
{
  "schema_version": 2,
  "dependencies": []
}
```
