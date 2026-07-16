# Context

## Frozen Decisions

- The relevant rule is already frozen in `docs/DESIGN.md`; implementation
  convenience does not override it.

## Required Interfaces

- Preserve `Queue.lease_next(..., ttl_seconds=None)` and
  `Queue.renew(..., ttl_seconds=None)`.

## Local Code Map

- `docs/INDEX.md` maps design topics to bounded sections of the larger source.
- `src/miniqueue/queue.py` owns duration resolution for acquisition and renewal.
- `tests/test_queue.py` contains visible representative behavior.

## Necessary Background

- Locate the relevant design heading through the index and read only that
  bounded section before changing the implementation.
