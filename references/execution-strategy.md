# Execution Strategy Protocol

Strategy schema v2 binds an approved strategy to `backend_id`. Dispatch compiles
the strategy into backend-native controls before lock acquisition. Claude Code
native-agent concurrency is hook-enforced. Codex uses native thread/depth
settings; its cumulative spawn budget and JSONL supervisor are optional. Additional
backends accept only controls their adapters implement. See
`references/backend-governance.md`.

Execution strategy is a task-level, coordinator-reviewed contract. It allows workers to use multiple workflows, skills, and subagents without giving unreviewed work an unlimited runtime or scope.

`EXECUTION_POLICY.json` separates writable `allowed_paths` from discovery
`read_paths` and carries forbidden paths in the deterministic policy envelope.
A workflow may narrow an allowed path, but may not widen it or overlap a
forbidden path. Attempt compilation combines this boundary with the
explicit `EXECUTION_POLICY.json.context_sources` in `runtime/READ_POLICY.json`.

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

The planning prompt and `rdo strategy scaffold` share one deterministic,
policy-bounded builder. A worker may stream a completed candidate to
`rdo strategy draft --file -`; only a candidate that passes the exact
strategy-payload preflight is stored as mutable attempt-local
`runtime/STRATEGY_DRAFT.json`. `strategy preflight` never writes, and
`strategy submit|revise --draft` revalidates before creating the immutable
revision. The draft is transport state only: it is excluded from handoff
evidence and never changes coordinator approval semantics.

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
  "resource_budget": {
    "max_model_turns": 60,
    "max_input_tokens": 500000,
    "max_output_tokens": 50000,
    "max_cost_usd": 5.0,
    "max_context_tokens": 200000,
    "first_workflow_start_seconds": 180,
    "max_no_progress_turns": 12
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
      "resume": {
        "from_attempt": "A002-claude-ab12cd",
        "from_workflow": "WF-previous-implementation",
        "mode": "reuse"
      },
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

`resource_budget` is optional. Every configured field is a hard limit. Dispatch rejects the attempt before worker launch when the selected backend and I/O mode cannot expose a required metric. Structured events are normalized to `runtime/USAGE.ndjson`; an exceeded limit writes a hard violation and terminates the worker with protocol exit 125. `first_workflow_start_seconds` and `max_no_progress_turns` measure protocol progress in workflow, command, and completion records, not free-form narration. No implicit model/cost defaults are added to existing strategies.

`resume` is optional and allowed only on revision 2 or later. Its mode is:

- `reuse`: the source output satisfies the target workflow; dispatch writes `workflow_carried_forward`.
- `revalidate`: the source output is useful context, but the target workflow remains pending and must run its checks.

The coordinator approves the explicit source-to-target mapping. At dispatch, RDO requires the source attempt to be terminal, the source workflow to be completed or previously carried forward, and the source `worktree-after` digest to equal the new attempt's `worktree-before` digest. A mismatch fails before worker launch. RDO then writes derived `runtime/RESUME_CONTEXT.json`; workers must execute only `remaining_workflows`.

Acceptance command records are deliberately attempt-local. A strategy whose required workflows are all `reuse` is rejected when `acceptance_commands_pass=true`; at least one required workflow must remain or use `revalidate` to produce current acceptance evidence.

An independent review workflow must declare `kind: "review"` and `review: {"mode": "independent", "required_reviewers": N}`. It must use read-only `native_subagents` with `max_agents >= N`. Each reviewer writes a non-empty artifact under `runtime/reviews/`; completion supplies `--review-evidence REVIEWER_ID=ARTIFACT_PATH`. RDO accepts completion only when the reviewer IDs are distinct and appear in backend lifecycle events. A primary worker cannot declare its own scan to be independent review.

Non-acceptance workflow commands run through `rdo exec` are bounded and
audited as workflow activity. Required acceptance commands run only through
`rdo check --check-id <id>`, which selects the exact argv, cwd, and timeout from
the frozen `ACCEPTANCE.md` contract and writes attempt-local structured command
records. Legacy `rdo exec --acceptance` records cannot satisfy a v2 completion
gate; exploratory command failures likewise do not become acceptance evidence.

Completion gates are enforced at the earliest deterministic boundary. Legacy
execution validates acceptance records before appending the last
`workflow_completed`. Artifact Protocol v2 instead validates workflow and
timeout policy at that boundary, freezes the source, and validates source-bound
acceptance records at final handoff. A failed workflow gate leaves the instance
active so the worker may repair it without consuming another `max_instances`
slot.

Direct/Delegated explicitly enter finalization once implementation,
ordinary tests, and self-review remediation are complete. Full enters after
the final required workflow completes; all implementation and remediation must
therefore precede that completion. RDO freezes the full semantic worktree
entries and publishes create-once
`runtime/FINALIZATION.json`; later begin calls cannot reset its deadline.
The effective final deadline is the original execution deadline plus the
configured grace. During finalize-only time the worker may record or repeat
exact required checks, commit the frozen tree, and call `rdo finalize`, but may
not run workflows, `rdo exec`, or change source bytes, paths, kinds, symlink
targets, or modes. Check records carry before/after source digests and only
records matching the frozen snapshot qualify. Finalize binds the entry
snapshot, deadline, and marker into `EVIDENCE.json`, then publishes `HANDOFF.json` and
`runtime/HANDOFF_READY.json`. Legacy-v1 retains its historical compatibility
path.

## Multiple Workflows

A workflow definition is approved strategy. A workflow instance is runtime activity such as `WF-review-I002`. Workers may create new instances inside an approved definition without another review when instance, concurrency, permission, path, and budget limits remain satisfied.

A new strategy revision is required before introducing a new workflow kind, increasing budget or concurrency, granting write access, widening paths, or starting an exhaustive/unknown-size search. The execution worker writes a checkpoint and a revision request, then exits; dispatch applies `running -> strategy_review` only after validating the new revision.

## Strategy Review

`REVIEW-vNNN.json` contains `strategy_id`, `strategy_sha256`, `decision`, `reviewer`, `reviewed_at`, and `notes`. Decisions are `approved` or `changes_requested`. The coordinator never edits worker strategy in place.

An approval applies atomically to the complete strategy revision. Changes requested cause a new planning attempt and a new immutable revision.

When the latest strategy review decision is `changes_requested`, dispatch binds
the review to its strategy digest and includes the reviewer notes in the next
planning prompt. A revision worker must not be expected to discover rejected
strategy feedback by scanning protocol files without that explicit prompt
context.
