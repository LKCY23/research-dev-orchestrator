# research-dev-orchestrator

[![Smoke Tests](https://github.com/LKCY23/research-dev-orchestrator/actions/workflows/smoke.yml/badge.svg)](https://github.com/LKCY23/research-dev-orchestrator/actions/workflows/smoke.yml)

[English](README.md) | [简体中文](README.zh-CN.md)

A repo-local orchestration protocol for turning research ideas into reproducible experiment code with Codex as the coordinator and CLI coding agents as workers.

Research code often evolves over weeks: requirements shift, baselines change, experiments fail, agents lose context, and results become hard to audit. `research-dev-orchestrator` gives Codex a lightweight way to manage that lifecycle without a server, database, queue, or daemon.

The runtime entrypoint is [SKILL.md](SKILL.md). The detailed design baseline is [DESIGN_SPEC.md](DESIGN_SPEC.md).

## Why This Exists

Short agent coding workflows are usually easy to inspect: one prompt, one patch, one review. Research and experiment development is different:

- Experiments span days or weeks.
- Requirements, datasets, baselines, and metrics change.
- Failed attempts matter because they explain why later decisions were made.
- Reproducibility artifacts are as important as implementation code.
- Review needs evidence, not just a diff.
- Humans and agents both forget context.

This project turns that long-running workflow into durable files inside the target repository.

## What It Is

`research-dev-orchestrator` is a Codex skill plus a set of scripts and protocol templates. It helps Codex:

- clarify requirements and experiment goals;
- choose a design method and record architecture decisions;
- create task packets with acceptance criteria and allowed paths;
- dispatch CLI coding agents such as Claude Code into isolated Git worktrees;
- validate worker handoffs using deterministic protocol gates;
- collect status, evidence, diagnostics, and long-term memory;
- support evidence-based Codex/human review before merge.

It is intentionally small: the protocol is files plus Git.

## Core Design

The design is built around four rules:

1. **Codex owns intent**
   Requirements, experiment design, task decomposition, acceptance criteria, review, and merge decisions stay with the coordinator.

2. **Workers own execution**
   A worker receives one task packet, works in one branch/worktree, and writes evidence plus a handoff.

3. **Filesystem is the protocol**
   Agents communicate through repo-local files such as `STATUS.json`, `ATTEMPT.json`, `EVENTS.ndjson`, and `JOURNAL.md`.

4. **Git is the isolation boundary**
   Each task uses an isolated branch/worktree. Workers do not merge.

## Architecture

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"Inter, ui-sans-serif, system-ui","primaryColor":"#f8fafc","primaryTextColor":"#0f172a","primaryBorderColor":"#cbd5e1","lineColor":"#64748b","tertiaryColor":"#ffffff"},"flowchart":{"curve":"basis"}}}%%
flowchart TB
  subgraph L1["Coordinator Layer"]
    direction TB
    U["User"]:::human
    C["Coordinator (Codex)"]:::coord
    U --> C
  end

  subgraph L2["Planning Layer"]
    direction TB
    P["Planning"]:::planning
    T["Task Contract"]:::planning
    P --> T
  end

  subgraph L3["Execution Layer"]
    direction TB
    D["Dispatcher"]:::exec
    W["Worker<br/>isolated worktree + backend"]:::exec
    D -- "launch" --> W
  end

  subgraph L4["Run Store"]
    direction TB
    S["Repo-local system of record<br/>state · evidence · events · memory"]:::truth
  end

  subgraph L5["Validation & Recovery Layer"]
    direction TB
    V["Validate"]:::validate
    O["Monitor"]:::validate
    R["Recover"]:::validate
    V --> O
    O --> R
  end

  C --> P
  T --> D
  W -- "handoff" --> S
  S --> V

  D -. "attempt metadata" .-> S
  C -. "notes, review decisions" .-> S
  O -. "status, blockers" .-> C
  R -- "approved repair" --> S

  classDef human fill:#eff6ff,stroke:#2563eb,color:#1e3a8a;
  classDef coord fill:#eef2ff,stroke:#4f46e5,color:#312e81;
  classDef planning fill:#f5f3ff,stroke:#7c3aed,color:#4c1d95;
  classDef exec fill:#fffbeb,stroke:#d97706,color:#78350f;
  classDef truth fill:#ecfdf5,stroke:#059669,color:#064e3b;
  classDef validate fill:#fff1f2,stroke:#e11d48,color:#881337;

  style L1 fill:#f8fbff,stroke:#bfdbfe,stroke-width:1px,color:#1e3a8a;
  style L2 fill:#fbfaff,stroke:#ddd6fe,stroke-width:1px,color:#4c1d95;
  style L3 fill:#fffdf5,stroke:#fde68a,stroke-width:1px,color:#78350f;
  style L4 fill:#f7fefb,stroke:#bbf7d0,stroke-width:1px,color:#064e3b;
  style L5 fill:#fff8f9,stroke:#fecdd3,stroke-width:1px,color:#881337;
```

The architecture is organized around ownership boundaries. The coordinator owns intent and review decisions. Workers own bounded execution. Git isolates implementation changes. The Run Store is the repo-local system of record for task state, attempt lifecycle, handoff evidence, events, memory, results, and recovery context. Validation gates worker handoffs; monitoring scripts produce derived artifacts without becoming a long-running service. Monitor output informs coordinator review, and recovery writes only user-approved minimal mutations back into the Run Store.

Implementation details are intentionally secondary in the diagram:

| Plane | Responsibility | Main implementation |
| --- | --- | --- |
| Coordinator | Requirements, design, task split, review, merge decisions | `SKILL.md`, `$research-dev-orchestrator` intent surface |
| Planning | Durable research intent and task contracts | `REQUIREMENTS.md`, `DESIGN_BRIEF.md`, `ADR/`, `EXPERIMENT_PLAN.md`, `TASK.md`, `ACCEPTANCE.md` |
| Execution | Worker dispatch, attempt supervision, Git-isolated execution | `dispatch_claude.sh`, `dispatch_assets.py`, plain/tmux backends, Git worktree |
| Run Store | Repo-local system of record for task state, attempt lifecycle, handoff evidence, event timeline, memory, results, and recovery context | `.agent-collab/runs/<run-id>/`, `STATUS.json`, `ATTEMPT.json`, `EVIDENCE.md`, `HANDOFF.md`, `EVENTS.ndjson`, `JOURNAL.md`, `RESULT_LEDGER.md` |
| Validation & recovery | Deterministic gates, read-only audit, derived reports, user-approved recovery | `validation.py`, `protocol_cli.py`, `collect_status.py`, `SUMMARY.md`, `diagnostics/` |

## Workflow

The intended flow is sequential but resumable:

```text
requirements
-> design method selection
-> architecture / experiment design
-> task packet
-> dispatch
-> worker handoff
-> collect status
-> Codex review
-> merge
-> close session
```

A run captures the full lifecycle: requirements, design notes, experiment plans, tasks, attempts, reviews, results, diagnostics, and memory.

## Protocol Files

The target repository gets a local `.agent-collab/` directory:

```text
.agent-collab/
  rdo.toml
  runs/
    <run-id>/
      RUN.json
      SUMMARY.md
      dashboard.html
      EVENTS.ndjson
      JOURNAL.md
      EXPERIMENT_PLAN.md
      REPRODUCIBILITY.md
      RESULT_LEDGER.md
      tasks/
        <task-id>/
          TASK.md
          CONTEXT.md
          ACCEPTANCE.md
          STATUS.json
          EVIDENCE.md
          HANDOFF.md
          HANDOFF.json
          attempts/
            <attempt-id>/
              ATTEMPT.json
              prompt.md
              transcript.log
              result.md
```

Key files:

- `STATUS.json`: task progress and finite-state-machine state.
- `ATTEMPT.json`: worker execution lifecycle for one attempt.
- `EVENTS.ndjson`: append-only machine-readable timeline.
- `JOURNAL.md`: human-readable session memory.
- `SUMMARY.md`: derived dashboard generated by `collect_status.py`.
- `dashboard.html`: derived human monitor generated by `render_dashboard.py`.
- `EVIDENCE.md`: commands, tests, metrics, outputs, and logs.
- `HANDOFF.md`: worker handoff summary and known limitations.
- `HANDOFF.json`: optional machine-readable handoff summary index.

See [references/state-machine.md](references/state-machine.md), [references/status-schema.md](references/status-schema.md), [references/attempt-lifecycle.md](references/attempt-lifecycle.md), and [references/events-schema.md](references/events-schema.md) for protocol details.

## Execution State Model: Tasks and Attempts

The execution state model separates work progress from worker execution.

A task is the durable work item: intent, constraints, acceptance criteria, and coordinator-owned progress. An attempt is one bounded worker execution trajectory for that task, materialized as an attempt directory with prompt, runtime metadata, transcript, result, evidence, and handoff.

Coordinator review is the boundary between them: an attempt can provide evidence, but only review can advance task state.

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"Inter, ui-sans-serif, system-ui","primaryColor":"#f8fafc","primaryTextColor":"#0f172a","primaryBorderColor":"#cbd5e1","lineColor":"#64748b","tertiaryColor":"#ffffff"},"flowchart":{"curve":"basis"}}}%%
flowchart TB
  T["Task<br/>intent + acceptance"]:::task
  TS["Task State<br/>coordinator-owned progress"]:::task
  A["Attempt<br/>filesystem execution trajectory"]:::attempt
  E["Evidence + Handoff<br/>tests, logs, outputs"]:::artifact
  R["Coordinator Review<br/>advance / revise / block / fail"]:::review
  RS["Run Store<br/>state, attempts, evidence, events"]:::store

  T --> TS
  TS -- "dispatch / retry" --> A
  A --> E
  E --> R
  R -- "state transition" --> TS

  TS --> RS
  A --> RS
  E --> RS
  R --> RS

  classDef task fill:#eef2ff,stroke:#4f46e5,color:#312e81;
  classDef attempt fill:#fffbeb,stroke:#d97706,color:#78350f;
  classDef artifact fill:#ecfdf5,stroke:#059669,color:#064e3b;
  classDef review fill:#fff1f2,stroke:#e11d48,color:#881337;
  classDef store fill:#f8fafc,stroke:#475569,color:#0f172a;
```

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"Inter, ui-sans-serif, system-ui","primaryColor":"#f8fafc","primaryTextColor":"#0f172a","primaryBorderColor":"#cbd5e1","lineColor":"#64748b","clusterBkg":"#ffffff","clusterBorder":"#cbd5e1"}}}%%
flowchart LR
  subgraph TaskFSM["Task FSM: work progress"]
    P["pending"]:::task --> R["running"]:::task
    R --> V["review"]:::task
    R --> B["blocked"]:::warn
    V --> A["approved"]:::ok
    V --> CR["changes_requested"]:::warn
    A --> M["merged"]:::ok
    CR --> R
    B --> R
    B --> F["failed"]:::bad
    V --> F
  end

  subgraph AttemptLifecycle["Attempt lifecycle: worker execution trajectory"]
    C["created"]:::attempt --> AR["running"]:::attempt
    AR --> DONE["completed<br/>valid handoff"]:::ok
    AR --> BAD["invalid_handoff<br/>bad or missing handoff"]:::bad
  end

  classDef task fill:#eef2ff,stroke:#4f46e5,color:#312e81;
  classDef attempt fill:#f8fafc,stroke:#475569,color:#0f172a;
  classDef ok fill:#ecfdf5,stroke:#059669,color:#064e3b;
  classDef warn fill:#fffbeb,stroke:#d97706,color:#78350f;
  classDef bad fill:#fff1f2,stroke:#e11d48,color:#881337;
```

Worker failure affects an attempt first, not the task directly. A completed attempt is review-ready evidence, not automatic task completion. This lets the system retry, inspect, compare, and recover worker executions without losing the task's intent or history.

## Runtime Backends

Two worker execution backends are supported:

- `plain`: default direct execution from `dispatch_claude.sh`.
- `tmux`: attachable execution for long-running workers.

The tmux backend is still synchronous from dispatch's protocol perspective. It is not a daemon, watcher, queue, or source of truth. Completion is determined by the attempt-local `exit_code` file and validated protocol files, not by tmux session state.

See [references/runtime-backends.md](references/runtime-backends.md) and [references/lock-recovery.md](references/lock-recovery.md).

## Long-Term Memory

Long-running research work needs explicit memory:

- `SUMMARY.md`: current dashboard.
- `JOURNAL.md`: human session notes and next actions.
- `EVENTS.ndjson`: append-only machine timeline.
- `RESULT_LEDGER.md`: experiment outcomes and claim support.
- `reviews/`: Codex/human review records.
- `tasks/*/attempts/`: worker execution records.

The goal is that a user or Codex can resume weeks later and answer: what changed, why it changed, what failed, what evidence exists, and what remains blocked.

## Installation

Install this repository as a Codex skill for the coordinator:

```bash
mkdir -p ~/.codex/skills
git clone https://github.com/LKCY23/research-dev-orchestrator.git \
  ~/.codex/skills/research-dev-orchestrator
```

Only the coordinator needs the skill installed. Claude Code or other CLI workers do not need to install this skill; they are launched by dispatch with task-packet paths and protocol instructions. A worker only needs the configured CLI command available, for example:

```bash
export CLAUDE_CODE_CMD=claude
```

For a cleaner final skill package, keep `SKILL.md`, `references/`, `scripts/`, `templates/`, and `agents/openai.yaml`; `README.md`, `DESIGN_SPEC.md`, `.github/`, and `tests/` are development artifacts.

## Quick Start

From a target repository, ask Codex to use the skill:

```text
Use $research-dev-orchestrator to initialize a run for a reproducible RAG benchmark pipeline.
```

You can also select the skill with Codex's built-in `/skills` picker, then ask for the same action in natural language. The examples here are skill invocations and intent phrases, not custom slash commands registered by the skill.

Codex should then:

1. clarify requirements and experiment details with you;
2. create a run under `.agent-collab/runs/<run-id>/`;
3. create task packets with acceptance criteria;
4. dispatch CLI workers when a task is ready;
5. collect status and review worker evidence;
6. update `SUMMARY.md`, `JOURNAL.md`, and related run artifacts at session closeout.

The worker side remains CLI-based. Configure defaults in `.agent-collab/rdo.toml` or with environment variables such as `CLAUDE_CODE_CMD`, `RDO_WORKER_BACKEND`, and `RDO_TMUX_KEEP_SESSION`.

### Direct script usage

For development, debugging, or running without Codex skill discovery, clone this repository and call scripts by absolute path from the target repository:

```bash
git clone https://github.com/LKCY23/research-dev-orchestrator.git
export RESEARCH_DEV_ORCHESTRATOR_HOME=/path/to/research-dev-orchestrator
```

Initialize a run:

```bash
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/init_run.py" \
  --project-slug rag-benchmark \
  --objective "Build a reproducible RAG benchmark pipeline" \
  --target-branch main
```

Create a task:

```bash
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/create_task.py" \
  --run-id <run-id> \
  --task-id T001-data-loader \
  --goal "Implement the dataset loader and smoke tests" \
  --allowed-paths src tests
```

Dispatch a worker:

```bash
"$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/dispatch_claude.sh" <run-id> T001-data-loader
```

Collect status:

```bash
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id>
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --write-summary
```

Close a session:

```bash
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/close_session.py" \
  --run-id <run-id> \
  --summary "Implemented loader first pass and identified schema blocker."
```

## Example Usage

Use tmux when you want to attach to a long-running worker:

```bash
RDO_WORKER_BACKEND=tmux \
  "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/dispatch_claude.sh" <run-id> T001-data-loader
```

Operational defaults live in `.agent-collab/rdo.toml`, but protocol truth is not configurable. Config may choose defaults such as backend, worker command, stale thresholds, and task path prefixes. It cannot change FSM states, blocker types, event types, protocol version, or review semantics.

See [references/configuration.md](references/configuration.md).

## Monitoring

The project has four monitor surfaces:

- Visual monitor: `.agent-collab/runs/<run-id>/dashboard.html`.
- Human-readable summary: `.agent-collab/runs/<run-id>/SUMMARY.md`.
- Interactive monitor: `python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id>`.
- Machine-readable monitor: `python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --json`.

Regenerate the visual dashboard and human-readable summary with:

```bash
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/render_dashboard.py" \
  --run-id <run-id>
```

```bash
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" \
  --run-id <run-id> \
  --write-summary
```

`dashboard.html` and `SUMMARY.md` are derived monitors, not protocol truth. The source of truth remains `RUN.json`, task `STATUS.json`, attempt `ATTEMPT.json`, `EVENTS.ndjson`, `EVIDENCE.md`, `HANDOFF.md`, and `RESULT_LEDGER.md`. Optional `HANDOFF.json` is a non-authoritative index for summaries and dashboards. Protocol warnings and recovery snapshots are written under `diagnostics/`.

## Versioning

This project tracks two versions in [VERSION](VERSION):

- `PACKAGE_VERSION`: the installable skill/repository release version.
- `PROTOCOL_VERSION`: the Run Store file protocol version written to `RUN.json`.

A package release declares the protocol version it implements, but patch releases may keep the same protocol version. Protocol version changes only when Run Store schemas, FSM transitions, event formats, or directory layout change.

## Validation and CI

CI runs automatically on pushes to `main` and on pull requests. It does not require secrets and does not call real model-backed workers.

The smoke tests use fake workers. They validate the protocol and orchestration behavior without consuming model/API budget:

- Python scripts compile.
- Bash scripts parse.
- Skill metadata is valid.
- Protocol smoke tests pass.
- `git diff --check` passes.

Local CI equivalent:

```bash
python3 .github/ci/quick_validate_skill.py .
python3 -m py_compile scripts/*.py .github/ci/quick_validate_skill.py
bash -n scripts/dispatch_claude.sh scripts/run_smoke_tests.sh tests/smoke/*.sh
RDO_KEEP_SMOKE_REPOS=0 scripts/run_smoke_tests.sh
git diff --check
```

For local debugging, omit `RDO_KEEP_SMOKE_REPOS=0` to keep temporary smoke-test repositories.

## Repository Layout

```text
SKILL.md                 # Codex skill runtime entrypoint
README.zh-CN.md          # Simplified Chinese README
DESIGN_SPEC.md           # Full design baseline and protocol rationale
LICENSE                  # MIT license
VERSION                  # Package and Run Store protocol versions
CHANGELOG.md             # Release history
references/              # FSM, schemas, review rubric, workflow and memory docs
scripts/                 # protocol, config, validation, dispatch, collect, close_session
templates/               # Scaffold source for run and task files
tests/smoke/             # Protocol and dispatch smoke tests using fake workers
agents/openai.yaml       # Codex UI metadata
.github/workflows/       # GitHub Actions smoke CI
```

If packaging this as a final Codex skill, include `SKILL.md`, `references/`, `scripts/`, `templates/`, and `agents/openai.yaml`. `README.md`, `DESIGN_SPEC.md`, `.github/`, and `tests/` can remain development artifacts.

## Design Boundaries

This is not:

- a server;
- an RPC framework;
- a queue;
- a daemon;
- an automatic code reviewer;
- a replacement for Codex/human review;
- a system that automatically repairs corrupted protocol truth.

Agent writes are never trusted. Deterministic validation gates them. Validation may mark a handoff invalid, but semantic repair requires coordinator/user review.

## Roadmap

- Better installation packaging as a Codex skill.
- More protocol validators and recovery review helpers.
- Optional real-worker integration tests.
- More examples for research experiment workflows.
- Optional argv-array worker command mode.

## Contributing

Before opening a pull request, run the local CI equivalent above.

When changing protocol behavior:

- update the relevant files in `references/`;
- update smoke tests;
- keep constants in `scripts/protocol.py`;
- keep shared validation rules in `scripts/validation.py`;
- keep coordinator-only decisions out of `scripts/protocol_cli.py`.

Please do not add a server, daemon, RPC layer, queue, or automatic protocol repair without a design discussion.

## License

MIT. See [LICENSE](LICENSE).
