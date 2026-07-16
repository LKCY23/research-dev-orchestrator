# MiniQueue Design Index

Use this index to locate only the part of `DESIGN.md` needed for a concrete
question. The design document is intentionally larger than the normal native
read threshold used by the light bench.

| Section | Topic | Read when deciding |
| --- | --- | --- |
| 1 | Goals and non-goals | whether a proposed feature belongs in MiniQueue |
| 2 | Vocabulary | meanings of job, lease, attempt, and worker |
| 3 | State model | legal lifecycle transitions and terminal states |
| 4 | Persistence | snapshot format and optimistic revision behavior |
| 5 | Ordering | how priority, availability, age, and id break ties |
| 6.1–6.3 | Lease acquisition | ownership, expiry, and renewal validation |
| 6.4 | Lease duration resolution | omitted versus explicit lease durations |
| 7 | Retry timing | one-based attempts, exponential delay, and capping |
| 8 | Scheduler | handler dispatch and failure conversion |
| 9 | Cancellation | reserved future behavior for cross-file changes |
| 10 | Observability | stable state counts and diagnostics |
| 11 | Error handling | caller errors versus durable job failures |
| 12 | Compatibility | serialization and API evolution rules |
| Appendix A | Scenario catalog | examples for design review and testing |

Headings are deterministic Markdown headings. A context tool can request one
section without loading the complete source.
