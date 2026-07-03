# research-dev-orchestrator

Codex skill for coordinating research proposal work, experiment design, reproducible implementation, and evidence-based review with CLI coding agents as workers.

The skill runtime entrypoint is [`SKILL.md`](SKILL.md). The detailed design baseline is [`DESIGN_SPEC.md`](DESIGN_SPEC.md).

## What It Does

- Turns research or experiment goals into requirements, design method selection, design briefs, ADRs, experiment plans, and reproducibility contracts.
- Creates repo-local orchestration runs under `.agent-collab/runs/<run-id>/`.
- Decomposes work into task packets with explicit allowed paths, acceptance criteria, evidence, and handoff files.
- Dispatches CLI coding agents through a filesystem protocol and Git worktree isolation.
- Enforces a finite state machine through `references/state-machine.json`.
- Provides human, machine, and diagnostic monitoring through `SUMMARY.md`, `collect_status.py --json`, and `diagnostics/`.

## Repository Layout

```text
SKILL.md                 # Codex skill entrypoint
DESIGN_SPEC.md           # Full design baseline and protocol rationale
references/              # Templates, FSM, schema, review rubric
scripts/                 # init_run, create_task, dispatch, collect_status
agents/openai.yaml       # UI metadata
```

## Basic Validation

```bash
python /path/to/skill-creator/scripts/quick_validate.py /path/to/research-dev-orchestrator
python -m py_compile scripts/init_run.py scripts/create_task.py scripts/collect_status.py
bash -n scripts/dispatch_claude.sh
```

When using the skill from another target repository, keep the current working directory at that target repository root and call this repository's scripts by absolute path, or set:

```bash
export RESEARCH_DEV_ORCHESTRATOR_HOME=/path/to/research-dev-orchestrator
```

## Notes

This repository intentionally keeps the long design document separate from `SKILL.md`. If packaging the final skill for installation, include `SKILL.md`, `references/`, `scripts/`, and `agents/openai.yaml`; `DESIGN_SPEC.md` and this README can remain development artifacts.
