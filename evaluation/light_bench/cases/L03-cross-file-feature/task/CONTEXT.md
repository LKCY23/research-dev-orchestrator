# Context

## Frozen Decisions

- Cancellation is a terminal state, not deletion and not a second delayed state.
- A repeated cancellation of an already-cancelled job is idempotent.

## Required Interfaces

- Add `Queue.cancel(job_id, reason=None) -> Job`.
- Add `JobState.CANCELLED` and `QueueStats.cancelled`.

## Local Code Map

- `docs/INDEX.md` points to the bounded normative cancellation section.
- `src/miniqueue/model.py` owns states, terminal validation, serialization, and
  QueueStats.
- `src/miniqueue/queue.py` owns mutations and clock access.
- `tests/test_cancellation.py` contains representative visible behavior.

## Necessary Background

- Existing selection already leases only queued jobs; preserve that invariant
  rather than introducing scheduler-specific cancellation state.
