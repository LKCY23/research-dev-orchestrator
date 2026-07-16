# MiniQueue Design

Status: frozen fixture contract. This document defines behavior that is not
always repeated in tests or implementation comments. Changes made by benchmark
workers must preserve it unless a task explicitly replaces a named rule.

## 1. Goals and Non-goals

MiniQueue is a small deterministic queue for local services and test programs.
It demonstrates durable state, lease ownership, retry scheduling, and handler
dispatch without a database or third-party dependency. Its principal design
goal is understandable behavior: a reader should be able to determine exactly
which job is eligible and what every operation changes.

The implementation is single-process. Atomic file replacement protects durable
JSON from partial writes, and revisions protect multiple Queue objects sharing
one Store from stale writes. Cross-host locking, distributed consensus, clock
synchronization, fairness across tenants, unbounded throughput, and exactly-once
execution are non-goals. A worker may run more than once after lease expiry, so
handlers remain responsible for application-level idempotence.

Public functions validate caller input before changing durable state. Durable
handler failures are recorded on jobs instead of escaping from the Scheduler.
This distinction makes operational failures inspectable while keeping mistakes
in API usage immediately visible to callers.

## 2. Vocabulary

A **job** is one durable payload and its lifecycle metadata. A **worker** is a
caller identity used to acquire and prove ownership of a lease. A **lease** is a
time-bounded exclusive claim; it is not a process lock. An **attempt** increments
when a job is leased, including an attempt whose worker later disappears. A
**retry** is a queued job whose previous attempt failed and whose availability
has moved into the future. A **snapshot** contains all jobs plus one optimistic
revision number.

The phrase **current time** always means the Queue's injected monotonic Clock.
Serialized timestamps use that clock's numeric domain and are comparable only
inside one fixture instance. Tests use ManualClock so no behavior depends on
sleep duration or wall-clock scheduling.

## 3. State Model

The base state set is `queued`, `leased`, `succeeded`, and `dead`. A queued job
may be unavailable until `available_at`; this is still one state rather than a
separate delayed state. Leasing changes queued to leased and increments attempts.
Acknowledgement changes leased to succeeded. Failure either returns leased to
queued with a future availability time or changes it to dead when the attempt
limit is reached. Lease expiry returns leased to queued without decrementing the
attempt count.

Succeeded and dead are terminal. They carry `completed_at` and no lease fields.
Queued jobs carry neither completion nor lease fields. Leased jobs require both
an owner and a deadline. These invariants are checked when records are created
from JSON so corrupted snapshots fail closed.

Tasks may introduce a new terminal state only when they update all coupled
surfaces: enum membership, terminal detection, serialized round trips, state
field validation, and statistics. A terminal state must never become leaseable.

## 4. Persistence and Revisions

Stores return isolated QueueSnapshot values. The caller modifies its private
snapshot, advances the revision by exactly one, and saves with the revision it
originally loaded. A mismatched revision is a ConflictError. Queue may retry a
bounded number of conflicts by reloading and replaying the operation.

The JSON representation is deliberately explicit. Every Job field is present,
including nullable fields, and unknown fields are rejected. Adding a durable
field therefore requires a schema decision rather than silently accepting data
that an older implementation would drop. Job ordering in JSON is by job id only
to keep files stable; runtime leasing order is independent of serialization.

JsonStore writes a sibling temporary file, flushes and fsyncs it, then performs
an atomic replace. It does not promise directory fsync or inter-process locking.
MemoryStore and JsonStore must expose the same observable revision semantics.

## 5. Eligibility and Ordering

A job is eligible when its state is queued, its availability is not later than
current time, and it contains every tag required by the leasing caller. Extra
job tags do not make a job ineligible. Required tags are a set for matching even
though the durable job representation preserves an ordered unique tuple.

Eligible jobs sort by descending priority, ascending availability, ascending
creation time, then ascending job id. This total order is stable across process
restarts and Python dictionary order. Priority is constrained to -100 through
100 so callers cannot use arbitrary magnitudes as hidden scheduling channels.

Expired leases are released immediately before eligibility is calculated. A
reclaimed job participates in the same ordering as every other queued job and
keeps its original creation time. The release time becomes its new availability
time, preventing an old deadline from distorting ordering.

## 6.1 Lease Acquisition

`lease_next` requires a non-empty worker id. If no eligible job exists it returns
None without advancing the store revision. On success it changes exactly one
job to leased, increments attempts, stores the worker id, and computes a lease
deadline from current time plus the resolved duration.

The returned Job is a clone. Mutating it does not change the store. Callers must
use renew, acknowledge, or fail for durable changes. This rule applies to jobs
returned by enqueue, get, list, and every lifecycle operation.

## 6.2 Lease Ownership

Only the active owner may renew, acknowledge, or fail a lease. Supplying another
worker id is a LeaseError and leaves the snapshot unchanged. Ownership is exact
string equality; names are not trimmed or case-folded after initial non-empty
validation. A terminal or queued job has no owner.

A lease whose deadline equals current time is expired. The former owner cannot
acknowledge it even if release_expired has not yet been called, because ownership
checks compare the deadline directly. A later acquisition may assign the job to
the same worker id, but that is a new attempt and a new lease.

## 6.3 Renewal and Expiry

Renewal replaces the deadline with current time plus a newly resolved duration;
it does not add duration to the old deadline. This prevents early renewal from
silently creating an ever-growing lease. Renewal does not increment attempts.

Explicit expiry release changes leased jobs to queued, clears owner and deadline,
and sets availability to current time. The operation returns the number released.
When there is nothing to release it does not write a new revision.

## 6.4 Lease Duration Resolution

The `ttl_seconds` argument has two intentionally distinct forms. When it is
`None`, Queue must use that Queue instance's configured
`QueueConfig.default_lease_seconds`. It must not use a module constant, the
dataclass type default, or a hard-coded value, because callers may construct
queues with different policy. The same resolution rule applies independently
to acquisition and renewal.

When `ttl_seconds` is explicitly supplied, its positive numeric value is used
exactly after conversion to float. Booleans are not numeric for this contract,
despite Python's bool subclassing int. Zero and negative values are invalid.
There is no sentinel for an infinite lease. Configuration validation guarantees
that the fallback duration is itself positive.

Resolution happens before loading or changing a snapshot. Invalid duration
input therefore cannot release expired jobs, advance a revision, or partially
lease work. A non-default queue configuration is the canonical test that
distinguishes instance policy from an accidentally hard-coded default.

## 7. Retry Timing

Attempts are one-based in retry calculations. After failed attempt one, the
delay is `base_delay`; attempt two multiplies once; attempt three multiplies
twice. The uncapped expression is therefore `base_delay * multiplier **
(attempt - 1)`. The result is capped at max_delay.

Optional spread is deterministic for a job id and attempt. It exists to make
examples of desynchronization possible without random tests. Spread never
raises a value above max_delay, and a zero base delay remains zero. The retry
policy validates attempts before calculating exponentiation.

A failed job at its attempt limit becomes dead immediately and receives
completed_at. Otherwise it becomes queued, clears lease fields, records the
stripped error string, and moves availability to current time plus the retry
delay. A later success clears last_error.

## 8. Scheduler Dispatch

Scheduler polls at most one job in run_once. A payload chooses a handler through
its non-empty string `kind`. Missing kinds and unknown handlers are durable job
failures, not caller errors, because they describe enqueued data. Exceptions
raised by handlers are converted to a stable `TypeName: message` error string.

A successful handler result is returned to the caller but is not serialized in
the Job. Scheduler acknowledges only after the handler returns. Batch execution
stops when a poll is idle or when max_jobs successful or failed executions have
been attempted. It does not advance ManualClock to wait for delayed retries.

## 9. Cancellation Extension Point

The base implementation intentionally has no cancellation API. A task that adds
cancellation must represent it as a new terminal `cancelled` state. Cancellation
is permitted only while a job is queued. It returns a changed Job on the first
successful cancellation and returns that same terminal state idempotently when
called again. Trying to cancel leased, succeeded, or dead work is rejected.

A cancelled job clears no active lease because it could not have one, receives
completed_at from the Queue clock, and stores an optional human-readable reason
in last_error. An omitted reason stores the stable text `cancelled`. Statistics
must count cancelled separately and include it in total. Serialization requires
no new Job field because the reason reuses last_error.

The public method is `Queue.cancel(job_id, reason=None) -> Job`. A supplied reason
must be a non-empty string after trimming. Cancellation does not consume an
attempt. Scheduler needs no special branch when leasing already filters on the
queued state, but its tests should demonstrate cancelled work is never handled.

## 10. Observability

QueueStats is a point-in-time immutable count derived from a fresh snapshot. It
has one named integer field per state and a total property that sums every named
field. State additions must update both construction and total; iterating the
enum for counts is preferred because it makes missing mapping cases visible.

The queue does not log payloads, worker ids, or exceptions. Benchmark telemetry
belongs outside this fixture. Tests and callers can inspect jobs explicitly when
they need lifecycle details.

## 11. Error Handling

InvalidJobError identifies invalid API values and corrupt serialized records.
JobNotFoundError identifies unknown ids. LeaseError identifies wrong ownership,
wrong source state, or an expired claim. ConflictError identifies a stale store
revision. These errors do not mutate state.

Scheduler catches Exception from handlers because handler failures are the work
being modeled. It does not catch BaseException. Errors from queue operations are
not converted into RunResult because they indicate infrastructure or caller
faults rather than a handler outcome.

## 12. Compatibility Rules

The package targets Python 3.10 or later and uses no runtime dependency outside
the standard library. Public names exported by miniqueue.__init__ are stable for
the fixture. Private helpers beginning with an underscore may change freely.

Snapshot schema version one rejects unknown fields. A benchmark task must not
bump the schema merely to add an enum value whose string fits the existing state
field. New required Job fields would need an explicit compatibility plan and are
outside the light-bench cases.

## Appendix A. Deterministic Scenario Catalog

The catalog makes the design document representative of a real project brief:
it records combinations reviewers may consider without turning every row into a
visible test. Rows restate identifiers and expected categories, not source code.

| ID | Initial condition | Operation | Expected category |
| --- | --- | --- | --- |
| A001 | empty queue at time 0 | lease by valid worker | idle, no revision write |
| A002 | one queued job at time 0 | lease by valid worker | leased attempt one |
| A003 | one delayed job at time 0 | lease before availability | idle |
| A004 | one delayed job at boundary | lease at availability | leased |
| A005 | low and high priority jobs | lease one | high priority selected |
| A006 | equal priority jobs | lease one | earliest availability selected |
| A007 | equal availability jobs | lease one | earliest creation selected |
| A008 | otherwise equal jobs | lease one | lexical job id selected |
| A009 | tagged job | lease with subset tags | eligible |
| A010 | tagged job | lease with absent tag | idle |
| A011 | queued job | lease with empty worker | caller error |
| A012 | queued job | lease with boolean duration | caller error |
| A013 | queued job | lease with zero duration | caller error |
| A014 | queued job | lease with negative duration | caller error |
| A015 | configured duration 17 | lease with omitted duration | deadline now plus 17 |
| A016 | configured duration 17 | lease with explicit 9 | deadline now plus 9 |
| A017 | active lease | renew by owner | deadline replaced |
| A018 | active lease | renew by other worker | lease error |
| A019 | deadline equals now | renew by former owner | lease error |
| A020 | active lease | acknowledge by owner | succeeded terminal |
| A021 | active lease | acknowledge by other | lease error |
| A022 | queued job | acknowledge | lease error |
| A023 | active first attempt | fail | queued with base delay |
| A024 | active second attempt | fail | queued with multiplied delay |
| A025 | active final attempt | fail | dead terminal |
| A026 | retry delay exceeds cap | fail | availability uses capped delay |
| A027 | handler succeeds | scheduler run | acknowledgement after result |
| A028 | handler raises ValueError | scheduler run | durable typed error |
| A029 | payload lacks kind | scheduler run | durable validation failure |
| A030 | payload has unknown kind | scheduler run | durable missing-handler failure |
| A031 | no eligible work | batch run | empty result list |
| A032 | two eligible jobs | batch limit one | exactly one result |
| A033 | expired lease | explicit release | queued and count one |
| A034 | no expired leases | explicit release | count zero, no revision write |
| A035 | expired high priority | next lease | reclaimed job participates normally |
| A036 | returned enqueue object | caller mutates payload | durable payload unchanged |
| A037 | returned leased object | caller clears owner | durable owner unchanged |
| A038 | duplicate job id | enqueue | caller error, original preserved |
| A039 | non-object payload | enqueue | caller error |
| A040 | unserializable payload | enqueue | caller error |
| A041 | oversized payload | enqueue | caller error |
| A042 | negative delay | enqueue | caller error |
| A043 | max attempts zero | enqueue | caller error |
| A044 | priority below minimum | enqueue | caller error |
| A045 | priority above maximum | enqueue | caller error |
| A046 | duplicate tags | enqueue | caller error |
| A047 | JSON store missing file | load | empty revision-zero snapshot |
| A048 | JSON store valid file | load | isolated reconstructed jobs |
| A049 | JSON store malformed JSON | load | caller-visible data error |
| A050 | snapshot wrong schema | load | caller-visible data error |
| A051 | snapshot unknown job field | load | caller-visible data error |
| A052 | stale expected revision | save | conflict, file preserved |
| A053 | revision skips a value | save | conflict |
| A054 | normal save | inspect file | stable sorted pretty JSON |
| A055 | job state leased without owner | deserialize | data error |
| A056 | queued job with lease deadline | deserialize | data error |
| A057 | terminal job without completion | deserialize | data error |
| A058 | non-terminal with completion | deserialize | data error |
| A059 | retry attempt zero | calculate delay | caller error |
| A060 | multiplier below one | create retry policy | caller error |
| A061 | max delay below base | create retry policy | caller error |
| A062 | spread outside range | create retry policy | caller error |
| A063 | zero spread | calculate repeatedly | exact exponential values |
| A064 | nonzero spread same key | calculate repeatedly | identical values |
| A065 | nonzero spread different keys | calculate | bounded deterministic values |
| A066 | succeeded job | list by succeeded | included |
| A067 | succeeded job | list by queued | excluded |
| A068 | mixed jobs | stats | one count per base state |
| A069 | mixed jobs | stats total | sum of all state counts |
| A070 | unknown id | get | not-found error |
| A071 | unknown id | fail | not-found error |
| A072 | unknown id | acknowledge | not-found error |
| A073 | ManualClock advance positive | now | increased value |
| A074 | ManualClock advance negative | now | caller error, unchanged |
| A075 | ManualClock set backwards | now | caller error, unchanged |
| A076 | explicit duration 2.5 | lease | floating deadline exact |
| A077 | explicit integer duration | renew | numeric conversion accepted |
| A078 | config duration boolean | construct queue config | caller error |
| A079 | mutation retries zero | construct queue config | caller error |
| A080 | empty handlers | construct scheduler | caller error |
| A081 | non-callable handler | construct scheduler | caller error |
| A082 | empty scheduler worker | construct scheduler | caller error |
| A083 | max_jobs zero | batch run | caller error |
| A084 | cancelled queued job | lease | never eligible |
| A085 | queued job | cancel without reason | cancelled with stable reason |
| A086 | queued job | cancel with spaced reason | cancelled with trimmed reason |
| A087 | cancelled job | cancel again | idempotent terminal result |
| A088 | leased job | cancel | rejected without mutation |
| A089 | succeeded job | cancel | rejected without mutation |
| A090 | dead job | cancel | rejected without mutation |
| A091 | cancelled job | serialize and reload | cancelled state preserved |
| A092 | cancelled and queued jobs | stats | cancelled counted separately |
| A093 | cancelled job | stats total | included in total |
| A094 | cancel without attempt | inspect job | attempts unchanged |
| A095 | cancel at time 42 | inspect job | completed_at equals 42 |
| A096 | cancel with blank reason | invoke | caller error, remains queued |
| A097 | cancellation-capable snapshot | old base enum | unsupported until feature added |
| A098 | multiple Queue instances | sequential mutations | fresh revisions preserved |
| A099 | synthetic stale Queue operation | conflict retry | operation replayed boundedly |
| A100 | exhausted conflict retries | mutation | last ConflictError escapes |

## Appendix B. Review Checklist

Review lifecycle changes against five surfaces: state membership, transition
validation, durable serialization, aggregate statistics, and selection filters.
Review timing changes against clock source, boundary comparison, mutation order,
configuration precedence, and invalid-input side effects. Review persistence
changes against isolated snapshots, exact revision advance, atomic replacement,
unknown-field behavior, and error type stability.

Visible tests intentionally cover only representative rows. Acceptance may use
an external deterministic verifier for rows directly named by a benchmark task.
The verifier must interact only through public APIs and disposable files; it
must not inspect a model-generated explanation or use an LLM judge.
