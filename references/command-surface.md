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

Read-only diagnostics:

```text
rdo cleanup audit --attempt-dir <path>
rdo task preview-prompt --task-dir <path>
```

Cleanup audit is eligible only after the attempt and its outer supervisor have
finished. It compares no historical PID or PGID as proof of ownership; it
reports only processes whose current userspace state exposes the attempt's
supervision token lineage. A zero result is therefore
`no_live_processes_observed`, not an absolute containment proof. The command
never writes protocol artifacts or sends signals. Exit status is `0` when no
tagged process is observed, `1` for observed live tagged processes, `2` for
ineligible/invalid evidence, and `126` when process inspection is unavailable.

`rdo task preview-prompt` renders the next dispatch prompt candidate without
creating an attempt, taking a lock, changing task state, or writing a prompt
file. Automatic backend, phase, and start/resume/replace selection mirrors the
deterministic pre-preflight dispatch rules. Its JSON result is explicitly
marked `selection_stage=preflight_candidate` and `byte_exact=false`: backend
preflight may still turn a resume candidate into a full-context start. Use
`--body-only` when only the rendered prompt is needed.

Worker-only commands:

```text
rdo strategy scaffold --attempt-dir <path>
rdo strategy preflight --attempt-dir <path> (--file <path|-> | --draft)
rdo strategy draft --attempt-dir <path> --file <path|->
rdo strategy submit|revise --task-dir <path> (--file <path|-> | --draft)
rdo workflow start|heartbeat|complete [--review-evidence REVIEWER_ID=ARTIFACT_PATH]
rdo exec --attempt-dir <path> --workflow-id <id> --instance-id <id> --timeout <seconds> -- <non-acceptance-command>
rdo check --attempt-dir <path> --check-id <acceptance-id>
rdo finalization begin --attempt-dir <path>
rdo finalize --attempt-dir <path> --state verified|review|blocked --summary <text>
```

Workers may submit artifacts and runtime events, but may not approve strategy, mutate `STATUS.json`, or merge.
For an active v2 Full planning attempt, `strategy scaffold` emits the next
policy-bounded revision without writing it. `strategy preflight` is read-only
and runs the exact immutable strategy-payload gates. `strategy draft` runs the same
preflight and atomically replaces only
`runtime/STRATEGY_DRAFT.json`; this mutable candidate is not evidence, an event,
or a reviewable strategy revision. A successful `submit|revise --draft`
revalidates those bytes before the existing immutable publication and handoff.
Using `--file -` reads one JSON object from stdin, eliminating the need for an
arbitrary `/tmp` draft.

`rdo check` selects exact `argv`, `cwd`, and timeout from the attempt's frozen
acceptance contract and appends a structured supervised record. In machine
mode the command remains inside the worker sandbox while the outer attempt
supervisor supplies the process-cleanup receipt; it never accepts arbitrary
argv from the broker protocol. Free text and `rdo exec --acceptance` cannot
satisfy a v2 acceptance gate.

`rdo finalization begin` explicitly freezes a Direct/Delegated source tree once
implementation, ordinary testing, and remediation are complete. Required
checks may have run immediately before entry or may be repeated during
finalization; RDO accepts them only when their source digests match the frozen
tree. Full enters automatically after its last required workflow. Repeating
begin is idempotent and never extends the deadline, which is fixed at the
original execution deadline plus the configured grace.

`rdo strategy submit|revise` and `rdo finalize` publish the attempt-local
`HANDOFF_READY.json` only after immutable handoff and evidence artifacts are
durable. In `tmux + human`, this allows deterministic worker shutdown; it does
not perform coordinator review or a task-state transition.

`rdo handoff`, task-root handoff/evidence, free-text command evidence, and
`COMPLETION.json` remain legacy-v0.5/v1 compatibility surfaces only.

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
$research-dev-orchestrator create-task run=<run-id> task=<task-id> goal="<text>" allowed=<path,path> profile=direct|delegated|full [forbidden=<path,path>]
```

Purpose: create a task packet with an explicit execution profile. Split the
work to one primary trust boundary before routing it. Use Direct for local
low-risk work whose implementation review can be worker-owned, Delegated when
independent coordinator judgment is required without strategy ceremony, and
Full for high-risk, materially cross-module, or explicitly strategy-gated work.
Full always adds pre-implementation strategy approval. Neither task prose nor
apparent complexity may silently select it.

Action: run `scripts/create_task.py`, then complete every required v2
task/context/acceptance section and machine contract before dispatch. The
scaffold intentionally remains unready until those coordinator-owned details
are supplied.

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
For v2, JSON output includes `status_projection`; summaries and the dashboard
use its attempt-attributed display fields rather than `STATUS.summary` or
`STATUS.evidence`. A previous publication is labeled with its attempt ID.

For a single task, `python scripts/rdo.py status --task-dir <task-dir>` returns
the same canonical projection under `projection`. Its separate raw `status`
member is the compatibility record and is not the v2 result/evidence source.

### review

```text
$research-dev-orchestrator review run=<run-id> task=<task-id>
```

Purpose: review a task that is ready for Codex/coordinator review.

Action:

```text
load references/review-rubric.md
resolve the current attempt bundle, then inspect STATUS.json, ATTEMPT.json,
TASK_INPUTS.json, EVIDENCE.json, HANDOFF.json, logs, branch diff, and path policy
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

An `approved` v2 decision binds the exact clean task-branch commit, source
branch, run target branch/commit, task inputs, handoff, evidence, and READY
digests. Merge rejects any task commit or reviewed artifact changed after
approval.

### merge

```bash
python scripts/rdo.py task merge \
  --task-dir <task-dir> \
  --target-worktree <path> \
  --expected-commit <commit> \
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

For v2, pre/post-merge commands come only from the frozen canonical
`ACCEPTANCE.md`; free `--verify-command` input is rejected. A failed post-merge
command returns non-zero but the task remains truthfully `merged`, because RDO
never rewinds a target branch after Git has accepted the commit. Legacy-v1
retains its historical optional `--verify-command` surface.

Every v2 merge records a verification object. If `verification.passed` is
false, Git truth remains `merged` and the merge command returns non-zero, but
dependency resolution exposes the task as `merged_unverified`; it cannot
satisfy another task's `required_state = merged` readiness gate. The
coordinator must arrange explicit remediation or a revision/repair task rather
than pretending the target branch was unmerged. The command appends the
`task_merged` event before advancing `STATUS.json`; replay can complete a
missing status transition. A historical `STATUS = merged`/missing-event crash
window is recovered conservatively with `verification.passed = false`.

Natural-language intent:

```text
$research-dev-orchestrator merge run=<run-id> task=<task-id> target-worktree=<path> [commit=<sha>]
```

Only an explicitly recognized legacy-v0.5/v1 task may map a separately supplied
verification command to its compatibility CLI option.

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
