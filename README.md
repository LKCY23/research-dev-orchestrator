# research-dev-orchestrator

Codex skill for coordinating research proposal work, experiment design, reproducible implementation, and evidence-based review with CLI coding agents as workers.

The skill runtime entrypoint is [`SKILL.md`](SKILL.md). The detailed design baseline is [`DESIGN_SPEC.md`](DESIGN_SPEC.md).

## What It Does

- Turns research or experiment goals into requirements, design method selection, design briefs, ADRs, experiment plans, and reproducibility contracts.
- Creates repo-local orchestration runs under `.agent-collab/runs/<run-id>/`.
- Decomposes work into task packets with explicit allowed paths, acceptance criteria, evidence, and handoff files.
- Dispatches CLI coding agents through a filesystem protocol and Git worktree isolation.
- Supports `plain` worker execution by default and optional attachable `tmux` execution for long-running workers.
- Enforces a finite state machine through `references/state-machine.json`.
- Keeps worker/process lifecycle in `ATTEMPT.json` instead of expanding the task FSM.
- Provides human, machine, and diagnostic monitoring through `SUMMARY.md`, `collect_status.py --json`, and `diagnostics/`.
- Preserves long-running context with required append-only memory files: `EVENTS.ndjson` and `JOURNAL.md`.
- Prevents destructive overwrites of audit-bearing artifacts; use a new run, new attempt, or revision task.
- Uses `collect_status.py` as an invariant checker across `STATUS.json`, `ATTEMPT.json`, `LOCK`, `.dispatch-lock`, `EVENTS.ndjson`, `EVIDENCE.md`, and `HANDOFF.md`.
- Provides an explicit stale dispatch-lock recovery workflow: detect with `collect_status.py`, review with the coordinator, confirm with the user, then remove only `.dispatch-lock` with an audited snapshot/event.

## Worker Backends

Default dispatch is direct and synchronous:

```bash
"$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/dispatch_claude.sh" <run-id> <task-id>
```

For attachable long-running workers, use tmux:

```bash
RDO_WORKER_BACKEND=tmux "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/dispatch_claude.sh" <run-id> <task-id>
```

The tmux backend is still synchronous from dispatch's protocol perspective. The completion source of truth is the attempt-local `exit_code` file, not a tmux signal. If dispatch times out before that file appears, it exits `5`, keeps `.dispatch-lock`, leaves `ATTEMPT.state=running`, writes diagnostics, and requires Lock Recovery Review.

## Repository Layout

```text
SKILL.md                 # Codex skill entrypoint
DESIGN_SPEC.md           # Full design baseline and protocol rationale
references/              # Templates, FSM, schema, review rubric, memory docs
scripts/                 # init_run, create_task, dispatch, collect_status, close_session
agents/openai.yaml       # UI metadata
```

## Basic Validation

```bash
python /path/to/skill-creator/scripts/quick_validate.py /path/to/research-dev-orchestrator
python -m py_compile scripts/init_run.py scripts/create_task.py scripts/collect_status.py scripts/close_session.py
bash -n scripts/dispatch_claude.sh
```

When using the skill from another target repository, keep the current working directory at that target repository root and call this repository's scripts by absolute path, or set:

```bash
export RESEARCH_DEV_ORCHESTRATOR_HOME=/path/to/research-dev-orchestrator
```

At the end of a working session, use:

```bash
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/close_session.py" --run-id <run-id> --summary "<what happened>"
```

## Notes

This repository intentionally keeps the long design document separate from `SKILL.md`. If packaging the final skill for installation, include `SKILL.md`, `references/`, `scripts/`, and `agents/openai.yaml`; `DESIGN_SPEC.md` and this README can remain development artifacts.
