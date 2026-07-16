# Attempt Supervision

The supervisor remains backend-independent. Backend-specific plugin, native
agent, hook, and CLI-setting governance belongs to the compilation layer in
`references/backend-governance.md`, not to this process supervisor.

An attempt supervisor is a deterministic, attempt-local program. It starts with one execution attempt, synchronously manages that worker, and exits when the attempt ends. It is not an LLM, queue, server, or persistent daemon.

## Process Model

```text
dispatch_agent.sh
  -> machine_attempt_supervisor.py (plain + machine)
     or supervise_attempt.py (tmux + human runner)
       -> worker process group
            -> worker commands and managed subprocesses
```

The supervisor records the worker PID and process-group ID, one attempt-wide
absolute execution deadline, approved strategy digest, runtime backend, and
outcome. `runtime/DEADLINE.json` is created once and reused by a same-attempt
session fallback, so restarting a backend process never resets the attempt
budget. The mutable supervisor state includes remaining time and a structured
`attempt_deadline_approaching` reminder before cutoff. Worker-facing RDO
commands also emit the notice on stderr when they run inside the reminder
window. This is deterministic state/next-command delivery, not an asynchronous
message injected into a backend while it is inside an unrelated native tool
call.

Every supervisor result binds the exact `DEADLINE.json` bytes loaded before
worker spawn. Runtime session fallback is allowed only when the first
supervisor proves clean process shutdown and the current deadline digest still
matches that receipt. Rewriting a self-consistent deadline cannot reset the
same attempt.

For `tmux + human`, worker completion is not inferred from an idle TUI. The
worker publishes `attempts/<attempt-id>/runtime/HANDOFF_READY.json` through
`rdo strategy submit|revise` or `rdo finalize`. The marker binds the task,
attempt, task inputs, requested state, source commit, and exact attempt-local
`HANDOFF.json`/`EVIDENCE.json` digests. The supervisor accepts it only while the
referenced attempt is current and active. A marker that passes the bounded
candidate gate requests process quiescence. A stale or structurally invalid
marker is ignored; failure of full post-cleanup bundle validation records
publication invalidation instead of success. Recognized legacy-v0.5/v1 attempts
continue to use their historical `COMPLETION.json` path explicitly.

Machine attempts have a separate startup deadline. Process creation and prompt
delivery are insufficient: the supervisor must decode a valid backend first
event and write `worker_started` to `runtime/STARTUP.json`. Early exit or startup
timeout terminates the process group, records `worker_startup_failed`, and is
classified as an environment blocker. This deadline is independent of the
larger attempt wall timeout.

For Codex, `thread.started` proves native session allocation but does not prove
that a model request was accepted. The supervisor separately records
`worker_progress_evidence` on the first non-error model/tool item. A known
provider, model, authentication, resume, or invocation rejection before that
progress may therefore rewrite the startup record to `worker_startup_failed`
even when a thread ID was already issued. This prevents backend configuration
failures from being reported as task implementation failures.

## Enforcement Layers

- Attempt: total wall time, termination, exit result, and no surviving descendants.
- Workflow: approved instance count, concurrency, deadline, permission mode, and timeout policy.
- Command: bounded execution, wall timeout, exit code, and process-group cleanup.
- Finalization: Direct/Delegated explicitly enter once their source is final.
  Full enters after the final required workflow passes its workflow gate. Entry
  must occur no later than the execution deadline. It freezes production
  source and activates a final deadline equal to the original execution
  deadline plus 90 seconds by default, so early entry never shortens the
  attempt and entry near cutoff still receives the full grace.
- Resource usage: structured model turns are normalized to `runtime/USAGE.ndjson`; configured turn/token/cost/context and no-progress limits terminate the attempt with exit 125.

Resource limits are enabled only when explicitly present in the approved strategy. Backend definitions declare metric observability separately for `machine` and `human`; dispatch fails closed if a hard metric is unavailable. OpenCode's session event stream supports this in both modes. Other interactive TUI modes currently reject model-usage budgets because their adapters do not expose a reliable structured stream.

Claude Code or another backend may expose an inner tool timeout. That timeout
is advisory only. Protocol safety comes from the attempt supervisor and the
shared process-group supervision used by `rdo exec`, `rdo check`, and canonical
merge checks.

## Termination

Termination is deterministic:

```text
SIGINT -> grace period -> SIGTERM -> grace period -> SIGKILL
```

Before and after every signal stage, the supervisor rescans the current process
group and descendant tree. It also performs a supervision-token scan after
SIGINT so a handler cannot escape cleanup by spawning a detached child. A
surviving descendant is a protocol failure; valid handoff publication must not
be normalized to success while any survivor remains.

Cleanup covers the supervised process group, discoverable descendants, and
processes that retain the inherited RDO supervision-token lineage, including
commands launched by nested `rdo check` supervisors. This is
deterministic cleanup for cooperative tool processes, not hostile process
containment. A process that deliberately detaches and strips identifying
environment may evade userspace discovery; a hard guarantee requires an
OS-level sandbox, cgroup/job object, or equivalent containment boundary.

Historical PID and PGID observations are audit data only. Termination targets
must be recomputed from the worker's current descendant tree immediately before
signalling. Long attempts can outlive the operating system's PID reuse window;
signalling every identifier ever observed can terminate unrelated processes.

Publication-triggered termination uses the same process-group escalation after
a short grace period. The supervisor records the worker's actual child exit
code but normalizes the supervised result to zero only after revalidating the
attempt-bound marker and confirming no descendants survive. Dispatch then
compares the immutable worktree snapshot semantically and independently
validates the handoff bundle, acceptance evidence, source commit, and profile
transition.

Publication detection is deliberately two-stage. The hot deadline path reads
only bounded marker/status/Git-control data and latches a stable supervisor
receipt. Once the process tree is stopped, the supervisor validates the full
artifact bundle, final source state, and dependency timestamps. A missing,
corrupt, or internally inconsistent supervisor result triggers attempt-local
cleanup for every runtime backend; cleanup uncertainty retains the dispatch
lock for coordinator recovery.

## Managed And Native Subagents

Managed subagents are separate RDO-launched worker processes with their own identity, budget, logs, and result. They are preferred for required or long-running workflows.

Backend-native subagents may not expose independently controllable local processes. They must be declared as native, short-lived, read-only, and optional unless the backend adapter can prove independent lifecycle control. Attempt-level termination remains the final safety boundary.

## No Daemon

The supervisor does not wait for future tasks. Plain dispatch runs it
synchronously. Tmux dispatch runs an attempt-local wrapper inside the tmux
session. When the worker exits, times out, or publishes a valid handoff marker,
supervision quiesces the process group, records the outcome, and exits.

Finalization grace is independent deadline time, but it is not additional
implementation time. At entry RDO publishes immutable
`runtime/finalization-worktree.json` and `runtime/FINALIZATION.json`. The worker
may add or repeat exact required-check records, commit the already-frozen tree,
and call `rdo finalize`. A required-check record qualifies only when its
before/after semantic source digest equals the frozen snapshot, so a stale
baseline pass cannot validate later code. Workflow activity and `rdo exec` are
rejected; any persistent content, path, symlink target/kind, or mode drift
makes finalization fail. If complete process cleanup cannot be verified,
dispatch fails closed and retains its execution lock.
