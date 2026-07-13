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

Use `rdo worker message` instead. Its result distinguishes `submitted` or `queued` from `acted_on`; pane echo alone cannot prove that a worker executed an instruction.

## Interrupt And Terminate

- `rdo worker interrupt` sends `Ctrl-C` to the pane. It may stop only the foreground tool.
- `rdo worker terminate` targets the recorded worker process group and verifies descendant cleanup.

Never claim an attempt stopped after `Ctrl-C` without checking its process group. Never remove `.dispatch-lock` merely because a tmux pane disappeared.

## Audit

Coordinator control commands append events with run, task, attempt, mode, and timestamp. They do not directly edit worker-owned evidence or bypass handoff validation.
