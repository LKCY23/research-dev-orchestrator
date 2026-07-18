# Tmux Control

Tmux provides attachable observation for one attempt. It is not a protocol source of truth and raw `tmux send-keys` is not the preferred coordinator interface.

Tmux is supported only with `io_mode=human`. Machine workers use the plain
runtime so their startup events and prompt transport can be supervised
deterministically. Human prompt submission is best effort: `prompt_submitted`
means RDO invoked the configured argv or tmux key path, not that the agent read
or acted on the message.

While dispatch is supervising the pane, a small deterministic probe recognizes
common workspace-trust, login, and explicit confirmation prompts. It records
`worker_waiting_for_user` and prints the attach command. It never answers the
prompt automatically and cannot recognize every backend-specific TUI screen.

## Completion

An interactive CLI may return to its input prompt after a worker has submitted
strategy or handoff artifacts. RDO does not require the model to type `/exit`.
For Artifact Protocol v2, the finalizer publishes attempt-local
`runtime/HANDOFF_READY.json` after the immutable handoff/evidence package is
durable. The attempt supervisor validates every bound digest, allows a short
grace period, and then quiesces the recorded worker process group. A partial,
stale, or invalid marker does not stop the worker. This path does not approve
the strategy or task; dispatch validation and coordinator review remain
separate actions. Recognized legacy-v0.5/v1 attempts use their historical
`COMPLETION.json` decoder only on the compatibility path.

With `RDO_TMUX_KEEP_SESSION=1`, the runner leaves a login shell in the pane after
the worker process has been quiesced, so an attached observer can still inspect
the attempt. Otherwise dispatch cleans up the tmux session after validation.
Dispatch creates the tmux session in a parked state, durably records its tmux
ID, name, and creation time in attempt-local `runtime/TMUX_SESSION.json`, and
only then starts the worker in that session. A new-protocol attempt fails
startup and closes the parked session when this receipt cannot be written.
Later lifecycle cleanup and worker control bind to the receipt instead of
trusting a reusable session name.

## Lifecycle Inventory And Prune

```bash
python scripts/rdo.py tmux list --repo-root <repo> [--run <run-id>] [--active]
python scripts/rdo.py tmux prune --repo-root <repo> [--run <run-id>] --terminal
```

The list command is read-only. Terminal prune closes only a retained tmux shell
whose attempt completed successfully, published a valid handoff, preserved its
transcript, and has verified descendant cleanup. It revalidates the exact tmux
ID and creation time before killing by ID. Sessions associated with active or
blocked work, invalid handoffs, retained dispatch locks, missing cleanup or
transcript evidence, ambiguous artifact mappings, or missing/mismatched
identity receipts are left untouched. Historical sessions created before the
identity receipt was introduced remain visible but require manual handling.

Prune changes only tmux runtime state. It never edits task/attempt protocol
state, deletes transcripts, or treats a missing pane as completion evidence.

## Attach And Detach

```bash
tmux attach -t <session>
```

Detach with `Ctrl-b`, then `d`. Read the session name and attach command from the active task's `.dispatch-lock` metadata.

## Message Submission

Typing text does not submit it. Literal text and Enter are separate operations:

```bash
tmux send-keys -t "$session" -l "$message"
tmux send-keys -t "$session" Enter
```

Use `rdo worker message` instead. Immediately before submission it requires the
current attempt, dispatch lock, receipt ID/name/creation time, and live tmux
identity to agree, then targets the stable tmux ID. Its result distinguishes
`submitted` or `queued` from `acted_on`; pane echo alone cannot prove that a
worker executed an instruction.

## Interrupt And Terminate

- `rdo worker interrupt` performs the same identity revalidation as `message`
  and sends `Ctrl-C` to the receipt-bound pane. It may stop only the foreground
  tool.
- `rdo worker terminate` acts only while the attempt supervisor reports
  `running`. It revalidates the live worker PID, dedicated PGID, and inherited
  supervision token before signalling, then verifies descendant cleanup.
  Historical observed PID/PGID lists are never termination authority. Missing
  identity, unavailable process inspection, or surviving processes return
  non-zero and append `worker_termination_failed`.

Never claim an attempt stopped after `Ctrl-C` without checking its process group. Never remove `.dispatch-lock` merely because a tmux pane disappeared.

## Audit

Coordinator control commands append events with run, task, attempt, mode, and timestamp. They do not directly edit worker-owned evidence or bypass handoff validation.
