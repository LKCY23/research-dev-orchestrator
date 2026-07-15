# Future Work

This file records deliberately deferred improvements. Entries are not current implementation commitments.

## FW-001: Context Broker MCP Transport

**Summary:** Defer exposing the deterministic Context Broker as an attempt-local MCP server; reconsider it only if real Codex runs frequently bypass the CLI Broker or require a backend-independent, enforceable bounded-document access path.

### Current Decision

- Keep the Context Broker as a CLI tool.
- Use native `PreToolUse` or plugin adapters where the backend exposes reliable file-tool interception.
- Treat Codex context-access guidance as an efficiency mechanism rather than a hard security boundary.
- Do not add MCP server lifecycle, registration, recovery, or cross-backend testing in the current iteration.

### Reconsider When

- Codex workers repeatedly read large indexed documents directly instead of using the CLI Broker.
- A task requires raw source documents to be inaccessible while bounded sections remain available.
- Maintaining backend-specific context tool adapters becomes more expensive than one shared MCP transport.
- All supported backends have a tested attempt-local MCP configuration and session-resume contract.

### Possible Scope

- Expose deterministic `context_index`, `context_search`, and `context_get` tools.
- Continue using the existing read policy, Markdown heading parser, output limits, source digests, and request records.
- Keep retrieval model-free; MCP would be a transport layer, not an LLM-based context service.
- Define startup, shutdown, authentication, attempt isolation, and failure behavior before implementation.

## FW-002: Hostile Process Containment

**Summary:** The current supervisors clean the worker process group,
discoverable descendants, and processes retaining the inherited supervision
token. They do not claim containment against an intentionally escaping
process.

### Current Boundary

- Process-group escalation and descendant/token scans cover normal CLI tools,
  acceptance checks, and cooperative subprocesses.
- A process may deliberately create a new session and strip identifying
  environment before it is observed; userspace scanning cannot make that case
  impossible.
- Artifact Protocol v2 phase 1 therefore treats surviving-process cleanup as a
  deterministic protocol guard, not a hostile security sandbox.

### Reconsider When

- Tasks execute untrusted code whose process behavior is adversarial.
- A supported platform provides an attempt-local cgroup, macOS sandbox,
  Windows job object, container, or equivalent lifecycle boundary.
- RDO can test containment, teardown, crash recovery, and interactive TUI
  compatibility across every supported runtime backend.

Only an operating-system containment boundary should upgrade the current
cooperative cleanup claim into a hard no-escape guarantee.
