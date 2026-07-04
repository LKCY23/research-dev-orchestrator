# Command Surface

`/rdo` commands are structured natural-language intents for Codex. They are not executable shell slash commands and do not bypass the skill protocol.

Codex must still obey:

```text
state-machine.json
STATUS.json schema
ATTEMPT lifecycle
runtime backend rules
Lock Recovery Review
review and merge gates
```

If required arguments are missing, infer only from clear current run context. Otherwise ask one concise clarification.

## Commands

### /rdo init

```text
/rdo init project=<slug> objective="<text>" [target=<branch>]
```

Purpose: create a new run scaffold.

Action: run `scripts/init_run.py`.

Outputs: `RUN.json`, required run artifacts, `EVENTS.ndjson`, `JOURNAL.md`, `SUMMARY.md`.

### /rdo plan

```text
/rdo plan run=<run-id> [scope=requirements|design|experiment|all]
```

Purpose: enter planning flow and update planning artifacts.

Action: discuss with the user, then update the relevant files:

```text
REQUIREMENTS.md
DESIGN_METHOD_SELECTION.md
DESIGN_BRIEF.md
ADR/*
EXPERIMENT_PLAN.md
REPRODUCIBILITY.md
```

Do not invent durable design decisions without user confirmation when the choice is material.

### /rdo create-task

```text
/rdo create-task run=<run-id> task=<task-id> goal="<text>" allowed=<path,path> [forbidden=<path,path>]
```

Purpose: create a task packet from the current plan/design.

Action: run `scripts/create_task.py`, then fill task context and acceptance details when needed.

### /rdo dispatch

```text
/rdo dispatch run=<run-id> task=<task-id> [backend=plain|tmux] [timeout=<seconds>]
```

Purpose: dispatch one task to the configured worker CLI.

Action: run `scripts/dispatch_claude.sh`.

Mapping:

```text
backend=tmux     -> RDO_WORKER_BACKEND=tmux
timeout=<secs>   -> RDO_TMUX_WAIT_TIMEOUT_SECONDS=<secs>
```

Default backend is `plain`. Use `tmux` only when attachable observation is useful.

`.agent-collab/rdo.toml` may define project defaults, but explicit `/rdo dispatch` arguments are one-off overrides and must not rewrite the config file.

### /rdo status

```text
/rdo status run=<run-id> [json] [summary] [diagnostics]
```

Purpose: inspect current run state.

Action: run `scripts/collect_status.py`.

Mapping:

```text
json         -> --json
summary      -> --write-summary
diagnostics  -> --write-diagnostics
```

`collect_status.py` is read-only except for derived `SUMMARY.md` and diagnostics outputs.

### /rdo review

```text
/rdo review run=<run-id> task=<task-id>
```

Purpose: review a task that is ready for Codex/coordinator review.

Action:

```text
load references/review-rubric.md
inspect STATUS.json, ATTEMPT.json, EVIDENCE.md, HANDOFF.md, logs, branch diff, allowed_paths
produce findings and recommendation
```

Important boundary:

```text
/rdo review does not automatically approve.
Only mutate review -> approved, review -> changes_requested, or review -> failed when the user explicitly asks and review gates support it.
```

### /rdo recover-lock

```text
/rdo recover-lock run=<run-id> task=<task-id>
```

Purpose: handle stale or ambiguous `.dispatch-lock`.

Action:

```text
load references/lock-recovery.md
perform Lock Recovery Review
report Finding / Evidence / Risk / Recommendation / Proposed mutation
ask for explicit user confirmation before removing .dispatch-lock
```

Only after confirmation, run `scripts/remove_dispatch_lock.py --confirmed`.

### /rdo close

```text
/rdo close run=<run-id> summary="<text>" [changed="<text>"] [next="<text>"]
```

Purpose: close the current work session and update long-term memory.

Action: run `scripts/close_session.py`.

Outputs: updated `SUMMARY.md`, appended `JOURNAL.md`, appended `session_closed` event.
