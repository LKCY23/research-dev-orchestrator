# research-dev-orchestrator 设计基线

## 1. 定位

`research-dev-orchestrator` 是一个面向 research proposal、experiment design、实验代码实现、开源贡献和可复现实验的 Codex orchestration skill 设计。

它不是 server、RPC 或队列系统，而是：

```text
Codex coordinator + Claude Code workers
```

核心原则：

```text
Codex owns intent.
Claude Code owns execution.
Filesystem is the protocol.
Git is the isolation boundary.
FSM is a hard protocol.
```

第一版不做常驻服务、不依赖 `resume`、不自动 review、不引入数据库或消息队列。

## 2. 顶层流程

```text
需求澄清
  -> REQUIREMENTS.md

设计方法选择
  -> DESIGN_METHOD_SELECTION.md

系统/实验设计
  -> DESIGN_BRIEF.md
  -> ADR/*
  -> EXPERIMENT_PLAN.md
  -> REPRODUCIBILITY.md

任务拆分
  -> TASKS.md
  -> tasks/Txxx-*/

派发执行
  -> dispatch_claude.sh
  -> Claude Code worker
  -> branch/worktree/attempt

证据提交
  -> STATUS.json
  -> EVIDENCE.md
  -> HANDOFF.md
  -> logs/*

状态收集
  -> collect_status.py
  -> SUMMARY.md / JSON / terminal summary

Codex review
  -> reviews/*.md
  -> approved / changes_requested / failed

集成
  -> dry-run merge + integration smoke test
  -> approved
  -> merged

实验记录
  -> RESULT_LEDGER.md
```

## 3. 目录结构

```text
.agent-collab/
  runs/
    <run-id>/
      SUMMARY.md
      EVENTS.ndjson
      JOURNAL.md
      RUN.json
      REQUIREMENTS.md
      DESIGN_METHOD_SELECTION.md
      DESIGN_BRIEF.md
      ADR/
      EXPERIMENT_PLAN.md
      REPRODUCIBILITY.md
      RESULT_LEDGER.md
      TASKS.md
      diagnostics/
        collect-status-<timestamp>.json
        collect-status-<timestamp>.md
      tasks/
        T001-name/
          TASK.md
          CONTEXT.md
          ACCEPTANCE.md
          STATUS.json
          HANDOFF.md
          EVIDENCE.md
          LOCK  # human-readable ownership/audit metadata
          .dispatch-lock/  # present only while active dispatch/worker execution is held
          logs/
          attempts/
            A001-claude-x4p9a/
              ATTEMPT.json
              prompt.md
              transcript.log
              result.md
      reviews/
      final/
```

`LOCK` 是人类可读审计文件。`.dispatch-lock` 是原子执行互斥目录。`create_task.py` 两者都不创建。

## 4. Identity Contract

身份分层：

```text
run_id      = one orchestration run
task_id     = one work unit
attempt_id  = one execution attempt
session_id  = one agent runtime session
```

`run_id` 格式：

```text
<UTC timestamp>-<project-slug>-<shortid>
```

示例：

```text
20260703T120405Z-rag-benchmark-a7f3c2
```

规则：

```text
Do not encode coordinator/worker identity in run_id.
Do not encode session id in run_id.
Do not encode model name in run_id.
```

`attempt_id` 格式：

```text
A<seq>-<agent>-<shortid>
```

示例：

```text
A001-claude-x4p9a
A002-claude-k7fa9
A003-codex-p91bc
```

规则：

```text
Do not encode model name in attempt_id.
Do not encode session id in attempt_id.
Model/runtime metadata belongs in ATTEMPT.json and is optional.
```

## 5. RUN.json

```json
{
  "run_id": "20260703T120405Z-rag-benchmark-a7f3c2",
  "protocol_version": "research-dev-orchestrator/v0.1",
  "created_at": "2026-07-03T12:04:05Z",
  "project_slug": "rag-benchmark",
  "objective": "Implement reproducible RAG benchmark experiment pipeline",
  "target_branch": "main",
  "base_commit": "abc123def4567890abc123def4567890abc123de",
  "coordinator_sessions": [
    {
      "agent": "codex",
      "role": "coordinator",
      "session_id": "codex-session-001",
      "started_at": "2026-07-03T12:04:05Z"
    }
  ]
}
```

`base_commit` 应由脚本记录为 full SHA。文档中的短 SHA 只能作为说明性占位符使用。

## 6. FSM 协议

FSM 是协议级硬约束，必须同时提供：

```text
references/state-machine.md
references/state-machine.json
```

脚本只信 `state-machine.json`。人读解释放在 `state-machine.md`。

机器读版本：

```json
{
  "states": [
    "pending",
    "running",
    "blocked",
    "review",
    "changes_requested",
    "approved",
    "merged",
    "failed"
  ],
  "terminal_states": ["merged", "failed"],
  "transitions": {
    "pending": {
      "running": ["dispatch"]
    },
    "running": {
      "review": ["claude-code"],
      "blocked": ["claude-code"]
    },
    "blocked": {
      "running": ["dispatch"],
      "failed": ["codex"]
    },
    "review": {
      "approved": ["codex"],
      "changes_requested": ["codex"],
      "failed": ["codex"]
    },
    "changes_requested": {
      "running": ["dispatch"]
    },
    "approved": {
      "merged": ["codex"]
    },
    "merged": {},
    "failed": {}
  }
}
```

权限边界：

```text
create_task.py:
  only creates pending

dispatch_claude.sh:
  pending -> running
  blocked -> running
  changes_requested -> running

Claude Code:
  running -> review
  running -> blocked

collect_status.py:
  read-only validation
  no mutation

Codex review:
  review -> approved
  review -> changes_requested
  review -> failed
  blocked -> failed
  approved -> merged
```

Claude Code 不能写：

```text
approved
merged
failed
changes_requested
```

如果 Claude Code 认为不可恢复，也只能写：

```text
blocked
```

并填：

```text
blocker_type = irrecoverable
blocking_reason = ...
```

由 Codex 决定是否进入 `failed`。

## 7. 状态语义

`pending`：

```text
Task packet exists but no worker has started.
```

`running`：

```text
A dispatch/worker owns execution and is actively working in a task worktree.
```

`blocked`：

```text
Worker cannot continue without Codex decision, user input, environment repair, budget decision, or failure triage.
```

`review`：

```text
Worker claims implementation is ready and has written evidence/handoff artifacts.
```

`changes_requested`：

```text
Codex reviewed the task and requires fixes before approval.
```

`approved`：

```text
Codex has reviewed the diff and evidence,
verified mergeability against the target branch,
and passed required integration smoke tests.

The task is ready to merge but has not yet been merged.
```

不能把“代码看起来可以”标成 `approved`。

`merged`：

```text
The approved task branch has been merged into the target branch,
post-merge status has been recorded,
post-merge smoke test result has been recorded if required by ACCEPTANCE.md,
and result/final artifacts are updated when applicable.
```

`failed`：

```text
Codex determines the task should stop under current requirements.
```

`review -> approved` 前必须满足：

```text
1. Diff 和实现质量通过 Codex review。
2. EVIDENCE.md 支持 ACCEPTANCE.md。
3. allowed_paths / forbidden_paths 没有越界。
4. 可干净合入 target branch，或 dry-run merge 通过。
5. 必需 integration smoke tests 通过。
6. 没有未解决 blocker 或异常 LOCK。
```

## 8. STATUS.json Schema

```json
{
  "task_id": "T001-name",
  "state": "review",
  "previous_state": "running",
  "owner": "claude-code",
  "branch": "agent/T001-name",
  "worktree": ".agent-worktrees/T001-name",
  "updated_at": "2026-07-03T12:00:00Z",
  "needs_coordinator": false,
  "summary": "",
  "blocking_reason": "",
  "blocker_type": "",
  "current_attempt_id": "A001-claude-x4p9a",
  "assigned_worker": {
    "agent": "claude-code",
    "agent_name": "claude-worker-1",
    "session_id": "s8d21",
    "role": "worker"
  },
  "evidence": {
    "commands_run": [],
    "logs": [],
    "passed": null
  },
  "state_history": [
    {
      "from": "pending",
      "to": "running",
      "actor": "dispatch",
      "at": "2026-07-03T12:00:00Z"
    }
  ]
}
```

`blocker_type` 在 `state = blocked` 时必填：

```text
needs_coordinator
needs_user
environment
budget
irrecoverable
```

含义：

```text
needs_coordinator
  需要协调者判断、重拆任务、设计决策、review、merge/conflict 处理或验收澄清。

needs_user
  需要用户输入、授权、偏好、数据访问或研究决策。

environment
  依赖、数据、硬件、服务、权限、文件系统、本地/远程运行环境问题。

budget
  时间、token、计算、成本或上下文预算问题。

irrecoverable
  worker 认为当前要求下不可完成，需要 coordinator 判定 failed、revision task 或 scope change。
```

`STATUS.json.evidence` 只是索引和摘要，真源是：

```text
EVIDENCE.md
logs/*
attempts/*/result.md
```

如果 `STATUS.json.evidence` 和 `EVIDENCE.md` / logs 冲突，`collect_status.py` 报 protocol violation，不自动修复。

## 9. Task Packet Contract

每个任务目录必须包含：

```text
TASK.md
CONTEXT.md
ACCEPTANCE.md
STATUS.json
HANDOFF.md
EVIDENCE.md
logs/
attempts/
```

`TASK.md` 必须包含：

```yaml
task_id:
goal:
allowed_paths:
forbidden_paths:
dependencies:
branch:
worktree:
non_goals:
```

`ACCEPTANCE.md` 必须包含：

```text
Required commands
Expected outputs
Metrics or thresholds
Smoke test
Failure handoff condition
```

`EVIDENCE.md` 必须移除 `<!-- RDO_TEMPLATE: EVIDENCE -->` marker，并包含：

```text
Commands Run
Tests Passed
Metrics / Outputs
Logs
Known Limitations
```

`HANDOFF.md` 必须移除 `<!-- RDO_TEMPLATE: HANDOFF -->` marker，并用于 worker 向 Codex 交接：

```text
What changed
What failed
Evidence
Decision needed
Suggested next action
```

## 10. attempts 规则

每次执行尝试创建一个 attempt：

```text
tasks/T001-name/
  attempts/
    A001-claude-x4p9a/
      ATTEMPT.json
      prompt.md
      transcript.log
      result.md
```

`ATTEMPT.json` 示例：

```json
{
  "attempt_id": "A001-claude-x4p9a",
  "task_id": "T001-name",
  "agent": "claude-code",
  "agent_name": "claude-worker-1",
  "session_id": "s8d21",
  "state": "completed",
  "handoff_valid": true,
  "handoff_state": "review",
  "started_at": "2026-07-03T12:10:00Z",
  "ended_at": "2026-07-03T12:20:00Z",
  "exit_code": 0,
  "runtime": {
    "model": null,
    "cli": "claude",
    "command": "claude ...",
    "cwd": "/path/to/worktree"
  }
}
```

`runtime.model` 可选，不作为协议主键。

ATTEMPT schema constraints:

```text
attempt_id: non-empty string
task_id: non-empty string
agent: non-empty string
agent_name: non-empty string
session_id: string; may be empty only if runtime cannot provide one
state: created|running|completed|invalid_handoff
started_at: non-empty valid ISO timestamp
ended_at: null for created/running; valid ISO timestamp for completed/invalid_handoff
exit_code: null for created/running; integer for completed/invalid_handoff
runtime: object
runtime.cli: non-empty string
runtime.command: non-empty string
runtime.cwd: non-empty string
runtime.model: optional/null
```

Attempt state 是 worker execution lifecycle，不是 task progress：

```text
created
  ATTEMPT.json exists, worker not yet launched. This should be brief.

running
  Worker process is active.

completed
  Worker exited and made a valid protocol handoff to review or blocked.

invalid_handoff
  Worker exited but did not produce legal STATUS/EVIDENCE/HANDOFF.
```

Task state invariants:

```text
STATUS.state = running requires:
  current_attempt_id exists
  ATTEMPT.state in [created, running]
  LOCK exists and matches current_attempt_id
  .dispatch-lock exists and matches current_attempt_id

STATUS.state = review requires:
  ATTEMPT.state = completed
  ATTEMPT.handoff_valid = true
  ATTEMPT.handoff_state = review
  STATUS.state_history ends with running -> review by actor claude-code
  STATUS.previous_state = running
  worker exit_code = 0
  EVIDENCE.md and HANDOFF.md have substantive content

STATUS.state = blocked requires:
  ATTEMPT.state = completed
  ATTEMPT.handoff_valid = true
  ATTEMPT.handoff_state = blocked
  STATUS.state_history ends with running -> blocked by actor claude-code
  STATUS.previous_state = running
  worker exit_code may be zero or nonzero
  HANDOFF.md has substantive content
  blocker_type in [needs_coordinator, needs_user, environment, budget, irrecoverable]
  blocking_reason non-empty
```

`changes_requested` 后的修复入口：

```text
小修 / 同一验收目标:
  同一 task 下新增 attempts/A002-*

范围变化 / 验收标准变化 / 设计变化:
  创建新 task T001R1-*
```

## 11. Git / Merge 规则

```text
1. 每个 task 一个 branch/worktree。
2. Claude Code 不直接 merge。
3. Codex review 通过后才 merge。
4. 两个并行任务不能改同一关键文件。
5. 如果 allowed_paths 重叠，Codex 必须重新排队或重拆任务。
6. approved 前必须验证 mergeability 和 integration smoke test。
7. merge 后状态才从 approved -> merged。
8. ACCEPTANCE.md 要求 post-merge smoke test 时，merged 必须记录结果。
```

## 12. LOCK 规则

`.dispatch-lock` 表示当前有 dispatch/worker 占用执行权，是原子互斥边界。`LOCK` 是人类可读 ownership/audit metadata，不是互斥真源。

位置：

```text
tasks/T001-name/LOCK
tasks/T001-name/.dispatch-lock/
```

内容：

```text
owner:
pid:
created_at:
command:
attempt_id:
```

规则：

```text
1. dispatch_claude.sh 用 mkdir 原子创建 .dispatch-lock。
2. .dispatch-lock 内必须写 owner/pid/attempt_id，释放前必须确认属于当前 dispatch。
3. worker 进程退出且 handoff validation 完成后释放 .dispatch-lock，包括 invalid_handoff。
4. dispatch_claude.sh 创建或更新 LOCK 作为可读审计元数据。
5. LOCK 可以保留到 Codex review/triage；不能作为互斥判断依据。
6. create_task.py 不创建 LOCK 或 .dispatch-lock。
7. STATUS.state 不是 running 时，残留 .dispatch-lock 是 protocol violation。
8. collect_status.py 只报告和校验，不删除、不修复。
```

## 13. 反向通知机制

第一版不依赖 Claude Code 主动唤醒 Codex。

Claude Code 卡住或完成时只写：

```text
STATUS.json
HANDOFF.md
EVIDENCE.md
logs/*
attempts/*/*
```

Codex 通过：

```bash
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id>
```

收集状态。

`codex resume`、`cc`、Codex plugin 可以作为可选增强，但不是协议依赖。

## 14. Lock Recovery Review

`.dispatch-lock` 异常不能由脚本自动修复，必须进入 Lock Recovery Review。

分工：

```text
collect_status.py  检测协议异常，只读
Codex/coordinator  检查上下文并给出判断
User               最终批准是否清理
remove_dispatch_lock.py 执行已批准的最小变更
EVENTS + diagnostics  保留审计记录
```

触发条件：

```text
STATUS.state != running but .dispatch-lock exists
STATUS.state = running but .dispatch-lock/attempt_id mismatch
STATUS.state = running but ATTEMPT.state in [completed, invalid_handoff]
.dispatch-lock age > stale threshold
.dispatch-lock/pid missing
.dispatch-lock/pid not alive
```

Codex/coordinator 检查：

```text
STATUS.json
ATTEMPT.json
LOCK
.dispatch-lock/*
attempts/<attempt>/transcript.log
attempts/<attempt>/result.md
recent EVENTS.ndjson entries
HANDOFF.md
EVIDENCE.md
git worktree / branch state
.dispatch-lock/pid liveness
whether transcript.log is still growing
```

分类：

```text
active     worker/dispatch 可能仍在运行，不清理
stale      worker/dispatch 明确已结束，建议只清理 .dispatch-lock
ambiguous  证据不足，继续观察或询问用户
```

用户确认前输出：

```text
Finding
Evidence
Risk
Recommendation
Proposed mutation
```

`Proposed mutation` 必须写明：

```text
Will:
  snapshot tasks/<task>/.dispatch-lock -> diagnostics/
  write recovery-operation.json in the snapshot
  remove tasks/<task>/.dispatch-lock
  append dispatch_lock_removed to EVENTS.ndjson

Will not:
  modify STATUS.json
  modify ATTEMPT.json
  modify HANDOFF.md
  modify EVIDENCE.md
  remove LOCK
  change FSM state
```

`remove_dispatch_lock.py` 默认 dry-run；必须有 `--confirmed` 才会修改文件。执行顺序：

```text
1. snapshot .dispatch-lock
2. write recovery-operation.json in the snapshot
3. rm -rf .dispatch-lock
4. append dispatch_lock_removed event
```

如果删除后 append event 失败，脚本必须在 snapshot 中写 `recovery-event-append-failed.json` 并返回非零。snapshot 必须足以作为 emergency audit fallback。

脚本不判断 active/stale，只执行用户批准后的机械清理。

## 15. Human / Machine Monitor

新增 `SUMMARY.md`：

```text
.agent-collab/runs/<run-id>/SUMMARY.md
```

角色：

```text
Human-readable monitor
Derived artifact
Not source of truth
Can be deleted and regenerated
```

协议真源仍然是：

```text
RUN.json
EVENTS.ndjson
JOURNAL.md
tasks/*/STATUS.json
references/state-machine.json
tasks/*/EVIDENCE.md
RESULT_LEDGER.md
```

`SUMMARY.md` 建议结构：

```markdown
# Run Summary

## Objective

## Current Status

## Task Board

## Active Blockers

## Ready For Codex Review

## Protocol Warnings

## Recent Decisions

## Experiment Results

## Next Actions
```

三层 monitor：

```text
Human monitor:
  SUMMARY.md

Machine monitor:
  collect_status.py --json

Interactive monitor:
  collect_status.py default output
```

## 16. Long-Term Memory

长期迭代需要两个 required memory artifacts：

```text
EVENTS.ndjson
JOURNAL.md
```

它们的分工：

```text
SUMMARY.md       = 当前状态 dashboard，可重建
EVENTS.ndjson    = 机器可读完整关键时间线，append-only
JOURNAL.md       = 人类可读 session 记忆，append-only
RESULT_LEDGER.md = 实验结果和 claim support
ADR/*            = 架构/设计级长期决策
reviews/*        = Codex review 记录
tasks/*/attempts = worker 执行记录
```

第一版不强制 `DECISIONS.md`。非架构但重要的取舍先写入 `JOURNAL.md`；只有长期有效的架构/设计决策才写入 `ADR/*`。如果后续 `JOURNAL.md` 中决策过多，再引入 `DECISIONS.md` 作为第二阶段索引。

`EVENTS.ndjson` 只记录能重建历史的关键事件，不记录每个小编辑。核心事件类型：

```text
run_created
requirements_updated
design_method_selected
adr_added
task_created
task_dispatched
worker_blocked
worker_review_ready
worker_exit_without_valid_status
codex_reviewed
changes_requested
task_approved
task_merged
task_failed
experiment_recorded
scope_changed
session_closed
```

每个工作 session 结束前，Codex 必须执行：

```text
1. run close_session.py or collect_status.py --write-summary
2. append JOURNAL.md
3. append important events to EVENTS.ndjson
4. update RESULT_LEDGER.md if experiments ran
5. add ADR only for durable architecture/design decisions
```

`close_session.py` 是推荐入口，因为它会同时更新 `SUMMARY.md`、追加 `JOURNAL.md`、追加 `session_closed` event。

## 17. Attempt Lifecycle And Audit Integrity

Task FSM 只表达任务目标进展。Worker/process 状态必须放在 `ATTEMPT.json`。`collect_status.py` 是 invariant checker，负责跨 `STATUS.json`、`ATTEMPT.json`、`LOCK`、`EVENTS.ndjson`、`EVIDENCE.md`、`HANDOFF.md` 检测协议一致性，但不自动修复。

四条实现原则：

```text
1. Task FSM stays about task progress only.
2. ATTEMPT.json owns worker execution lifecycle.
3. collect_status.py validates invariants across STATUS, ATTEMPT, LOCK, EVENTS, EVIDENCE, HANDOFF.
4. No destructive overwrite; use new run, new attempt, or revision task.
```

No destructive overwrite principle:

```text
No command may destructively overwrite or reinitialize audit-bearing artifacts.
Updates must be append-only where applicable, or legal state/protocol transitions where mutable.
```

Audit-bearing artifacts:

```text
STATUS.json
TASK.md
CONTEXT.md
ACCEPTANCE.md
EVIDENCE.md
HANDOFF.md
attempts/*
EVENTS.ndjson
JOURNAL.md
reviews/*
```

第一版不提供覆盖语义：

```text
init_run.py existing run -> fail
create_task.py existing task -> fail
```

需要变化时：

```text
new overall collaboration -> new run
same task retry -> new attempt
scope / acceptance / design change -> revision task, e.g. T001R1-*
```

## 18. Diagnostics

协议错误和状态异常可以写入：

```text
diagnostics/
  collect-status-<timestamp>.json
  collect-status-<timestamp>.md
```

`collect_status.py --write-diagnostics` 写入 diagnostics 文件。

`diagnostics/` 是派生诊断输出，不是协议真源。它可以被删除并重新生成；如果用户选择保留某次诊断作为审计快照，则应把对应文件视为历史记录，而不是当前状态真源。

`collect_status.py --write-summary` 必须把 protocol violations 写进 `SUMMARY.md` 的 `Protocol Warnings` 小节。

协议错误包括但不限于：

```text
非法状态
非法状态转移
非法 actor
缺失 STATUS.json 字段
STATUS.json.evidence 与 EVIDENCE.md/logs 冲突
缺失 current_attempt_id
缺失 EVIDENCE.md
陈旧 LOCK
LOCK attempt_id 与 STATUS.json.current_attempt_id 不一致
```

## 19. scripts 设计

第一版 scripts：

```text
scripts/
  init_run.py
  create_task.py
  dispatch_claude.sh
  collect_status.py
  close_session.py
```

### init_run.py

职责：

```text
1. 创建 .agent-collab/runs/<run-id>/。
2. 生成 RUN.json。
3. 创建空模板文件和目录。
4. 创建 SUMMARY.md 初始骨架。
5. 创建 EVENTS.ndjson 和 JOURNAL.md。
6. 创建 diagnostics/。
7. 记录 target_branch、base_commit、protocol_version。
8. 追加 run_created event。
9. 已有 run 直接拒绝；不提供 --force 覆盖语义。
```

限制：

```text
Do not generate substantive research/design decisions.
Do not fill REQUIREMENTS.md / DESIGN_BRIEF.md with inferred content.
Only scaffold headings/templates.
```

### create_task.py

职责：

```text
1. 创建标准 task packet。
2. 初始化 STATUS.json 为 pending。
3. 创建 TASK.md / CONTEXT.md / ACCEPTANCE.md / HANDOFF.md / EVIDENCE.md。
4. 创建 logs/ 和 attempts/。
5. 校验 task_id、allowed_paths、forbidden_paths。
6. 追加 task_created event。
7. 已有 task 直接拒绝；不覆盖 audit-bearing artifacts。
```

限制：

```text
Do not dispatch worker.
Do not create LOCK.
Do not create branch/worktree unless explicitly configured later.
Only creates pending task.
```

### dispatch_claude.sh

职责：

```text
1. 读取 state-machine.json。
2. 校验 pending|blocked|changes_requested -> running 是否合法。
3. 原子获取 .dispatch-lock；不能用 LOCK 作为互斥判断。
4. 创建 branch/worktree。
5. 创建 attempt 目录。
6. 拼接 prompt.md，并显式写入 TASK_DIR、STATUS_PATH、EVIDENCE_PATH、HANDOFF_PATH、ATTEMPT_DIR 等绝对协议路径。
7. 调用配置化 Claude Code CLI。
8. 保存 transcript.log / result.md。
9. 检查 worker 是否写出合法交付状态；review 必须 exit_code = 0，blocked 可为非零。
10. 维护 ATTEMPT.json state / ended_at / exit_code / handoff_valid / handoff_state。
11. 追加 task_dispatched / worker_review_ready / worker_blocked / worker_exit_without_valid_status events。
```

限制：

```text
Do not assume worker succeeded.
Do not auto-mark review.
Do not synthesize review/blocked on behalf of the worker.
Do not merge.
Do not approve.
```

dispatch 后必须检查：

```text
STATUS.json.state in [review, blocked]
STATUS.json.current_attempt_id == current attempt
STATUS.json.previous_state == running
last state_history is running -> review|blocked by claude-code with valid timestamp
review: exit_code == 0 and EVIDENCE.md + HANDOFF.md have substantive content
blocked: HANDOFF.md + allowed blocker_type + blocking_reason have substantive content
attempt transcript/result exists
```

如果不满足，报告：

```text
worker_exit_without_valid_status
```

但不自动标成功。

`dispatch_claude.sh` 必须配置化，不写死 CLI：

```bash
CLAUDE_CODE_CMD="${CLAUDE_CODE_CMD:-claude}"
```

### collect_status.py

职责：

```text
1. 遍历 tasks/*/STATUS.json。
2. 校验 STATUS schema。
3. 校验 FSM transition history。
4. 校验 actor 权限。
5. 汇总 task states。
6. 汇总 blocker_type。
7. 报告 LOCK 状态和可能陈旧锁。
8. 报告缺失 evidence/log/current_attempt。
9. 校验 ATTEMPT.json lifecycle 和 running/review/blocked invariants。
10. 严格校验 EVENTS.ndjson required fields 和 run_id。
11. 可生成 SUMMARY.md。
12. 可写 diagnostics 文件。
```

模式：

```bash
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id>
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --json
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --write-summary
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/collect_status.py" --run-id <run-id> --write-diagnostics
```

限制：

```text
Read-only by default.
--json 不写文件。
--write-summary 只写 SUMMARY.md。
--write-diagnostics 只写 diagnostics/collect-status-<timestamp>.*。
Never modify STATUS.json.
Never delete LOCK.
Never change FSM state.
Never repair protocol violations automatically.
```

### remove_dispatch_lock.py

职责：

```text
1. 只在 Lock Recovery Review 后、用户明确批准时使用。
2. 默认 dry-run；没有 --confirmed 不修改文件。
3. 读取 STATUS.current_attempt_id 作为 attempt_id。
4. snapshot tasks/<task>/.dispatch-lock 到 diagnostics/dispatch-lock-removed-<task>-<timestamp>/。
5. 在 snapshot 中写 recovery-operation.json。
6. 删除 tasks/<task>/.dispatch-lock。
7. 删除成功后追加 dispatch_lock_removed event。
8. 如果 event append 失败，写 recovery-event-append-failed.json 并返回非零。
```

限制：

```text
Does not decide active/stale/ambiguous.
Does not modify STATUS.json.
Does not modify ATTEMPT.json.
Does not modify LOCK.
Does not modify HANDOFF.md / EVIDENCE.md.
Does not change FSM state.
Fails if .dispatch-lock does not exist.
```

模式：

```bash
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/remove_dispatch_lock.py" --run-id <run-id> --task-id <task-id> --reason "<reason>"
python "$RESEARCH_DEV_ORCHESTRATOR_HOME/scripts/remove_dispatch_lock.py" --run-id <run-id> --task-id <task-id> --reason "<reason>" --confirmed
```

### close_session.py

职责：

```text
1. 调用 collect_status 生成当前 run report。
2. 更新派生 SUMMARY.md。
3. 追加 JOURNAL.md session entry。
4. 追加 EVENTS.ndjson session_closed event。
5. 不修改 STATUS.json、不删除 LOCK、不改变 FSM。
```

第一版不做：

```text
review_task.py
```

原因：review 依赖工程判断、研究目标、实验语义、diff 质量和 integration context，先由 Codex 按 `review-rubric.md` 手动 review 更稳。

## 20. Skill 文件结构

```text
research-dev-orchestrator/
  SKILL.md
  references/
    requirements-template.md
    design-method-selection.md
    adr-template.md
    experiment-plan-template.md
    reproducibility-template.md
    result-ledger-template.md
    task-packet-template.md
    review-rubric.md
    state-machine.md
    state-machine.json
    status-schema.md
    attempt-lifecycle.md
    lock-recovery.md
    summary-template.md
    events-schema.md
    journal-template.md
  scripts/
    init_run.py
    create_task.py
    dispatch_claude.sh
    collect_status.py
    remove_dispatch_lock.py
    close_session.py
```

## 21. SKILL.md 的职责

`SKILL.md` 保持轻量，只写：

```text
1. 何时使用该 skill。
2. 顶层 orchestration workflow。
3. 必须先完成 requirements，再做 design method selection。
4. 什么时候读取哪些 references。
5. 状态更新必须服从 state-machine.json。
6. 任务必须符合 task packet / evidence contract。
7. Codex / Claude Code 的职责边界。
8. scripts 的调用顺序和限制。
9. review 前必须检查 diff、evidence、mergeability、integration smoke test。
10. SUMMARY.md 是派生 monitor，不是真源。
11. 每个 session 结束必须维护 EVENTS.ndjson 和 JOURNAL.md。
12. Task FSM / Attempt lifecycle / audit integrity 必须分层处理。
13. Lock Recovery Review 必须先判断、再用户确认、再最小清理。
```

详细模板、schema、rubric、FSM 解释放进 `references`。

## 22. 推荐 references 内容

```text
requirements-template.md:
  需求、研究目标、非目标、约束、数据、baseline、metric、验收口径。

design-method-selection.md:
  设计前选择架构风格、拆分方法、接口风格、测试策略、实验追踪策略。

adr-template.md:
  Architecture Decision Record 模板。

experiment-plan-template.md:
  hypothesis、dataset、baseline、metric、ablation、expected outputs。

reproducibility-template.md:
  environment、seed、data version、commands、expected output、hardware notes。

result-ledger-template.md:
  每次实验结果、日志路径、结论、是否支持 claim。

task-packet-template.md:
  TASK / CONTEXT / ACCEPTANCE / HANDOFF / EVIDENCE 模板和修复入口规则。

review-rubric.md:
  Codex review diff、证据、测试、mergeability、integration smoke test、实验结果的 rubric。

state-machine.md:
  人读 FSM 解释、状态语义、状态转移权限、approved/merged 语义。

state-machine.json:
  机器读 FSM，脚本唯一可信来源。

status-schema.md:
  STATUS.json 字段解释、必填条件、blocker_type、evidence 摘要语义。

attempt-lifecycle.md:
  ATTEMPT.json schema、attempt states、handoff_valid、handoff_state、running/review/blocked invariants。

lock-recovery.md:
  .dispatch-lock 异常检测、Lock Recovery Review、用户确认格式、remove_dispatch_lock.py 边界。

summary-template.md:
  SUMMARY.md 结构和 derived artifact 约束。

events-schema.md:
  EVENTS.ndjson 的 append-only 事件格式和核心事件类型。

journal-template.md:
  JOURNAL.md 的 session closeout 模板和写入边界。
```

## 23. 最终审计结论

这版可以作为实现基线。

它解决了关键风险：

```text
1. 不 over-engineer：不用 server/RPC/queue。
2. 可恢复：所有状态在 repo-local 文件中。
3. 可审计：run/task/attempt/session 分离。
4. 可并行：branch/worktree + allowed_paths。
5. 可控状态：FSM 是机器可读硬协议。
6. 可验证：ACCEPTANCE + EVIDENCE + logs。
7. 可复现：EXPERIMENT_PLAN + REPRODUCIBILITY + RESULT_LEDGER。
8. 可 resume：SUMMARY.md + collect_status.py。
9. 可诊断：diagnostics/ 保存 protocol violations。
10. 可长期记忆：EVENTS.ndjson + JOURNAL.md 支撑跨周恢复和审计。
11. 可区分 task progress 与 worker execution：Task FSM + ATTEMPT lifecycle 分层。
12. 可避免破坏性覆盖：new run / new attempt / revision task。
13. 可扩展：`resume`、Codex plugin、更多 worker 都只是可选增强。
```

下一步可以继续迭代 `research-dev-orchestrator` skill，同时保持长期记忆层简单：required 只有 `EVENTS.ndjson` 和 `JOURNAL.md`，`DECISIONS.md` 暂不强制。
