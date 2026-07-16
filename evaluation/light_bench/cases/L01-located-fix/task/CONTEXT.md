# Context

## Frozen Decisions

- Retry attempt numbers are one-based and the first failure uses `base_delay`.

## Required Interfaces

- Keep `RetryPolicy.delay_for_attempt(attempt, *, key="") -> float` unchanged.

## Local Code Map

- `src/miniqueue/retry.py` contains the complete faulty calculation.
- `tests/test_retry.py` contains the focused visible regression tests.

## Necessary Background

- The failure is localized; no broad repository exploration is necessary.
