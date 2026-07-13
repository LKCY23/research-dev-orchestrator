# Execution Strategy Protocol

Strategy schema v2 binds an approved strategy to `backend_id`. Dispatch compiles
the strategy into backend-native controls before lock acquisition. Claude Code
native-agent concurrency is hook-enforced. Codex uses native thread/depth
settings; its cumulative spawn budget and JSONL supervisor are optional. Additional
backends accept only controls their adapters implement. See
`references/backend-governance.md`.

Execution strategy is a task-level, coordinator-reviewed contract. It allows workers to use multiple workflows, skills, and subagents without giving unreviewed work an unlimited runtime or scope.

`EXECUTION_POLICY.json` copies the task's allowed and forbidden paths into the deterministic policy envelope. A workflow may narrow an allowed path, but may not widen it or overlap a forbidden path.

## Artifacts

```text
tasks/<task-id>/
  EXECUTION_POLICY.json
  strategy/
    CURRENT.json
    STRATEGY-v001.json
    REVIEW-v001.json
    STRATEGY-v002.json
    REVIEW-v002.json
```

Strategy and review revisions are immutable. `CURRENT.json` is a derived pointer to the latest approved revision. A review binds the SHA-256 digest of exactly one strategy revision.

## Planning Contract

A planning attempt is read-only with respect to the task worktree. It may inspect task context and source files, but may write only its attempt artifacts and the next strategy revision. A valid planning handoff requests `strategy_review`; dispatch validates the revision and applies `planning -> strategy_review`.

Dispatch fingerprints tracked and untracked worktree content before and after planning. Any content change invalidates the handoff and blocks the task for coordinator review; the changed files are retained as evidence rather than silently reverted.

Execution uses the same fingerprints to reject changes outside the union of write-enabled workflow paths in the approved strategy or inside task-level forbidden paths. This check includes committed, uncommitted, and untracked content changes.

The execution worker starts only after coordinator approval. Approval does not resume a paused interactive worker: dispatch creates a new execution attempt with the approved strategy in its prompt.

## Strategy Schema

Each `STRATEGY-vNNN.json` contains:

```json
{
  "schema_version": 2,
  "backend_id": "claude-code",
  "strategy_id": "T001-S001",
  "task_id": "T001-name",
  "revision": 1,
  "supersedes": null,
  "objective": "Implement the task contract",
  "global_budget": {
    "wall_seconds": 2700,
    "max_workflows": 6,
    "max_workflow_instances": 12,
    "max_parallel_workflows": 2,
    "max_subagents": 4,
    "max_parallel_subagents": 2
  },
  "workflows": [
    {
      "workflow_id": "WF-implementation",
      "kind": "implementation",
      "purpose": "Implement the accepted task contract",
      "depends_on": [],
      "required": true,
      "executor": {
        "mode": "primary_worker",
        "write_access": true,
        "max_agents": 0,
        "max_parallel": 0,
        "allowed_paths": ["src/"]
      },
      "budget": {
        "wall_seconds": 1200,
        "command_seconds": 120,
        "max_enumerated_cases": 100,
        "max_instances": 1
      },
      "completion": {"evidence": "acceptance commands pass"},
      "on_timeout": "block"
    }
  ],
  "runtime_change_policy": {
    "allow_new_instances_of_approved_workflows": true,
    "require_revision_for_new_workflow_kind": true,
    "require_revision_for_budget_increase": true,
    "allow_unbounded_search": false
  },
  "completion_gate": {
    "required_workflows_complete": true,
    "acceptance_commands_pass": true,
    "optional_workflows_may_timeout": true
  }
}
```

Each workflow definition has a stable `workflow_id`, `kind`, `purpose`, `depends_on`, `required`, `executor`, `budget`, `completion`, and `on_timeout`. `budget.max_instances` bounds runtime instances of that definition. Counts and durations are policy values, never protocol constants.

Commands run through `rdo exec` are bounded and audited. Only commands explicitly marked `--acceptance` participate in `completion_gate.acceptance_commands_pass`; exploratory failures remain evidence but do not permanently poison a later valid handoff.

## Multiple Workflows

A workflow definition is approved strategy. A workflow instance is runtime activity such as `WF-review-I002`. Workers may create new instances inside an approved definition without another review when instance, concurrency, permission, path, and budget limits remain satisfied.

A new strategy revision is required before introducing a new workflow kind, increasing budget or concurrency, granting write access, widening paths, or starting an exhaustive/unknown-size search. The execution worker writes a checkpoint and a revision request, then exits; dispatch applies `running -> strategy_review` only after validating the new revision.

## Strategy Review

`REVIEW-vNNN.json` contains `strategy_id`, `strategy_sha256`, `decision`, `reviewer`, `reviewed_at`, and `notes`. Decisions are `approved` or `changes_requested`. The coordinator never edits worker strategy in place.

An approval applies atomically to the complete strategy revision. Changes requested cause a new planning attempt and a new immutable revision.
