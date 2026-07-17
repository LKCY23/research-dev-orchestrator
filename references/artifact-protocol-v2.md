# Artifact Protocol v2

Artifact Protocol v2 gives every task input and attempt output one owner, one
lifecycle, and one source-of-truth role. It does not change the Direct,
Delegated, or Full profile authority model or FSM.

## Version boundary

New tasks declare `artifact_protocol_version: 2` in task status, and every new
JSON artifact declares its own `schema_version`. A v2 reader must reject an
unknown version; it must never guess that an unversioned artifact is v2.

Existing runs remain byte-for-byte unchanged.
`STATUS.artifact_protocol_version = 1` selects the explicit `legacy-v1`
decoder. A recognized historical missing/v0.5 discriminator selects
`legacy-v0.5`. Neither decoder is a fallback for an unknown version or an
invalid v2 bundle. Legacy artifacts remain readable and auditable, but cannot
satisfy a v2 gate and are never migrated or copied into v2 truth.

## Task input contract

The coordinator owns four canonical task-root inputs:

| Artifact | Sole responsibility | Lifecycle |
| --- | --- | --- |
| `TASK.md` | Objective, Deliverables, Invariants, Non-goals, Dependencies | Authored before first dispatch; immutable for the task revision |
| `CONTEXT.md` | Non-normative frozen decisions, required-interface description, local code map, necessary background | Authored before first dispatch; cannot create obligations |
| `ACCEPTANCE.md` | Executable checks, required outputs, behavioral checks, merge conditions, blocked conditions, pre/post-merge checks | Authored before first dispatch; canonical acceptance source |
| `EXECUTION_POLICY.json` | Profile-independent execution limits, path boundaries, and explicit context sources | Authored before first dispatch; canonical execution-policy source |

`TASK.md` has exactly these level-two sections: `Objective`, `Deliverables`,
`Invariants`, `Non-goals`, and `Dependencies`. Its only machine block is a
fenced block whose info string is `json rdo-task-dependencies`:

```json
{
  "schema_version": 2,
  "dependencies": [
    {"task_id": "T001-contracts", "required_state": "merged"}
  ]
}
```

An empty dependency set is `"dependencies": []`. Prose is not a dependency.
At readiness time, every entry must identify an existing task in the required
state, and the resolver freezes its exact merged commit. A v2 dependency in
state `merged` additionally requires a matching `task_merged` event whose
`verification.passed` is true; a failed post-merge verification resolves as
`merged_unverified` and cannot satisfy the dependency.

`CONTEXT.md` has exactly four level-two sections: `Frozen Decisions`,
`Required Interfaces`, `Local Code Map`, and `Necessary Background`. It has no
Source Index. Paths written in Markdown have no policy effect. The only context
sources available to context tooling are normalized repository-relative paths
listed in `EXECUTION_POLICY.json.context_sources`. If context conflicts with a
normative input, `TASK.md` or `ACCEPTANCE.md` wins.

`ACCEPTANCE.md` contains exactly one machine block whose info string is
`json rdo-acceptance-contract`. Its shape is:

```json
{
  "schema_version": 2,
  "required_commands": [
    {
      "id": "unit",
      "argv": ["python3", "-m", "unittest", "discover", "-s", "tests/unit"],
      "cwd": ".",
      "timeout_seconds": 300
    }
  ],
  "required_outputs": ["build/result.json"],
  "pre_merge_commands": [],
  "post_merge_commands": []
}
```

Command IDs are non-empty and unique across all three command lists. `argv` is
a non-empty string array, never a shell command string. `cwd` and every output
path are normalized worktree-relative paths without parent traversal;
`timeout_seconds` is a positive integer. The human sections `Behavioral
Checks`, `Merge Preconditions`, `Blocked Conditions`, `Pre-Merge Checks`, and
`Post-Merge Checks` contain reviewer judgment, but cannot substitute prose for
a command or output that belongs in the machine block. No second acceptance
file exists.

Policy schema 2 makes `allowed_paths`, `read_paths`, `forbidden_paths`, and
`context_sources` explicit. Readiness rejects absolute or traversing paths,
write paths outside the read boundary, allowed/read paths overlapping a
forbidden boundary, and context sources outside the effective read boundary.

### Readiness and input freezing

Dispatch validates all four inputs before creating or launching a worker. It
rejects missing files or sections, duplicate or malformed machine blocks,
`RDO_TEMPLATE_INCOMPLETE` markers, invalid commands or paths, unsatisfied
dependencies, conflicting path boundaries, and a task with no executable
required command. Validation failure cannot allocate an attempt, acquire a
dispatch/task lock, create a worktree, or mutate the task into an execution
state.

For a ready task, dispatch writes the derived-only
`attempts/<attempt-id>/TASK_INPUTS.json`. It contains:

- protocol and schema version, task ID, and attempt ID;
- path and SHA-256 digest for each of the four canonical inputs;
- the task base commit;
- each resolved dependency task and exact merged commit;
- a stable contract digest over those input bindings.

The stable shape is:

```json
{
  "schema_version": 2,
  "artifact_protocol_version": 2,
  "task_id": "T002-implementation",
  "attempt_id": "A001-worker-ab12cd",
  "inputs": {
    "task": {"ref": "TASK.md", "sha256": "<sha256>"},
    "context": {"ref": "CONTEXT.md", "sha256": "<sha256>"},
    "acceptance": {"ref": "ACCEPTANCE.md", "sha256": "<sha256>"},
    "execution_policy": {"ref": "EXECUTION_POLICY.json", "sha256": "<sha256>"}
  },
  "task_base_commit": "0123456789abcdef0123456789abcdef01234567",
  "resolved_dependencies": [
    {
      "task_id": "T001-contracts",
      "required_state": "merged",
      "commit": "89abcdef0123456789abcdef0123456789abcdef"
    }
  ],
  "dependency_context": {
    "ref": "runtime/DEPENDENCY_CONTEXT.json",
    "sha256": "<sha256>"
  },
  "contract_sha256": "<sha256>"
}
```

The contract digest excludes attempt identity and is computed from canonical
input digests, task base commit, and sorted resolved dependency bindings.
When at least one merged dependency has a validated v2 merge bundle, the
optional `dependency_context` descriptor binds a derived, non-normative short
manifest. The descriptor is deliberately excluded from `contract_sha256`:
dependency commits are contractual, while their bounded context projection is
an implementation detail that older attempts may not contain.
The four `inputs.*.ref` values are task-root logical paths; only
`ATTEMPT.task_inputs_ref` is relative to the attempt directory.

`ATTEMPT.json` references `TASK_INPUTS.json` by attempt-relative path and exact
file SHA-256. It does not copy individual input digests. A later attempt may
create a new derived file only when its stable contract digest matches the
task's first frozen contract. Input drift blocks ordinary resume/retry and
requires a new revision task; editing the old task in place is not a revision.

## Attempt output contract

Every v2 attempt owns this complete artifact tree:

```text
attempts/<attempt-id>/
├── ATTEMPT.json
├── TASK_INPUTS.json
├── HANDOFF.json
├── EVIDENCE.json
└── runtime/
    ├── DEPENDENCY_CONTEXT.json
    ├── HANDOFF_READY.json
    ├── ARTIFACT_LOCK
    ├── COMMANDS.ndjson
    ├── check-broker/
    ├── transcript.log
    ├── worktree-before.json
    └── worktree-after.json
```

| Artifact | Truth role | Owner and lifecycle |
| --- | --- | --- |
| `ATTEMPT.json` | Canonical attempt identity/runtime metadata; input binding is only a `TASK_INPUTS.json` ref and digest | Dispatcher creates it; protocol code advances attempt metadata |
| `TASK_INPUTS.json` | Derived immutable snapshot of canonical task inputs and resolved commits | Dispatcher publishes it before launch |
| `runtime/DEPENDENCY_CONTEXT.json` | Optional short catalog binding merged predecessor bundles and Broker-visible fields; contains no full predecessor document, diff, or log | Dispatcher derives it from verified `task_merged` artifact bindings before prompt rendering; `TASK_INPUTS.json` binds its exact digest |
| `runtime/COMMANDS.ndjson` | Append-only raw supervised-command facts, including before/after semantic source digests | `rdo check` appends records before or during finalization |
| `runtime/check-broker/` | Ephemeral request, one-use supervision lease, and cleanup receipt transport; not evidence | Machine attempt supervisor creates one instance per launch and serves it only for that worker lifetime |
| `runtime/ARTIFACT_LOCK` | Internal process lock; not evidence | Shared by supervised command writers and held exclusively by finalization so no command can append after publication |
| `runtime/DEADLINE.json` | Create-once attempt execution deadline shared by backend resume fallback | Supervisor creates it before worker launch |
| `runtime/finalization-worktree.json` | Create-once full semantic source snapshot at finalize-only entry | RDO publishes it before the finalization marker |
| `runtime/FINALIZATION.json` | Create-once phase marker binding entry time, grace, task inputs, fixed final deadline, and source snapshot | RDO publishes it after the profile's source-freeze gate passes |
| `runtime/transcript.log` | Raw worker/supervisor log | Supervisor appends while the worker runs; it is not selected into the frozen evidence package before worker exit |
| `runtime/worktree-*.json` | Raw before/after worktree facts | Dispatcher/supervisor capture them at their defined boundaries |
| `EVIDENCE.json` | Frozen, structured index selecting raw facts for review; never a second command log | Finalizer derives and publishes it once |
| `HANDOFF.json` | Canonical worker request for one FSM transition | Worker supplies request fields; finalizer validates and publishes once |
| `runtime/HANDOFF_READY.json` | Derived atomic publication marker binding one handoff package | Finalizer writes it last; supervisor only validates it |

There is no task-root `HANDOFF.md`, `HANDOFF.json`, or `EVIDENCE.md` in v2,
and no preexisting-artifact copy step. A dashboard may render Markdown from the
attempt JSON at read time, but rendered text is never protocol truth.

### Structured checks and evidence

All profiles execute required commands through the same supervised interface:

```text
rdo check --attempt-dir <attempt-dir> --check-id <id>
```

The check ID selects an exact command from the frozen acceptance contract;
workers cannot satisfy it with free text. Every raw command record declares
`artifact_protocol_version = 2` and `schema_version = 2`, and binds
`record_id`, `task_id`, `attempt_id`, `task_inputs_sha256`,
`acceptance_contract_sha256`, `category = required_commands`, and `check_id`.
It records the exact argv, cwd, timeout, start/finish times, exit code, timeout
flag, elapsed time, surviving processes, and attempt-local stdout/stderr refs
plus SHA-256 digests. `record_sha256` covers the canonical record excluding
that self-declared digest. Optional workflow and instance fields bind the same
record to an active Full workflow instance. Missing, foreign-attempt,
digest-mismatched, or mutable-log records are invalid. Coordinator
pre/post-merge checks use the same supervisor against their respective command
lists.

`EVIDENCE.json` indexes those immutable records by ID/reference and record
digest, including exit code, timeout and elapsed time. It also indexes changed
paths, the exact source commit, worktree snapshot refs/digests, logs, produced
artifacts, and reviewer evidence. Raw process and filesystem facts remain in
`COMMANDS.ndjson`, logs, and snapshots. Finalization accepts only real matching
records from this attempt; text such as `pytest (109 passed)` is not evidence.

Its review-manifest shape is:

```json
{
  "schema_version": 2,
  "artifact_protocol_version": 2,
  "task_id": "T001-contracts",
  "attempt_id": "A001-worker-ab12cd",
  "frozen": true,
  "source_commit": "0123456789abcdef0123456789abcdef01234567",
  "command_records": [
    {
      "check_id": "unit",
      "record_id": "C001",
      "record_ref": "runtime/COMMANDS.ndjson",
      "record_sha256": "<sha256>",
      "acceptance_contract_sha256": "<sha256>",
      "category": "required_commands",
      "argv": ["python3", "-m", "unittest", "discover", "-s", "tests/unit"],
      "cwd": ".",
      "timeout_seconds": 300,
      "exit_code": 0,
      "elapsed_seconds": 12.5,
      "timed_out": false,
      "surviving_processes": [],
      "stdout_ref": "runtime/commands/C001.stdout.log",
      "stdout_sha256": "<sha256>",
      "stderr_ref": "runtime/commands/C001.stderr.log",
      "stderr_sha256": "<sha256>"
    }
  ],
  "changed_paths": ["scripts/example.py"],
  "required_outputs": [
    {
      "path": "build/result.json",
      "git_mode": "100644",
      "git_oid": "<40-or-64-character-lowercase-git-blob-oid>",
      "sha256": "<sha256-of-blob-bytes>"
    }
  ],
  "worktree": {
    "before": {"ref": "runtime/worktree-before.json", "sha256": "<sha256>"},
    "after": {"ref": "runtime/worktree-after.json", "sha256": "<sha256>"}
  },
  "logs": [
    {"ref": "runtime/commands/C001.stdout.log", "sha256": "<sha256>"},
    {"ref": "runtime/commands/C001.stderr.log", "sha256": "<sha256>"}
  ],
  "artifacts": [],
  "reviewer_evidence": []
}
```

Only stable logs may be selected into `EVIDENCE.json`. In particular,
`runtime/transcript.log` remains live until the worker and supervisor exit, so
worker finalization must not freeze its digest. The exact stdout/stderr logs
created by completed `rdo check` records are stable and are the normal evidence
log selection.

`required_outputs` is distinct from generic attempt-local artifacts. Every
required output must be a regular non-symlink file tracked by `source_commit`
as a `100644` or `100755` Git blob. Finalization binds its path, Git mode, blob
object ID, and SHA-256 of the blob bytes, and verifies that the live file bytes
and executable bit match that commit. Dispatch validation, review, approval,
and merge recompute this binding. An untracked or gitignored output cannot
satisfy the acceptance contract.

### Handoff and publication

`HANDOFF.json` contains only the transition request and its binding:

```json
{
  "schema_version": 2,
  "artifact_protocol_version": 2,
  "task_id": "T001-contracts",
  "attempt_id": "A001-worker-ab12cd",
  "requested_state": "verified",
  "summary": "Implemented and checked the contract.",
  "known_limitations": [],
  "conditional_blocker": null,
  "direct_self_review": {
    "performed": true,
    "passed": true,
    "summary": "No unresolved findings.",
    "findings": []
  },
  "source_commit": "0123456789abcdef0123456789abcdef01234567",
  "evidence_ref": "EVIDENCE.json",
  "evidence_sha256": "<sha256>"
}
```

The requested state must be legal for the active profile. Direct requires a
substantive self-review; Delegated and Full preserve their coordinator/reviewer
authority. The `direct_self_review` field is always present; profiles that do
not use it set `performed` to false and do not treat it as approval. A blocked
request uses `conditional_blocker` to state the concrete condition and
otherwise leaves it `null`. Commands and changed-file lists do not appear in
the handoff.

Finalization builds candidate evidence and handoff files, validates their
cross-references, writes each with no-overwrite semantics, and writes
`runtime/HANDOFF_READY.json` last. The marker binds:

- task and attempt IDs plus the `ATTEMPT.json`/`TASK_INPUTS.json` binding;
- requested state and exact source commit (including its digest);
- `HANDOFF.json` and `EVIDENCE.json` paths and SHA-256 digests.

The marker shape is:

```json
{
  "schema_version": 2,
  "artifact_protocol_version": 2,
  "publication": "handoff_ready",
  "published_at": "2026-07-16T12:00:00.123Z",
  "published_at_epoch": 1784203200.123,
  "task_id": "T001-contracts",
  "attempt_id": "A001-worker-ab12cd",
  "attempt_ref": "ATTEMPT.json",
  "attempt_binding_sha256": "<sha256-of-immutable-attempt-identity>",
  "task_inputs_ref": "TASK_INPUTS.json",
  "task_inputs_sha256": "<sha256>",
  "requested_state": "verified",
  "source_commit": "0123456789abcdef0123456789abcdef01234567",
  "source_commit_sha256": "<sha256-of-the-commit-string>",
  "handoff_ref": "HANDOFF.json",
  "handoff_sha256": "<sha256>",
  "evidence_ref": "EVIDENCE.json",
  "evidence_sha256": "<sha256>"
}
```

Publication is complete only when all bindings validate. Published handoff,
evidence, and marker files are immutable; a second finalization cannot replace
them. A crash before the marker leaves an unpublished but auditable partial
package. Recovery may finish publication only when existing candidate bytes
match the recomputed bindings; otherwise it reports the conflict without
overwriting evidence.

`published_at` and `published_at_epoch` are worker-supplied audit metadata, not
deadline proof. The supervisor performs a bounded same-descriptor marker read,
rejects concurrent mutation, records its own observation receipt and matching
Git source state, and requires that first observation to finish before the
active deadline. After the process tree is quiescent it validates the complete
bundle and creation-time closure of every bound dependency. For
`strategy_review`, that closure includes the canonical strategy revision
outside the attempt directory.

`HANDOFF_READY.json` is not task completion, approval, or an FSM transition.
The supervisor accepts only the marker located under the active attempt and
only after validating every bound digest, attempt ID, requested state, and
source commit. A stale or foreign marker cannot finish another attempt. The
dispatcher then applies the existing profile-specific transition separately.
That transition requires a readable, coherent supervisor result binding the
accepted marker identity, immutable deadline digest, source receipt, cleanup
proof, and zero supervisor exit; a worker cannot self-declare those facts by
calling the handoff validator.

Post-process validation is anchored to dispatcher-captured expectations rather
than values first derived from worker-mutable files: task/profile/phase/branch/
worktree identity, backend profile/settings/read-policy digests, the exact
Full strategy ID/revision/digest, `TASK_INPUTS.json` digest, task base commit,
and the pre-launch worktree snapshot digest. If dispatch stops after persisting
the completed `ATTEMPT.json` but before the `STATUS.json` transition, replay
revalidates the bundle and applies only the missing transition. Replay over an
already advanced coordinator state validates the exact dispatch transition,
coordinator-owned history suffix, immutable review decision, and matching
`task_merged` evidence as applicable. It never rolls the FSM backward; missing
provenance or artifact drift returns failure.

### Publication resolution

Monitoring distinguishes `unpublished`, `partial`, `published`, and
`rejected`. `rejected` is used when `STATUS.json` is blocked and the current
attempt is `invalid_handoff`: existing candidate files remain audit-only,
`bundle` is null, and monitoring may expose their refs with a warning. This
never converts invalid bytes into a publication. Strict review, approval,
and merge consumers still require a validated `published` bundle and reject
this state. Dependency readiness does not load the bundle, but a rejected
attempt is not a verified `merged` task/event and therefore cannot satisfy a
dependency.

## Reference graph and downstream binding

```text
TASK.md ───────────────┐
CONTEXT.md ────────────┤
ACCEPTANCE.md ─────────┼─> TASK_INPUTS.json <─digest/ref─ ATTEMPT.json
EXECUTION_POLICY.json ─┘           │
                                   ├─> rdo check ─> COMMANDS.ndjson
                                   │                    │
worktree snapshots / logs ─────────┴────────────────────┼─> EVIDENCE.json
                                                        │        │
                                                        └────────┴─> HANDOFF.json
                                                                     │
ATTEMPT + source commit + exact artifact digests ─────────────────────┴─> HANDOFF_READY.json
```

Review resolves the active attempt and validates the complete publication
package. Approval records digests for the exact `TASK_INPUTS.json`,
`HANDOFF.json`, `EVIDENCE.json`, and source commit. Merge revalidates those
same bytes and commit before changing Git state. Status collection and
dashboards resolve v2 attempt-local artifacts first and route recognized
legacy-v0.5/v1 runs through explicit legacy readers; neither consumer
recreates task-root truth. Every v2 merge records a verification object. When
`verification.passed` is false, Git truth remains `merged`, but dependency
resolution exposes `merged_unverified` and blocks downstream readiness until
the coordinator arranges an explicit repair or revision task.
