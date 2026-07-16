# Execution Profiles

RDO routes each task through the lightest profile that still gives the task an
appropriate verification boundary. Profile selection is explicit at task
creation, is stored in `STATUS.json`, and does not change within a task.

Choose a profile only after the work has been split to a coherent task. Full is
not the default for a large or vaguely complex task.

## Task Size Before Profile Selection

A task should contain:

- one primary trust boundary;
- one outcome that can be accepted independently; and
- one cohesive group of deliverables with a shared failure and remediation
  boundary.

A **primary trust boundary** is the main property whose failure would create a
distinct class of harm or invalid result, such as API compatibility, persistence
integrity, process-tree cleanup, filesystem isolation, permission enforcement,
or evaluator correctness. An **independent acceptance group** is a set of checks
that can prove that outcome without waiting for unrelated adjacent work.

Split the task when any of the following is true:

- parts can independently pass, fail, ship, roll back, or be remediated;
- parts enforce different permission, isolation, persistence, or platform
  boundaries;
- a platform-specific mechanism can be accepted separately from the portable
  runtime;
- one part can unblock downstream work without the others;
- acceptance naturally separates into unrelated test suites with different
  failure owners.

Do not split implementation, focused tests, and documentation merely because
they are different files when they all prove the same boundary. File count,
allowed-path count, estimated duration, or the word "complex" are not
deterministic task-size or profile signals.

## Profile Decision Table

Apply these questions in order:

| Question | Route |
| --- | --- |
| Does the proposed task cover more than one primary trust boundary or independently acceptable outcome? | Split it before selecting a profile. |
| Is the task high-risk, materially cross-module, experimental or multi-workflow, changing a permission/threat model, governed by hard resource budgets, or otherwise in need of coordinator-approved strategy before implementation? | Explicitly select `full`. |
| Is the implementation path bounded and the design already settled, but correctness still requires independent judgment, such as a contained fix to concurrency, a public API, or an existing security/Git/evidence boundary? | Select `delegated`. |
| Is the change local and low-risk, with objective acceptance checks and worker self-review sufficient for the code-verification boundary? | Select `direct`. |

If the choice is between Direct and Delegated, choose Delegated. Full is an
explicit judgment that the task is high-risk, materially cross-module, or needs
strategy approval; it is never inferred from generic complexity.

| Profile | Execution path | Verification owner |
| --- | --- | --- |
| `direct` | worker implements, tests, self-reviews, fixes findings, then requests `verified` | worker owns implementation review; coordinator checks protocol and merge mechanics |
| `delegated` | worker implements, tests, and self-reviews, then requests `review` | coordinator independently reviews implementation and evidence |
| `full` | worker proposes a read-only strategy, coordinator approves its exact digest, worker executes, then requests `review` | coordinator reviews both strategy and final implementation |

Profile selection is a routing decision, not a quality level. Every profile
keeps Git isolation, bounded supervision, evidence, valid handoff, and merge
gates. Direct removes independent coordinator code review; it does not remove
worker testing or self-review.

`create_task.py` requires `--profile direct|delegated|full`. The coordinator
intent surface likewise requires `profile=...`. Full must be selected with the
literal value `full`; RDO does not infer it from task prose, file counts,
estimated time, or a generic complexity score.

For Artifact Protocol v2, readiness and status audit also require exactly one
`task_created` event whose profile matches `STATUS.profile`. Changing that
binding requires a revision task.

## Examples

Good profile choices:

- **Direct**: add one validation rule to an existing private parser and cover it
  with focused unit tests.
- **Delegated**: repair exact merge-commit binding or descendant process cleanup
  where the implementation is bounded but the trust property needs independent
  review.
- **Full**: introduce a new sandbox permission model, destructive migration, or
  cross-backend governance mechanism whose high-risk/cross-module strategy must
  be reviewed before code changes.

Bad routing and decomposition:

- choosing Full because a change touches twelve files;
- using Direct for a small security or Git/evidence-boundary change solely
  because the diff is short;
- putting process supervision, workspace isolation, and platform sandboxing
  into one task and treating Full as permission to keep the epic intact.

A T005A2-style task spanning runtime invocation, workspace/environment
isolation, descendant cleanup, and Darwin sandbox enforcement should be split
at least as follows:

1. **Runtime invocation and process supervision**: command launch, deadlines,
   process groups, descendant cleanup, exit results, and their focused tests.
2. **Workspace and environment isolation**: working directory, environment
   filtering, path/symlink traversal, cleanup, and their focused tests.
3. **Darwin sandbox enforcement**: sandbox profile generation, platform
   invocation, deny rules, fail-closed behavior, and Darwin-specific mechanism
   tests.

The runtime-supervision and workspace-isolation tasks may be Delegated when
their contracts and mechanisms are already settled. A new Darwin sandbox
enforcement boundary is normally Full because it is permission-sensitive and
high-risk. Route each split task independently rather than inheriting the
profile of the original epic.

## Continuity and Escalation

Execution workers commit all task changes on the assigned task branch before
final handoff. `rdo finalize` requires a clean task worktree, freezes the exact
source commit, and derives changed paths from the attempt's frozen task-base
commit. The attempt-local before/after snapshots remain raw worktree facts.

Escalate `direct -> delegated -> full` by creating a revision task when scope or
acceptance materially changes. If uncertainty is discovered before substantive
execution, the coordinator may replace the task with a higher-profile task
while preserving the original audit trail. Profile escalation does not replace
task decomposition: split a newly discovered second trust boundary into another
task.

## Identity and Continuity

The protocol uses four distinct concepts:

- **Task**: the durable unit of intent, scope, acceptance, state, branch, and worktree.
- **Worker**: the logical execution owner assigned to the task. It should remain stable across ordinary feedback cycles.
- **Attempt**: one bounded, supervised execution slice with its own prompt, logs, runtime result, and handoff. A new attempt is an audit and supervision boundary, not automatically a new worker.
- **Backend session**: the native Claude Code, Codex, OpenCode, or Kimi conversation/session used by that worker.

An attempt records `worker_id`, `parent_attempt_id`, and `execution_mode`:

- `start`: first attempt for the logical worker; create a native backend session.
- `resume`: another bounded attempt for the same worker, resuming its native session and worktree.
- `replace`: a deliberate worker/backend replacement; preserve the previous lineage and record the reason.

Ordinary coordinator feedback uses `changes_requested -> running`, creates a new attempt, and resumes the same worker/session. This preserves context while keeping timeouts, logs, and handoffs independently auditable. Return to planning only when the strategy is invalidated by a scope, design, backend, workflow-kind, permission, or budget change.

When a Full revision changes backend or strategy shape, native session resume may be impossible while work resume remains valid. The revision explicitly maps source workflows to target workflows with `reuse` or `revalidate`; dispatch verifies exact worktree continuity before honoring the mapping.

Session reuse is best effort only when a backend cannot expose a native session identifier. The supported built-in backends use their native resume mechanism when a session ID is available.

## Artifact Boundary

Artifact Protocol v2 separates canonical task inputs from attempt-owned
outputs:

- `TASK.md`: Objective, Deliverables, Invariants, Non-goals, Dependencies.
- `CONTEXT.md`: non-normative frozen decisions, interfaces, code map, and
  necessary background.
- `ACCEPTANCE.md`: the only canonical acceptance commands, outputs, and human
  merge/blocking criteria.
- `EXECUTION_POLICY.json`: path boundaries, context sources, and execution
  limits.
- `STATUS.json`: coordinator-owned task state, profile, and worker assignment.
- `TASK_INPUTS.json`: immutable attempt-local binding to all four task inputs,
  the task base, and resolved dependencies.
- `ATTEMPT.json`: one execution slice and the exact task-input binding.
- `EVIDENCE.json`: frozen attempt-local review index over raw command, log,
  reviewer, commit, and worktree facts.
- `HANDOFF.json`: minimal attempt-local transition request, including Direct
  self-review when applicable.
- `HANDOFF_READY.json`: final supervisor publication marker, never approval or
  completion state.

Direct reaches `verified` after worker-owned self-review and recorded acceptance commands. Delegated reaches `review` and requires an explicit coordinator decision before `approved`. Direct `verified` and Delegated/Full `approved` both require coordinator-owned merge mechanics before `merged`.

All four task inputs are required. V2 has no task-root `HANDOFF.md`,
`HANDOFF.json`, or `EVIDENCE.md`; summaries and dashboards render derived views
without creating another protocol truth source. Historical runs retain those
files only through the explicit legacy decoder.
