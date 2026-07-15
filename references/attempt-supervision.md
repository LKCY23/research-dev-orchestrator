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

The supervisor records the worker PID and process-group ID, monotonic start/deadline values, approved strategy digest, runtime backend, and outcome. It owns process cleanup even when the worker CLI's own timeout is ineffective.

For `tmux + human`, worker completion is not inferred from an idle TUI. The
worker publishes `attempts/<attempt-id>/runtime/HANDOFF_READY.json` through
`rdo strategy submit|revise` or `rdo finalize`. The marker binds the task,
attempt, task inputs, requested state, source commit, and exact attempt-local
`HANDOFF.json`/`EVIDENCE.json` digests. The supervisor accepts it only while the
referenced attempt is current and active. A valid marker requests process
quiescence; an invalid or stale marker is ignored. Recognized legacy-v0.5/v1
attempts continue to use their historical `COMPLETION.json` path explicitly.

Machine attempts have a separate startup deadline. Process creation and prompt
delivery are insufficient: the supervisor must decode a valid backend first
event and write `worker_started` to `runtime/STARTUP.json`. Early exit or startup
timeout terminates the process group, records `worker_startup_failed`, and is
classified as an environment blocker. This deadline is independent of the
larger attempt wall timeout.

## Enforcement Layers

- Attempt: total wall time, termination, exit result, and no surviving descendants.
- Workflow: approved instance count, concurrency, deadline, permission mode, and timeout policy.
- Command: bounded execution, wall timeout, exit code, and process-group cleanup.
- Finalization: after `runtime/FINALIZATION.json` appears, `tmux + human` receives a separate 90-second deadline to produce a valid atomic handoff.
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

After escalation, the supervisor scans the process group again. A surviving descendant is a protocol failure and must be recorded in diagnostics.

Cleanup covers the supervised process group, discoverable descendants, and
processes that retain the inherited RDO supervision token. This is
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

## Managed And Native Subagents

Managed subagents are separate RDO-launched worker processes with their own identity, budget, logs, and result. They are preferred for required or long-running workflows.

Backend-native subagents may not expose independently controllable local processes. They must be declared as native, short-lived, read-only, and optional unless the backend adapter can prove independent lifecycle control. Attempt-level termination remains the final safety boundary.

## No Daemon

The supervisor does not wait for future tasks. Plain dispatch runs it
synchronously. Tmux dispatch runs an attempt-local wrapper inside the tmux
session. When the worker exits, times out, or publishes a valid handoff marker,
supervision quiesces the process group, records the outcome, and exits.

Finalization timeout is not additional implementation time. Once all required workflows have completed, the worker may only summarize limitations and call `rdo finalize`; it must not resume broad investigation or start another workflow.
