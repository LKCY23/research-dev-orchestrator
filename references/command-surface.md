# Coordinator Intent Surface

Coordinator intents are structured natural-language requests for Codex. They are not executable shell commands, and they are not registered Codex slash commands.

The separate `scripts/rdo.py` executable is a narrow local control surface used by coordinator and worker agents through shell tools. It is not RPC and does not call a foreground agent session.

Coordinator-only commands:

```text
rdo strategy approve|changes
rdo task review|merge
rdo worker message|interrupt|terminate
rdo status
```

Worker-only commands:

```text
rdo strategy submit|revise
rdo workflow start|heartbeat|complete [--review-evidence REVIEWER_ID=ARTIFACT_PATH]
rdo exec --attempt-dir <path> --workflow-id <id> --instance-id <id> --timeout <seconds> [--acceptance] -- <command>
rdo finalize --task-dir <path> --state verified|review|blocked --summary <text>
```

Workers may submit artifacts and runtime events, but may not approve strategy, mutate `STATUS.json`, or merge.
`rdo strategy submit|revise` and `rdo finalize` commit an attempt-bound
`COMPLETION.json` only after their handoff artifacts are durable. In
`tmux + human`, this allows deterministic worker shutdown; it does not perform
coordinator review or a task-state transition.

`rdo handoff` remains a compatibility surface. New prompts use `rdo finalize`, which derives acceptance commands and changed files instead of requiring workers to hand-author three overlapping handoff artifacts.

`--review-evidence` is accepted only when completing a strategy-declared independent review workflow. Repeat it once per reviewer; artifacts must be non-empty files under the current attempt's `runtime/reviews/`, and reviewer IDs must match observed backend agent lifecycle events.

Use them in either of these forms:

```text
$research-dev-orchestrator init project=<slug> objective="<text>"
$research-dev-orchestrator dispatch run=<run-id> task=<task-id> backend=tmux
```

Or select the skill through `/skills`, then write the same intent in natural language:

```text
Initialize a run for this repository.
Dispatch task T001 with the tmux backend.
Collect status and write SUMMARY.md.
```

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

## Intents

### init

```text
$research-dev-orchestrator init project=<slug> objective="<text>" [target=<branch>]
```

Purpose: create a new run scaffold.

Action: run `scripts/init_run.py`.

Outputs: `RUN.json`, required run artifacts, `EVENTS.ndjson`, `JOURNAL.md`, `SUMMARY.md`.

### plan

```text
$research-dev-orchestrator plan run=<run-id> [scope=requirements|design|experiment|all]
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

### create-task

```text
$research-dev-orchestrator create-task run=<run-id> task=<task-id> goal="<text>" allowed=<path,path> [forbidden=<path,path>] [profile=direct|delegated|full]
```

Purpose: create a task packet with an explicit execution profile. Use Direct for small low-risk work, Delegated for independent coordinator review without strategy ceremony, and Full for strategy-gated work.

Action: run `scripts/create_task.py`, then fill task context and acceptance details when needed.

### dispatch

```text
$research-dev-orchestrator dispatch run=<run-id> task=<task-id> [worker=claude-code|codex|opencode|kimi-code] [runtime=plain|tmux] [io=machine|human] [permission=default|auto|yolo] [timeout=<seconds>]
```

Purpose: dispatch one task to the configured worker CLI.

Action: run `scripts/dispatch_agent.sh`.

Mapping:

```text
worker=<id>      -> RDO_WORKER_BACKEND=<id>
runtime=tmux     -> RDO_RUNTIME_BACKEND=tmux
io=human         -> RDO_IO_MODE=human
permission=yolo  -> RDO_PERMISSION_MODE=yolo
timeout=<secs>   -> RDO_TMUX_WAIT_TIMEOUT_SECONDS=<secs>
```

Default runtime is `plain` with `machine` IO. Use `tmux` with `human` IO only when attachable observation is useful.

`.agent-collab/rdo.toml` may define project defaults, but explicit dispatch intent arguments are one-off overrides and must not rewrite the config file.

### status

```text
$research-dev-orchestrator status run=<run-id> [json] [summary] [dashboard] [diagnostics]
```

Purpose: inspect current run state.

Action: run `scripts/collect_status.py`.

Mapping:

```text
json         -> --json
summary      -> --write-summary
dashboard    -> scripts/render_dashboard.py --run-id <run-id>
diagnostics  -> --write-diagnostics
```

`collect_status.py` is read-only except for derived `SUMMARY.md` and diagnostics outputs.
`render_dashboard.py` writes only derived `dashboard.html`.

### review

```text
$research-dev-orchestrator review run=<run-id> task=<task-id>
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
review does not automatically approve.
Only mutate review -> approved, review -> changes_requested, or review -> failed when the user explicitly asks and review gates support it.
```

After that explicit decision, use the coordinator command rather than editing
`STATUS.json`:

```bash
python scripts/rdo.py task review \
  --task-dir <task-dir> \
  --decision approved|changes_requested|failed \
  --reviewer <coordinator-id> \
  --findings-file <task-local-review-file>
```

The command binds the non-empty task-local findings file by SHA-256, writes an
immutable `reviews/DECISION-vNNN.json`, updates
`reviews/CURRENT_TASK_REVIEW.json`, performs the legal coordinator FSM
transition, and appends review events. When the decision is
`changes_requested`, subsequent planning and execution prompts include the
digest-verified findings until a newer task review decision supersedes them.

An `approved` decision also binds the exact clean task-branch commit, source
branch, run target branch/commit, and current evidence/handoff digests. Merge
rejects any task commit or reviewed artifact changed after approval.

### merge

```bash
python scripts/rdo.py task merge \
  --task-dir <task-dir> \
  --target-worktree <path> \
  --expected-commit <commit> \
  [--verify-command "pytest -q"] \
  [--verification-timeout 300] \
  --coordinator <coordinator-id>
```

Purpose: perform the coordinator-owned mechanical merge gate for an approved
task or a verified Direct task.

The command derives source and target branches from `STATUS.json` and
`RUN.json`, requires clean task and target worktrees, permits only
fast-forward merge, and uses Git ancestry as the merge source of truth. If Git
already contains the bound task commit but task state is not yet `merged`, the
same command resumes verification and protocol recording. Repeated completed
invocations do not duplicate `task_merged` events. No `MERGE.json` artifact is
created.

Optional post-merge commands run as parsed argv without a shell and write one
task-local `logs/post-merge.log`. A failed post-merge command returns non-zero
but the task remains truthfully `merged`, because RDO never rewinds a target
branch after Git has accepted the commit.

Natural-language intent:

```text
$research-dev-orchestrator merge run=<run-id> task=<task-id> target-worktree=<path> [commit=<sha>] [verify="<command>"]
```

### recover-lock

```text
$research-dev-orchestrator recover-lock run=<run-id> task=<task-id>
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

### close

```text
$research-dev-orchestrator close run=<run-id> summary="<text>" [changed="<text>"] [next="<text>"]
```

Purpose: close the current work session and update long-term memory.

Action: run `scripts/close_session.py`.

Outputs: updated `SUMMARY.md`, appended `JOURNAL.md`, appended `session_closed` event.
