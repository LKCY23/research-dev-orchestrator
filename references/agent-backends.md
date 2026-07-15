# Agent Backends

Agent backends are concrete CLI adapters. They are separate from protocol roles and runtime supervision.

The registry defines launch commands, permission-mode mappings, verified
capabilities, and adapter-owned governance fields. Claude Code, Codex, OpenCode,
and Kimi Code each compile their own verified governance surfaces. See
`references/backend-governance.md`.

```text
coordinator backend = who makes intent/review decisions
worker backend      = which CLI executes one task attempt
runtime backend     = how dispatch supervises the process
io mode             = machine or human interaction shape
```

Attempts are supervision slices, not conversation boundaries. When dispatch records `execution_mode=resume`, the adapter reuses the assigned worker's native session ID: Claude Code `--resume`, Codex `exec resume`, OpenCode `run --session`, and Kimi `--session`. A first attempt uses `start`; an intentional backend/worker replacement uses `replace` and starts a new session while retaining attempt lineage.

## Supported Backends

```text
claude-code
codex
opencode
kimi-code
```

Backend definitions live in `agent_backends/*.toml`. Validate them with:

```bash
python scripts/agent_backend_cli.py validate --backend all
```

## Runtime Combinations

The current runtime matrix supports only:

```text
plain + machine
tmux + human
```

`plain + machine` is for non-interactive dispatch and transcript capture.

`tmux + human` is for attachable observation. It is still supervised by dispatch; tmux is not a protocol source of truth.

The command builder returns structured `argv` and environment values in
addition to a display command. Machine supervision launches the structured
form without shell parsing. Each adapter declares exactly one initial-prompt
transport, preventing the same prompt from being supplied through both argv and
stdin.

Arbitrary worker command overrides are not a production adapter surface. They
are available only to isolated test fixtures because RDO cannot infer their
startup event, permission, or prompt-delivery contract.

Codex native-subagent strategies support both runtime/IO pairs. Native thread
and depth limits apply in either mode. If project policy explicitly enables the
optional cumulative spawn limit, the adapter requires `plain + machine` so its
JSONL supervisor can enforce that additional control.

For Codex, RDO `auto` means the Codex **Approve for me** profile:
`approval_policy=on-request`, `sandbox=workspace-write`, and the guardian
approval reviewer. It is distinct from `yolo`, which bypasses both approvals and
the sandbox. `approval_policy=never` is not used for `auto`: it merely prevents
approval requests and returns denied escalations to the model.

Kimi supports both runtime/IO pairs through an attempt-local configuration
overlay. Its native swarm limit and background-task limit are combined with
lifecycle hooks. OpenCode supports both pairs through a per-attempt local server:
machine mode streams server events, while human mode attaches the TUI to the
same supervised session. Human attempts record a password-redacted
`runtime/ATTACH.json`; the initial prompt is still sent exactly once through the
server API before attachment.

## Prompt Transport

Machine mode currently uses direct argument prompt transport for all shipped
adapters. The supervisor also supports adapter-declared stdin transport without
changing the exactly-once rule.

Human mode supports:

```text
arg
  The CLI accepts an initial prompt when launching its TUI.

tmux_send_keys
  Dispatch starts the TUI, sends the prompt to the tmux pane, waits briefly,
  then sends the submit key.
```

Current mapping:

```text
claude-code human: arg
codex human: arg
opencode human: arg
kimi-code human: tmux_send_keys
```

Kimi Code currently does not support `kimi --auto "<prompt>"`; top-level positional arguments are parsed as subcommands. Use `kimi -p "<prompt>" --output-format stream-json` for machine mode, and `kimi --auto` plus tmux key injection for human mode.

## Permission Modes

The protocol names three permission modes:

```text
default
auto
yolo
```

If a backend does not support a requested permission mode, dispatch must fail before acquiring locks, creating attempts, or mutating `STATUS.json`.

OpenCode currently supports `default` and `auto`; `yolo` is intentionally unsupported in its backend definition.

Backend governance relies on documented upstream surfaces: [Kimi configuration](https://moonshotai.github.io/kimi-code/en/configuration/config-files.html),
[Kimi hooks](https://moonshotai.github.io/kimi-code/en/customization/hooks.html),
[OpenCode agents](https://opencode.ai/docs/agents/), and
[OpenCode server API](https://opencode.ai/docs/server/).
