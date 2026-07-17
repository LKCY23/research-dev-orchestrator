# Backend Governance And Attempt Compilation

Status: Claude Code, Codex, Kimi Code, and OpenCode baselines are implemented.
Backend contracts, project policy, strategy binding, pure profile compilation,
attempt-local settings, native-agent governance, profile integrity checks, and
handoff violation gates use adapter-specific enforcement.

## Purpose And Scope

RDO launches different agent CLIs whose configuration and control surfaces are
not equivalent. Backend governance therefore belongs to each backend adapter,
not to a fictional cross-backend agent policy.

This design governs only processes launched by RDO dispatch:

```text
RDO dispatch
  -> worker CLI
       -> backend-native subagents or agent team members
       -> RDO-managed subprocesses
```

It does not modify user-global CLI settings and does not govern the foreground
coordinator session in which the RDO skill is being used.

## Lifecycle Boundaries

The design separates durable policy from task intent and attempt-local settings.

| Layer | Artifact | Lifetime | Ownership |
| --- | --- | --- | --- |
| Backend contract | `agent_backends/<id>.toml` | installed RDO version | RDO maintainers |
| Project backend policy | `.agent-collab/rdo.toml` | project/run configuration | project coordinator |
| Task limits | `EXECUTION_POLICY.json` | one task | task creator/coordinator |
| Approved execution intent | `STRATEGY-vNNN.json` and review digest | one strategy revision | worker proposes, coordinator approves |
| Compiled backend profile | `attempts/<id>/runtime/BACKEND_PROFILE.json` | one attempt | dispatch |
| Compiled read policy | `attempts/<id>/runtime/READ_POLICY.json` | one attempt | dispatch |
| Native CLI settings | attempt-local settings, environment, hooks, and argv | one attempt process | backend adapter |
| Runtime facts | supervisor, workflow, command, usage, and backend event logs | one attempt | supervisor and backend monitor |

No attempt artifact becomes a new policy source. It is an immutable or
append-only record of how durable policy and approved task intent were compiled
for one process launch.

Attempt-local settings do not imply a fresh logical worker. Dispatch preserves a stable `worker_id` and resumes the backend's native session across ordinary attempts. Backend or worker replacement is explicit, starts a new session, and records lineage plus a reason.

## Existing Mechanisms

The current implementation already provides:

- project operational defaults in `.agent-collab/rdo.toml`;
- backend command templates and permission-mode mappings;
- task-level `EXECUTION_POLICY.json` limits;
- immutable, coordinator-reviewed strategy revisions;
- attempt-local process-group timeout and cleanup;
- workflow and bounded-command APIs;
- planning worktree immutability and execution path post-validation.

All four worker adapters compile this boundary. Strategy schema v2
binds an approved strategy to one backend, and dispatch compiles its policy
before lock acquisition or task mutation. Unsupported controls remain a
dispatch-time error rather than a silent fallback.

## Backend Contract

Each backend definition has two separate sections.

It also declares `usage_observability.machine` and `usage_observability.human`: the exact normalized metrics that adapter can extract from structured events. This is a capability contract, not a best-effort hint. A strategy hard budget that names an undeclared metric is rejected before dispatch.

The contract describes public adapter output, not private backend session files. For example, Codex machine mode declares only terminal `input_tokens` and `output_tokens`; one `turn.completed` event is not treated as a count of the backend's internal model calls, and token totals are not reinterpreted as context-window occupancy.

### Capabilities

Capabilities describe facts about the installed adapter. They do not grant
permission and do not contain project policy.

```toml
[capabilities]
session_settings = true
pre_tool_hooks = true
tool_stream_events = true
native_subagents = true
agent_teams = true
process_level_native_agent_control = false
```

Capability names and backend-specific validation may differ between adapters.
RDO must not add placeholder capabilities merely to make backend files look
uniform. Context access uses a thin, versioned backend adapter rather than
duplicating native tool facts as booleans. The compiled profile records
the selected adapter, its version, enforced tools, known gaps, and one of these
enforcement levels:

| Backend | Attempt-local interception | Declared level |
| --- | --- | --- |
| Claude Code | `PreToolUse` for `Read`, `Grep`, and `Glob` | `tool_blocking` |
| Kimi Code | `PreToolUse` for `Read`, `Grep`, and `Glob` | `fail_open_tool_blocking` |
| OpenCode | `tool.execute.before` plugin for `read`, `grep`, and `glob` | `tool_blocking` |
| Codex | `PreToolUse` classification of common Bash reads/searches | `best_effort` |

These labels are intentionally not flattened into a claim that every backend
has the same enforcement strength.

The levels describe interception of the listed native read/search tools only.
Even `tool_blocking` is not arbitrary filesystem mediation: shell commands,
Python scripts, alternate tools, backend bugs, or unsupported native surfaces
may bypass it. Context governance is a deterministic efficiency,
discovery-shaping, and audit guardrail; it is not a confidentiality boundary
or hostile sandbox. Security isolation must come from the backend sandbox or
operating system.

## Context Access

`EXECUTION_POLICY.json.read_paths` is independent of writable
`allowed_paths`. New tasks default it to the write scope for a narrow starting
surface; existing policies without the field retain `.` for compatibility.
`CONTEXT.md` is a frozen decision capsule. Explicit on-demand source exceptions
come only from `EXECUTION_POLICY.json.context_sources`; Markdown formatting is
never interpreted as access policy.

The attempt-local `READ_POLICY.json` is generated, not hand-maintained. The
Context Broker uses `rg` for bounded search and a deterministic Markdown heading
parser for section retrieval. Results include source digests and request
metadata. It does not call an LLM or a document-extraction agent.

Supported native read/search adapters also append normalized, content-free
request facts to attempt-local `CONTEXT_ACCESS.ndjson`. Each record identifies
the backend, operation, path or search scope, requested bounds, allow/deny
decision, source size when known, and the adapter's telemetry coverage. These
records describe intercepted tool requests, not operating-system reads: Claude
Code, Kimi Code, and OpenCode report native-tool coverage, while Codex remains
best-effort. Context Broker requests remain separately auditable in
`CONTEXT_REQUESTS.ndjson`.

Materialization initializes the access log with a sentinel so consumers can
distinguish a genuine zero-request attempt from missing telemetry. Access-log
append failures are diagnostic only and do not replace the allow/deny decision.
Paths outside the assigned worktree are recorded as `outside_worktree`, not as
host-absolute paths. Broker audit records do retain the submitted search query
or section question and therefore follow the same retention boundary as other
attempt artifacts.

The first policy version is deliberately stateless: it rejects other
worktrees, forbidden/out-of-scope paths, and unbounded reads of large Markdown
outside the write scope. It has no cumulative byte budget, counter, or lock.
The same evaluator is used by native hooks/plugins and the CLI Broker. Backend
adapters only normalize tool names and arguments; policy semantics remain in
one deterministic Python module.

### Shipped Governance Defaults

Governance defaults describe how RDO invokes this backend. They are not settings
for the user's normal CLI sessions.

```toml
[governance]
disabled_plugins = ["plugin-name@marketplace"]
enable_agent_teams = true
max_tool_use_concurrency = 4
enforce_spawn_limit = false
```

Only fields implemented by that backend adapter are legal. Unknown fields are
configuration errors, not ignored wishes.

## Project Backend Policy

Projects may tighten or select backend-specific governance in `rdo.toml`:

```toml
[backends."claude-code"]
disabled_plugins = ["additional-plugin@marketplace"]
enable_agent_teams = true
max_tool_use_concurrency = 3
enforce_spawn_limit = false

[backends.codex]
enable_multi_agent = true
max_agent_threads = 3
max_agent_depth = 1
enforce_spawn_limit = false
```

The backend adapter owns validation and merge semantics. Security restrictions
merge monotonically:

- deny lists use set union;
- maximum budgets use the lower value;
- a project may disable a capability enabled by default;
- a project may not claim a capability absent from the backend contract;
- ordinary dispatch flags may select a backend or permission mode but may not
  silently remove long-term backend restrictions.

This is intentionally different from ordinary operational defaults, where CLI
and environment overrides have higher precedence. Governance is a dispatch gate,
not a convenience default.

Claude Code always enforces the approved native-agent concurrency limit. The
cumulative Agent/Task launch limit is retained as an optional control and is
disabled by default. Set `enforce_spawn_limit = true` to enforce
`global_budget.max_subagents` as a cumulative launch cap. When disabled, the
hook still records launch counts for audit without rejecting sequential reuse.

## Task Policy And Strategy

`EXECUTION_POLICY.json` remains the task-level ceiling for workflow count,
runtime, paths, enumeration, and agent use. It says what the task may request;
it does not describe how a particular CLI enforces the request.

An execution strategy must bind itself to one backend:

```json
{
  "schema_version": 2,
  "backend_id": "claude-code",
  "strategy_id": "T001-S001",
  "task_id": "T001-name",
  "revision": 1,
  "workflows": []
}
```

Backend-specific workflow requirements live in a namespaced object validated by
the selected adapter:

```json
{
  "executor": {
    "mode": "native_subagents",
    "max_agents": 3,
    "max_parallel": 2,
    "backend_options": {
      "claude-code": {
        "coordination": "agent_team"
      }
    }
  }
}
```

Strategy review must reject:

- a backend different from the task's selected execution backend;
- a requested capability absent from the backend contract;
- values exceeding task or backend governance limits;
- a hard requirement for which the adapter offers only observation;
- backend options belonging to another adapter.

Changing backend after approval requires a new strategy revision. Dispatch must
never silently translate an approved native-agent workflow into another backend's
different mechanism.

## Attempt Compilation

Before lock acquisition or task-state mutation, dispatch performs a pure compile
step:

```text
backend contract
  + shipped backend governance
  + project backend policy
  + task execution policy
  + approved strategy
  + allowed one-off launch selections
  -> compiled backend profile
```

The compiler either returns a complete profile or fails closed. It must not
silently drop unsupported controls.

Example `BACKEND_PROFILE.json`:

```json
{
  "schema_version": 1,
  "backend_id": "claude-code",
  "backend_version": "2.1.185",
  "phase": "execution",
  "strategy_id": "T001-S001",
  "strategy_sha256": "...",
  "controls": [
    {
      "name": "disabled_plugins",
      "value": ["plugin-name@marketplace"],
      "enforcement": "backend_settings"
    },
    {
      "name": "max_native_agent_spawns",
      "value": 3,
      "enforcement": "rdo_hook"
    },
    {
      "name": "write_paths",
      "value": ["scripts/", "tests/"],
      "enforcement": "post_validated"
    }
  ],
  "generated_files": ["READ_POLICY.json", "claude-settings.json"],
  "environment": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  },
  "unsupported_requests": []
}
```

The profile digest is stored in `ATTEMPT.json`. The actual argv recorded in the
attempt must reference only attempt-local generated files by absolute path.
Secrets are never written into the profile; it records secret source names or
redacted values only.

## Enforcement Classes

Every compiled control declares how it is enforced:

| Class | Meaning |
| --- | --- |
| `backend_native` | Direct CLI flag or documented environment control |
| `backend_settings` | Attempt-local settings consumed by the backend |
| `rdo_hook` | A deterministic pre-action hook can reject the action |
| `rdo_supervisor` | RDO controls process lifetime and cleanup externally |
| `rdo_stream_supervisor` | RDO consumes a machine event stream and terminates the attempt on a hard violation |
| `post_validated` | The action cannot be prevented reliably but its result is checked before handoff |
| `observed` | RDO can record it but cannot guarantee a bound |

A strategy may mark a control as hard or advisory. A hard control may compile
only to `backend_native`, `backend_settings`, `rdo_hook`, `rdo_supervisor`,
`rdo_stream_supervisor`, or a specifically accepted `post_validated` check.
`observed` is never sufficient for a hard completion or safety requirement.

## Claude Code Adapter Target

The first implementation uses only documented Claude Code surfaces:

- an attempt-local `--settings` file for plugin state and hooks;
- environment variables for Agent Team enablement and supported concurrency;
- `PreToolUse` hooks to reject disallowed native-agent launches before they run;
- `SubagentStart` and `SubagentStop` hooks for lifecycle evidence;
- `stream-json` in machine mode for an additional audit trail;
- `--disable-slash-commands` for RDO worker invocations so bundled and user
  skills cannot inject unreviewed workflows or large implicit context;
- the existing process-group supervisor as the final deadline and cleanup
  boundary.

The adapter must distinguish native guarantees from approximations. For example,
Claude Code's tool-use concurrency setting is not documented as a precise total
subagent budget, so a total spawn limit requires an RDO hook and an atomic
attempt-local counter.

Plugin overrides do not provide an absolute guarantee against organization-level
managed settings. The current profile records this as an external limitation and
guarantees only that the generated attempt-local settings contain the requested
disable entries and remain unchanged through handoff. It does not claim that
ordinary settings can override an organization force-enable policy.

Disabling slash-command skills does not disable Claude Code's native `Task`
subagents or Agent Teams. The flag is scoped to RDO-launched workers and does
not change direct user-launched Claude Code sessions.

## Codex Adapter

The Codex implementation always uses documented per-invocation configuration.
The `codex exec --json` event stream is additionally required only when the
optional cumulative launch limit is enabled:

- `features.multi_agent=false` when an execution strategy does not declare a
  `native_subagents` workflow;
- `features.multi_agent=true` only when native delegation was approved;
- `features.enable_fanout=false` and `features.multi_agent_v2=false` so batch or
  experimental agent launch paths cannot bypass per-spawn JSONL accounting;
- `agents.max_threads` for the native parallel thread limit;
- `agents.max_depth` for the native delegation-depth limit;
- `--strict-config` so unsupported injected settings fail at startup;
- optional `codex_stream_monitor.py` to consume `collab_tool_call` events and
  enforce the total `spawn_agent` request budget when
  `enforce_spawn_limit=true`;
- the existing process-group supervisor as the final deadline and cleanup
  boundary.

By default, RDO does not limit cumulative Codex subagent launches. Native
`agents.max_threads` and `agents.max_depth` still enforce parallelism and nesting.
Codex does not currently expose a pre-spawn hook that can reliably reject every
native-agent launch, so explicitly enabling the cumulative limit activates the
stream monitor. A total-spawn excess is then a hard post-start violation: the
monitor records it, terminates the Codex child process group, exits `125`, and
causes handoff validation to fail.

The total-spawn guarantee applies to the stable `spawn_agent` path. RDO disables
`spawn_agents_on_csv` fanout and the experimental multi-agent v2 surface because
their event streams do not provide the per-agent accounting required by this
version of the monitor. A future adapter may re-enable them only after adding a
verified event contract and tests for every launched child.

Codex native subagents support both `plain + machine` and `tmux + human` when the
cumulative limit is disabled. Enabling the hard cumulative control requires
`plain + machine`, because `tmux + human` does not expose the JSONL stream;
command rendering then fails before launch for human IO.

Planning may use Codex native subagents when project policy permits them, but
planning remains available as a single-agent attempt when project policy turns
multi-agent off. Execution is stricter: a strategy that declares
`native_subagents` fails compilation when multi-agent is disabled or
`max_agent_depth=0`.

All Codex settings are passed as one-invocation `-c` overrides. RDO does not
write `~/.codex/config.toml`.

RDO also injects one attempt-scoped `PreToolUse` hook for `Bash`. It recognizes
common direct reads (`cat`, `head`, `tail`, bounded `sed`), searches
(`rg`, `grep`), and listings (`find`, `ls`), then evaluates their resolved paths
against `READ_POLICY.json`. RDO worker launches use
`--dangerously-bypass-hook-trust` because dispatch already generated and bound
the exact hook command; the flag bypasses hook-definition trust, not command
approvals or sandbox policy. This
adapter is recorded as `best_effort`: complex shell
syntax, indirect Python/shell scripts, and native file tools that do not expose
a stable hook payload can bypass its classifier. It is an efficiency guardrail,
not a filesystem security boundary.

Codex `plain + machine` worker invocations use `codex exec
--ignore-user-config` and all Codex worker invocations set
`skills.include_instructions=false` for the attempt. Authentication is still
read from `CODEX_HOME`; in machine mode, user-level model choices, plugins, MCP
servers, and skills cannot delay startup or recursively activate inside an RDO
worker. RDO's permission and governance settings are supplied explicitly on
argv. The user's normal Codex sessions and configuration remain unchanged.

Older Codex releases do not expose `--ignore-user-config` on the interactive
top-level command used by `tmux + human`. Human mode therefore omits skill
instructions but otherwise remains explicitly best-effort with respect to user
configuration isolation.

## Kimi Code Adapter

Kimi Code uses a temporary `KIMI_CODE_HOME` for each attempt. The launcher copies
the user's base configuration, authentication, Skills, MCP, plugin, instruction,
and managed-tool assets, applies
the approved background-task limit, and appends attempt-local permission and
hook rules. It never edits `~/.kimi-code`.

- `Agent` is allowed only when the approved strategy declares native subagents;
- `AgentSwarm` is independently enabled or denied by backend policy;
- `KIMI_CODE_AGENT_SWARM_MAX_CONCURRENCY` bounds swarm fanout;
- `background.max_running_tasks` bounds background tasks;
- `PreToolUse` and subagent lifecycle hooks audit foreground concurrency;
- a separate `Read|Grep|Glob` `PreToolUse` hook evaluates the common read policy;
- Kimi's native subagents have depth one, recorded as a backend-native limit.

Kimi documents hooks as fail-open on crash or timeout. RDO therefore does not
claim that foreground hook admission is an absolute security boundary. A hook
failure or post-start excess is a hard violation that invalidates handoff. The
native swarm/background controls remain independent of hook execution. Context
interception carries the same fail-open qualification and is recorded as
`fail_open_tool_blocking` rather than absolute enforcement.

## OpenCode Adapter

OpenCode does not expose a verified native parallel-subagent limit. RDO therefore
starts one local `opencode serve` process per attempt and runs a permission
guardian against its SSE/API surface:

- root `task` calls are configured as `ask` and are approved only for allowed
  subagent types while capacity remains;
- approved child agent configurations receive `task=deny`, enforcing depth one;
- session creation must consume a matching permission reservation;
- unapproved, nested, or foreign child sessions are aborted and recorded as hard
  violations;
- child idle events release capacity, so sequential subagent reuse is unlimited;
- machine mode emits server events; human mode attaches the TUI while the same
  guardian remains active.

The local server is bound to `127.0.0.1` with a random per-attempt password and is
terminated with the attempt process group. `OPENCODE_CONFIG_CONTENT` is applied
only to that server. Project policy may additionally request `--pure` to disable
external plugins.

When pure mode is off, dispatch also supplies an attempt-local OpenCode plugin
directory. Its `tool.execute.before` hook normalizes `read`, `grep`, and `glob`
arguments and calls the shared policy evaluator before execution. Adapter errors
fail closed. Pure mode cannot load the plugin, so the compiled profile downgrades
context access to the prompt-and-CLI advisory adapter instead of claiming hard
interception.

## Runtime Events And Violations

Backend-specific hooks and monitors append attempt-local events such as:

```text
backend_agent_requested
backend_agent_started
backend_agent_stopped
backend_control_denied
backend_governance_violation
```

Runtime events are facts, not policy. They reference the compiled profile digest
and, when applicable, the approved workflow and instance identifiers.

Violation handling is deterministic:

- a pre-action denial returns a concise reason to the worker and records an
  event;
- repeated or non-recoverable violations terminate the attempt and require a
  blocked handoff;
- a hard control discovered violated after execution invalidates the handoff;
- a missing or malformed generated settings file fails before worker launch;
- inability to observe an advisory control is reported as a warning, not
  rewritten as success.

## Planning And Execution Phases

No new role system is introduced. Current attempt phases remain:

```text
planning
execution
```

Both phases use backend governance. Their task permissions differ through the
existing phase contract:

- planning is read-only and submits a strategy revision;
- execution uses the approved strategy and may change only approved paths.

The foreground coordinator is outside this process-launch boundary and is not
given an attempt profile.

## Dispatch Sequence

The target sequence is:

1. Load and validate project operational configuration.
2. Resolve the selected backend and installed backend version.
3. Load backend capabilities, shipped governance, and project backend policy.
4. For execution, load the exact approved strategy and verify its backend ID.
5. Compile and validate the backend profile without mutating protocol state.
6. Verify every configured hard resource metric is observable in the selected I/O mode.
7. Atomically acquire the dispatch lock and create the attempt.
8. Atomically render attempt-local backend settings, hooks, environment, and
   command from the already validated profile.
9. Store the profile and digest in attempt metadata before worker launch.
10. Launch through the existing attempt supervisor.
11. Collect backend usage/events, worktree checks, evidence, and handoff.
12. Reject the handoff if a hard governance violation occurred.

## Implementation Status

### Implemented: Schema And Compilation

- extend backend validation with adapter-owned capabilities and governance;
- add backend-specific project configuration parsing;
- bind strategies to `backend_id`;
- implement a pure `compile_backend_profile` operation;
- record the profile digest in attempts.

### Implemented: Claude Code Adapter

- render attempt-local Claude settings and environment;
- pass `--settings` through the generated command rather than editing user
  configuration;
- validate generated JSON before dispatch;
- add native-agent lifecycle hooks and atomic counters;
- disable bundled and user skills while preserving native subagents and Agent
  Teams;
- preserve Agent Team use when approved.

### Implemented: Codex Adapter

- compile project limits and approved native-agent intent into Codex `-c`
  overrides;
- disable undeclared native delegation;
- enforce native concurrency and depth through Codex settings;
- consume `collab_tool_call` JSONL events and maintain attempt-local counters;
- terminate the Codex process group and invalidate handoff when the total spawn
  budget is exceeded;
- reject native-subagent strategies on the unobservable human/TUI path.

### Implemented: Runtime Gate Integration

- append backend runtime events;
- include governance violations in handoff validation and diagnostics;
- expose the compiled profile and current counters through `rdo status`;
- make machine and tmux modes preserve the same policy semantics.

### Remaining: Managed Policy Verification

- add a reliable Claude managed-settings effective-state probe if the CLI exposes
  one; until then, ordinary `--settings` overrides cannot defeat an
  organization-level force-enabled plugin.

## Acceptance Tests

At minimum, tests must prove:

- RDO invocation does not modify user-global backend configuration;
- project backend policy compiles into an attempt-local profile;
- invalid or unsupported backend fields fail before lock/state mutation;
- an approved strategy cannot execute with a different backend;
- profile digest and actual attempt command agree;
- generated Claude settings contain the configured plugin disable entries, stay
  unchanged through handoff, and are accepted by the real CLI;
- approved native subagents remain usable;
- native-agent concurrency limits are denied and recorded when exceeded;
- Codex execution without declared native subagents receives
  `features.multi_agent=false`;
- Codex native concurrency/depth overrides appear in the recorded attempt
  command;
- when Codex cumulative enforcement is enabled, malformed JSONL and excess
  `spawn_agent` requests terminate the worker, record a hard violation, and
  invalidate handoff;
- Kimi's generated TOML is accepted by `kimi doctor`, does not modify the user
  home, and rejects concurrent foreground launches when its hook is healthy;
- OpenCode task permissions reject disallowed type, depth, and concurrency while
  allowing unlimited sequential reuse;
- attempt timeout terminates the worker and descendants;
- planning writes and out-of-policy execution writes invalidate handoff;
- managed-setting conflicts are reported as an external limitation until Claude
  exposes a reliable non-interactive effective-settings probe.

## Non-Goals

- changing the user's normal Claude Code, Codex, OpenCode, or Kimi settings;
- imposing one common governance schema on all agent CLIs;
- governing the foreground coordinator process;
- promising process-level control over opaque backend-native agents;
- treating prompt instructions as enforcement;
- silently falling back to a weaker execution mechanism.
