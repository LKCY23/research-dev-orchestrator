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
worker commits `attempts/<attempt-id>/COMPLETION.json` through `rdo strategy
submit|revise` or `rdo finalize`. The signal binds the task, attempt, phase,
requested state, and exact `HANDOFF.json` digest. The supervisor accepts it only
while the referenced attempt is current and active. A valid signal requests
process quiescence; an invalid or stale signal is ignored.

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

Claude Code or another backend may expose an inner tool timeout. That timeout is advisory only. Protocol safety comes from the attempt supervisor and `rdo exec`, which use independent OS process groups.

## Termination

Termination is deterministic:

```text
SIGINT -> grace period -> SIGTERM -> grace period -> SIGKILL
```

After escalation, the supervisor scans the process group again. A surviving descendant is a protocol failure and must be recorded in diagnostics.

Historical PID and PGID observations are audit data only. Termination targets
must be recomputed from the worker's current descendant tree immediately before
signalling. Long attempts can outlive the operating system's PID reuse window;
signalling every identifier ever observed can terminate unrelated processes.

Completion-triggered termination uses the same process-group escalation after a
short grace period. The supervisor records the worker's actual child exit code
but normalizes the supervised result to zero only after validating the
attempt-bound completion signal and confirming no descendants survive. Dispatch
then performs worktree fingerprinting and full handoff validation.

## Managed And Native Subagents

Managed subagents are separate RDO-launched worker processes with their own identity, budget, logs, and result. They are preferred for required or long-running workflows.

Backend-native subagents may not expose independently controllable local processes. They must be declared as native, short-lived, read-only, and optional unless the backend adapter can prove independent lifecycle control. Attempt-level termination remains the final safety boundary.

## No Daemon

The supervisor does not wait for future tasks. Plain dispatch runs it synchronously. Tmux dispatch runs an attempt-local wrapper inside the tmux session. When the worker exits, times out, or commits a valid completion signal, supervision quiesces the process group, records the outcome, and exits.

Finalization timeout is not additional implementation time. Once all required workflows have completed, the worker may only summarize limitations and call `rdo finalize`; it must not resume broad investigation or start another workflow.
