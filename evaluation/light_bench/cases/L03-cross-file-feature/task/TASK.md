# Task T903

## Objective

Add durable, idempotent cancellation for queued MiniQueue jobs according to the
frozen cancellation contract.

## Deliverables

- Add terminal `cancelled` lifecycle representation with valid JSON round trips.
- Add `Queue.cancel(job_id, reason=None) -> Job` for queued jobs.
- Count cancelled work separately in QueueStats and include it in total.
- Ensure cancelled work can never be leased or dispatched.
- Pass the complete visible test suite.

## Invariants

- Cancellation does not consume an attempt and records Queue clock time as
  completion time.
- Existing queued, leased, succeeded, dead, retry, and persistence behavior is
  unchanged.
- No new durable Job field, schema version, dependency, or public signature
  outside the requested method.

## Non-goals

- Do not support cancelling an active lease or terminal succeeded/dead work.
- Do not add bulk cancellation, deletion, pause, or scheduler control APIs.
- Do not modify tests or documentation.

## Dependencies

```json rdo-task-dependencies
{
  "schema_version": 2,
  "dependencies": []
}
```
