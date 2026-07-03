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
          LOCK  # present only while execution ownership is held
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

`LOCK` 是条件存在文件。`create_task.py` 不创建 `LOCK`，只有 dispatch 或执行权占用时才创建。

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
  "needs_codex": false,
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
needs_codex
needs_user
environment
budget
irrecoverable
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

`EVIDENCE.md` 必须包含：

```text
Commands Run
Tests Passed
Metrics / Outputs
Logs
Known Limitations
```

`HANDOFF.md` 用于 worker 向 Codex 交接：

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
  "started_at": "2026-07-03T12:10:00Z",
  "ended_at": null,
  "runtime": {
    "model": "glm-5.2",
    "cli": "claude",
    "command": "claude ...",
    "cwd": "/path/to/worktree"
  }
}
```

`runtime.model` 可选，不作为协议主键。

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

`LOCK` 表示当前有 worker/dispatch 占用执行权，不表示任务完成状态。

位置：

```text
tasks/T001-name/LOCK
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
1. dispatch_claude.sh 创建 LOCK。
2. create_task.py 不创建 LOCK。
3. LOCK 存在则不重复派发。
4. worker 到 review 或 blocked 后，Codex review 前必须检查 LOCK。
5. Codex 负责释放、保留或重建 LOCK。
6. collect_status.py 只报告 LOCK 状态，不删除、不修复。
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
python scripts/collect_status.py --run-id <run-id>
```

收集状态。

`codex resume`、`cc`、Codex plugin 可以作为可选增强，但不是协议依赖。

## 14. Human / Machine Monitor

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

## 15. Diagnostics

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

## 16. scripts 设计

第一版 scripts：

```text
scripts/
  init_run.py
  create_task.py
  dispatch_claude.sh
  collect_status.py
```

### init_run.py

职责：

```text
1. 创建 .agent-collab/runs/<run-id>/。
2. 生成 RUN.json。
3. 创建空模板文件和目录。
4. 创建 SUMMARY.md 初始骨架。
5. 创建 diagnostics/。
6. 记录 target_branch、base_commit、protocol_version。
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
3. 检查 LOCK。
4. 创建 branch/worktree。
5. 创建 attempt 目录。
6. 拼接 prompt.md。
7. 调用配置化 Claude Code CLI。
8. 保存 transcript.log / result.md。
9. 检查 worker 是否写出合法交付状态。
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
EVIDENCE.md or HANDOFF.md has non-empty content
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
9. 可生成 SUMMARY.md。
10. 可写 diagnostics 文件。
```

模式：

```bash
python scripts/collect_status.py --run-id <run-id>
python scripts/collect_status.py --run-id <run-id> --json
python scripts/collect_status.py --run-id <run-id> --write-summary
python scripts/collect_status.py --run-id <run-id> --write-diagnostics
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

第一版不做：

```text
review_task.py
```

原因：review 依赖工程判断、研究目标、实验语义、diff 质量和 integration context，先由 Codex 按 `review-rubric.md` 手动 review 更稳。

## 17. Skill 文件结构

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
    summary-template.md
  scripts/
    init_run.py
    create_task.py
    dispatch_claude.sh
    collect_status.py
```

## 18. SKILL.md 的职责

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
```

详细模板、schema、rubric、FSM 解释放进 `references`。

## 19. 推荐 references 内容

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

summary-template.md:
  SUMMARY.md 结构和 derived artifact 约束。
```

## 20. 最终审计结论

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
10. 可扩展：`resume`、Codex plugin、更多 worker 都只是可选增强。
```

下一步可以生成真正的 `research-dev-orchestrator` skill：先实现最小脚本闭环 `init_run.py`、`create_task.py`、`collect_status.py`，再把 `dispatch_claude.sh` 做成配置化 wrapper。
