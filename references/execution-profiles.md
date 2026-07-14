# Execution Profiles

RDO routes each task through the lightest profile that still gives the task an appropriate verification boundary. The profile is stored in `STATUS.json` and does not change within a task.

## Profiles

| Profile | Use when | Execution path | Final verification owner |
| --- | --- | --- | --- |
| `direct` | Small, local, low-risk, and easy to verify | worker implements, tests, self-reviews, fixes findings, then requests `verified` | worker; coordinator checks protocol and merge mechanics |
| `delegated` | Moderate work that benefits from independent review but not strategy ceremony | worker implements, tests, and self-reviews, then requests `review` | coordinator |
| `full` | High-risk, ambiguous, experimental, multi-workflow, or budget-sensitive work | worker proposes a strategy, coordinator approves it, worker executes, then requests `review` | coordinator |

Profile selection is a routing decision, not a quality level. Every profile keeps Git isolation, bounded supervision, evidence, valid handoff, and merge gates. Direct removes independent coordinator code review; it does not remove worker testing or self-review.

Escalate `direct -> delegated -> full` by creating a revision task when scope or acceptance materially changes. If uncertainty is discovered before substantive execution, the coordinator may replace the task with a higher-profile task while preserving the original audit trail.

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

Authors should reason about a small set of canonical concerns even when compatibility files are materialized separately:

- `TASK.md`: human intent, scope, context, and acceptance contract.
- `STATUS.json`: coordinator-owned task state, profile, and worker assignment.
- `ATTEMPT.json`: one execution slice and its lineage/session metadata.
- `HANDOFF.json`: machine-readable worker completion request, including Direct self-review attestation.
- logs and evidence: execution facts used by the responsible reviewer.

`CONTEXT.md` and `ACCEPTANCE.md` are optional normalization files when their content is already complete in `TASK.md`. `HANDOFF.md`, `EVIDENCE.md`, summaries, and dashboards are human-readable or derived views; they must not introduce a second conflicting decision source.
