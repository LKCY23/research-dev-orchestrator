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

Claude Code or another backend may expose an inner tool timeout. That timeout is advisory only. Protocol safety comes from the attempt supervisor and `rdo exec`, which use independent OS process groups.

## Termination

Termination is deterministic:

```text
SIGINT -> grace period -> SIGTERM -> grace period -> SIGKILL
```

After escalation, the supervisor scans the process group again. A surviving descendant is a protocol failure and must be recorded in diagnostics.

## Managed And Native Subagents

Managed subagents are separate RDO-launched worker processes with their own identity, budget, logs, and result. They are preferred for required or long-running workflows.

Backend-native subagents may not expose independently controllable local processes. They must be declared as native, short-lived, read-only, and optional unless the backend adapter can prove independent lifecycle control. Attempt-level termination remains the final safety boundary.

## No Daemon

The supervisor does not wait for future tasks. Plain dispatch runs it synchronously. Tmux dispatch runs an attempt-local wrapper inside the tmux session. When the worker exits or is terminated, supervision validates the outcome and exits.
