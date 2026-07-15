# Review Rubric

Use this rubric before the coordinator changes a Delegated or Full task from
`review` to `approved`, `changes_requested`, or `failed`. Direct tasks do not
receive this independent code-review transition; their worker self-review is
validated at `verified`, followed by the same coordinator-owned merge gate.

## Resolve the reviewed object

1. Confirm `STATUS.artifact_protocol_version = 2`, the profile/state are legal,
   and `current_attempt_id` identifies the attempt being reviewed.
2. Resolve that attempt only. Validate `ATTEMPT.json` and its exact
   `TASK_INPUTS.json` ref/digest, then validate the complete
   `EVIDENCE.json`/`HANDOFF.json`/`runtime/HANDOFF_READY.json` publication
   closure. Do not fall back to task-root handoff or evidence files.
3. Recompute the four frozen task-input digests and confirm the resolved
   dependency commits and task base commit still match `TASK_INPUTS.json`.
4. Confirm the handoff/evidence source commit is an exact Git commit, equals the
   task worktree HEAD, and the task worktree is clean.

## Review implementation and evidence

1. Review the exact `task_base_commit..source_commit` diff, including file
   modes and renamed/deleted files.
2. Verify changed paths stay within `EXECUTION_POLICY.json.allowed_paths`, avoid
   `forbidden_paths`, and satisfy the task's invariants and non-goals.
3. For every `ACCEPTANCE.md.required_commands` entry, require a matching passing
   `rdo check` record from this attempt with identical acceptance-contract
   digest, argv, cwd, timeout, exit result, and stable log digests.
4. Verify every required output is a tracked regular blob at the frozen source
   commit and that its path, Git mode, blob OID, and content digest binding all
   match; then inspect outputs relevant to the claimed behavior.
5. Perform the human `Behavioral Checks`, `Merge Preconditions`, and applicable
   `Pre-Merge Checks`. Structured command evidence does not replace semantic
   review.
6. Inspect selected logs, produced artifacts, reviewer evidence, and known
   limitations. Reject missing, stale, mutable, foreign-attempt, or
   digest-mismatched evidence.
7. Confirm no unresolved blocker, lock ambiguity, protocol warning, or
   post-finalize branch mutation remains.
8. Verify fast-forward mergeability into the target branch. The mechanical
   merge command will rerun canonical pre/post-merge commands from the frozen
   acceptance contract.

## Outcomes

- `approved`: all gates pass. The immutable review decision binds the exact
  clean source commit and current task-input/handoff/evidence/READY digests.
- `changes_requested`: implementation is salvageable without changing the
  frozen task contract; record concrete digest-bound findings for the next
  attempt.
- `failed`: the task should stop under its current contract.
- New revision task: scope, acceptance, profile, ownership, dependency, or
  design changed enough that v2 input drift would be required.

Do not approve plausible code without valid attempt-local evidence and
mergeability. Do not edit the reviewed task packet in place after dispatch.

## Legacy review

For a recognized legacy-v0.5/v1 task only, use its historical task-root
`EVIDENCE.md`, `HANDOFF.md`, `HANDOFF.json`, and status evidence summary through
the legacy resolver. Never mix those files into a v2 review.
