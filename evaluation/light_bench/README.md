# RDO light bench

This directory is a small regression lab for RDO worker execution. It is not a
leaderboard harness and does not reproduce SWE-bench, Harbor, or an LLM judge.
Every case is self-contained, uses only the Python standard library, and runs
against a disposable Git repository outside the RDO source tree.

RDO is the product under test. The runner calls its public initialization,
task-creation, dispatch, and status commands against the disposable repository;
it never uses RDO to modify the RDO repository itself.

## Cases

| Case | Purpose | Profiles |
| --- | --- | --- |
| `L01-located-fix` | Small, path-located retry correction and fixed overhead | Direct, Delegated |
| `L02-context-needle` | Localized fix with one relevant section in indexed Markdown larger than 16 KiB | Direct, Delegated |
| `L03-cross-file-feature` | Bounded model/queue feature spanning multiple files | Direct, Delegated, Full |

The common MiniQueue fixture has no solution history. The runner copies the
fixture, applies the case setup patch, and only then creates the first Git
commit. A worker therefore cannot recover the answer from a parent commit.
Visible tests and task acceptance are available to the worker; the external
verifier is deterministic but is not a hostile security boundary.

## Commands

Validate every manifest, task packet, setup patch, and initially failing
verifier without starting an agent:

```bash
python3 evaluation/light_bench/bench.py validate
```

List cases:

```bash
python3 evaluation/light_bench/bench.py list
```

Run one live worker. Machine/plain mode is fixed so structured attempt telemetry
is available:

```bash
python3 evaluation/light_bench/bench.py run \
  --case L02-context-needle \
  --profile direct \
  --backend claude-code \
  --rdo-root "$PWD" \
  --model-label '<exact configured model>' \
  --repeat 3 \
  --output /tmp/rdo-light-bench-candidate
```

Run a named matrix with the same backend:

```bash
python3 evaluation/light_bench/bench.py run \
  --preset profiles \
  --backend claude-code \
  --rdo-root "$PWD" \
  --repeat 1
```

Compare two result sets. Groups match only when case digest, profile, backend,
backend version, declared model label, recorded runtime model configuration, and
permission mode match. Unlabelled results are retained but are not comparable:

```bash
python3 evaluation/light_bench/bench.py compare \
  --baseline /tmp/rdo-light-bench-baseline \
  --candidate /tmp/rdo-light-bench-candidate \
  --output /tmp/rdo-light-bench-comparison.json
```

Comparison is strict by default: any unmatched group or unlabelled record exits
with status 2. `--allow-partial` is available only for deliberate exploratory
comparisons that retain at least one matched group; direct `ab` runs are always
strict. Strict comparison also requires equal sample counts on both sides.

Or run a paired A/B matrix directly. Repetitions alternate baseline/candidate
order, and the command writes both raw result sets plus `comparison.json` and
`comparison.md`:

```bash
python3 evaluation/light_bench/bench.py ab \
  --preset context \
  --backend claude-code \
  --baseline-rdo /path/to/clean-baseline-worktree \
  --candidate-rdo /path/to/clean-candidate-worktree \
  --model-label '<exact configured model>' \
  --repeat 4 \
  --output /tmp/rdo-light-bench-ab
```

Run and A/B output directories must be new or empty and must live outside every
measured RDO source tree. This prevents benchmark artifacts from changing the
candidate provenance or stale results from entering a later comparison.

Use separate clean Git worktrees and alternate baseline/candidate order for a
serious A/B run. Four balanced repetitions are the minimum useful live sample. The
runner records a dirty-source digest for exploratory runs; do not promote a
dirty run as a durable baseline.

`model_label` is the operator's exact configuration label. The separate
`configured_models` field comes from attempt runtime metadata; it is not claimed
to be a provider-attested response-model identity. Both are included in the
comparison key so conflicts become unmatched groups instead of silent mixing.

## Result boundary

Each result has independent sections for correctness, timing, normalized usage,
context access, protocol activity, and provenance. Comparison reports both
all-run resource medians and passed-run performance medians, including sample
counts. There is deliberately no weighted score.

Hard pass requires:

- the deterministic verifier passes;
- `collect_status.py` reports a valid protocol state;
- the profile reaches `verified` for Direct or `review` for Delegated/Full;
- changed paths stay within the case expectation;
- dispatch succeeds without a hard governance violation.

If outer-timeout cleanup fails or reports surviving worker PIDs, the current
sample is saved as failed and the whole run stops immediately; later samples
would no longer be isolated enough to compare.

`CONTEXT_ACCESS.ndjson` measures intercepted Read/Grep/Glob access checks, not
operating-system filesystem reads. Repeated-access metrics cover Read requests
only, because search patterns are intentionally not logged. Claude Code, Kimi
Code, and OpenCode report native-tool coverage; Codex is best-effort and one
shell tool call may produce multiple target checks. Broker calls are counted
separately from `CONTEXT_REQUESTS.ndjson`. Missing backend telemetry remains
null; declared, initialized, and observed states are reported separately. The
runner does not infer telemetry from TUI context size.

L02 does not require a Broker call to pass. It measures whether a worker uses
targeted search/bounded retrieval—or needlessly opens the large source—without
turning one preferred tool sequence into a correctness requirement.

Delegated and Full results currently measure the worker side only. The runner
does not pretend that a deterministic verifier is coordinator review. For Full,
the planning strategy is mechanically approved after protocol validation and
the result records `strategy_review_mode=protocol_auto_approve`. A future
reviewer lane should use labelled good/bad patches and report false approvals
and false rejections separately.

## CI policy

`list`, `validate`, and the runner unit tests are deterministic and safe for CI.
The fake-backend Direct/Delegated/Full lifecycle checks run from
`tests/smoke/test_light_bench.sh`, outside ordinary unit discovery.
Real backend runs are opt-in or scheduled; they should run sequentially to
avoid rate-limit and machine-contention noise. Performance deltas are warnings;
only correctness regression changes the `compare` exit status.
