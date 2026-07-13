#!/usr/bin/env python3
"""Role-safe local command surface for coordinator and worker actions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protocol import SKILL_ROOT, append_event, load_json, parse_iso, utc_now, write_json
from strategy import StrategyValidationError, canonical_digest, load_approved_strategy, review_strategy, submit_strategy
from supervisor import run_supervised, terminate_processes


def task_dir(value: str) -> Path:
    path = Path(value).resolve()
    if not (path / "STATUS.json").exists():
        raise SystemExit(f"invalid task directory: {path}")
    return path


def run_dir(path: Path) -> Path:
    return path.parent.parent


def event(path: Path, name: str, actor: str, **extra: Any) -> None:
    status = load_json(path / "STATUS.json")
    append_event(
        run_dir(path),
        {"at": utc_now(), "actor": actor, "event": name, "run_id": run_dir(path).name, "task_id": status["task_id"], **extra},
    )


def transition(path: Path, target: str, actor: str) -> None:
    status_path = path / "STATUS.json"
    status = load_json(status_path)
    source = status.get("state")
    fsm = load_json(SKILL_ROOT / "references" / "state-machine.json")
    if actor not in fsm.get("transitions", {}).get(source, {}).get(target, []):
        raise SystemExit(f"illegal transition: {source!r} -> {target!r} by {actor}")
    now = utc_now()
    status.update(previous_state=source, state=target, updated_at=now, owner=actor)
    status.setdefault("state_history", []).append({"from": source, "to": target, "actor": actor, "at": now})
    write_json(status_path, status)


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def strategy_submit(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    if status.get("state") not in {"planning", "running"}:
        raise SystemExit("strategy submission requires planning or running state")
    attempt_id = status.get("current_attempt_id")
    attempt = load_json(path / "attempts" / str(attempt_id) / "ATTEMPT.json")
    expected_phase = "planning" if status["state"] == "planning" else "execution"
    if attempt.get("phase") != expected_phase or attempt.get("state") not in {"created", "running"}:
        raise SystemExit("strategy submission requires the current active attempt")
    payload = load_json(Path(args.file))
    if args.strategy_action == "submit" and payload.get("revision") != 1:
        raise SystemExit("strategy submit is only for revision 1; use strategy revise")
    if args.strategy_action == "revise" and (
        not isinstance(payload.get("revision"), int) or payload["revision"] <= 1
    ):
        raise SystemExit("strategy revise requires revision > 1")
    output, digest = submit_strategy(path, payload)
    summary = f"Submitted strategy {payload['strategy_id']} for coordinator review"
    atomic_text(path / "EVIDENCE.md", f"# Evidence\n\nValidated `{output.name}` with SHA-256 `{digest}`.\n")
    atomic_text(path / "HANDOFF.md", f"# Strategy Handoff\n\n{summary}.\n")
    write_json(
        path / "HANDOFF.json",
        {
            "_template": False,
            "requested_state": "strategy_review",
            "summary": summary,
            "commands_run": [],
            "files_changed": [],
            "known_limitations": [],
            "needs_coordinator": True,
            "blocker_type": "",
            "blocking_reason": "",
            "strategy_revision": payload["revision"],
            "strategy_sha256": digest,
        },
    )
    event(path, "strategy_submitted", "worker", strategy_id=payload["strategy_id"], revision=payload["revision"], strategy_sha256=digest)
    print(json.dumps({"path": str(output), "strategy_sha256": digest}))
    return 0


def strategy_review(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    if load_json(path / "STATUS.json").get("state") != "strategy_review":
        raise SystemExit("strategy review requires strategy_review state")
    handoff = load_json(path / "HANDOFF.json")
    submitted = load_json(path / "strategy" / f"STRATEGY-v{args.revision:03d}.json")
    digest = canonical_digest(submitted)
    if handoff.get("strategy_revision") != args.revision or handoff.get("strategy_sha256") != digest:
        raise SystemExit("strategy review revision does not match the validated worker handoff")
    decision = "approved" if args.strategy_action == "approve" else "changes_requested"
    review = review_strategy(path, args.revision, decision=decision, reviewer=args.reviewer, notes=args.note)
    event(path, "strategy_reviewed", "coordinator", decision=decision, revision=args.revision, strategy_sha256=review["strategy_sha256"])
    if decision == "changes_requested":
        transition(path, "changes_requested", "coordinator")
    print(json.dumps(review))
    return 0


def workflow_events(attempt: Path) -> list[dict[str, Any]]:
    path = attempt / "runtime" / "WORKFLOWS.ndjson"
    return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def active_execution_attempt(value: str) -> tuple[Path, Path, dict[str, Any]]:
    attempt = Path(value).resolve()
    task = attempt.parent.parent
    status = load_json(task / "STATUS.json")
    if status.get("state") != "running" or status.get("current_attempt_id") != attempt.name:
        raise SystemExit("command requires the current running execution attempt")
    metadata = load_json(attempt / "ATTEMPT.json")
    if metadata.get("phase") != "execution" or metadata.get("state") not in {"created", "running"}:
        raise SystemExit("attempt is not an active execution attempt")
    strategy, review = load_approved_strategy(task)
    if (
        metadata.get("strategy_id") != strategy.get("strategy_id")
        or metadata.get("strategy_sha256") != review.get("strategy_sha256")
    ):
        raise SystemExit("attempt strategy does not match current approval")
    return attempt, task, strategy


def workflow_action(args: argparse.Namespace) -> int:
    attempt, task, strategy = active_execution_attempt(args.attempt_dir)
    definitions = {item["workflow_id"]: item for item in strategy["workflows"]}
    if args.workflow_id not in definitions:
        raise SystemExit(f"workflow is not approved: {args.workflow_id}")
    records = workflow_events(attempt)
    active: dict[str, dict[str, Any]] = {}
    completed: set[str] = set()
    seen: set[str] = set()
    for record in records:
        instance = record.get("instance_id")
        seen.add(instance)
        if record.get("event") == "workflow_started":
            active[instance] = record
        elif record.get("event") in {"workflow_completed", "workflow_timed_out", "workflow_cancelled"}:
            previous = active.pop(instance, None)
            if previous and record.get("event") == "workflow_completed":
                completed.add(previous["workflow_id"])
    definition = definitions[args.workflow_id]
    timed_out = False
    if args.workflow_action == "start":
        if args.instance_id in seen:
            raise SystemExit("workflow instance_id must be unique")
        missing = sorted(set(definition["depends_on"]) - completed)
        if missing:
            raise SystemExit(f"workflow dependencies are incomplete: {missing}")
        if len(active) >= strategy["global_budget"]["max_parallel_workflows"]:
            raise SystemExit("max_parallel_workflows exceeded")
        starts_for_workflow = sum(
            1 for record in records
            if record.get("event") == "workflow_started" and record.get("workflow_id") == args.workflow_id
        )
        if starts_for_workflow >= definition["budget"]["max_instances"]:
            raise SystemExit("workflow max_instances exceeded")
        total_starts = sum(1 for record in records if record.get("event") == "workflow_started")
        if total_starts >= strategy["global_budget"]["max_workflow_instances"]:
            raise SystemExit("global max_workflow_instances exceeded")
        name = "workflow_started"
    elif args.workflow_action == "heartbeat":
        if args.instance_id not in active:
            raise SystemExit("heartbeat requires an active workflow instance")
        name = "workflow_heartbeat"
    else:
        if args.instance_id not in active:
            raise SystemExit("completion requires an active workflow instance")
        name = "workflow_completed"
    if args.workflow_action != "start":
        started_at = parse_iso(active[args.instance_id].get("at"))
        if started_at is None:
            raise SystemExit("active workflow has an invalid start timestamp")
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        if elapsed > definition["budget"]["wall_seconds"]:
            timed_out = True
            name = "workflow_timed_out"
    record = {"at": utc_now(), "event": name, "workflow_id": args.workflow_id, "instance_id": args.instance_id, "attempt_id": attempt.name}
    runtime = attempt / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "WORKFLOWS.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    event(task, name, "worker", **{key: value for key, value in record.items() if key not in {"at", "event"}})
    print(json.dumps(record))
    if timed_out and definition["on_timeout"] != "continue_without_result":
        raise SystemExit(f"workflow timed out; policy action is {definition['on_timeout']}")
    return 0


def handoff(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    if args.state not in {"review", "blocked"}:
        raise SystemExit("handoff state must be review or blocked")
    if args.state == "blocked" and (not args.blocker_type or not args.blocking_reason):
        raise SystemExit("blocked handoff requires blocker type and reason")
    status = load_json(path / "STATUS.json")
    if args.state == "review" and status.get("state") != "running":
        raise SystemExit("review handoff requires running state")
    if args.state == "blocked" and status.get("state") not in {"planning", "running"}:
        raise SystemExit("blocked handoff requires an active attempt")
    if args.state == "review":
        attempt = path / "attempts" / str(status.get("current_attempt_id"))
        strategy, _ = load_approved_strategy(path)
        records = workflow_events(attempt)
        if strategy["completion_gate"]["required_workflows_complete"]:
            completed = {
                record.get("workflow_id")
                for record in records
                if record.get("event") == "workflow_completed"
            }
            missing = sorted(
                item["workflow_id"] for item in strategy["workflows"]
                if item["required"] and item["workflow_id"] not in completed
            )
            if missing:
                raise SystemExit(f"required workflows are incomplete: {missing}")
        commands_path = attempt / "runtime" / "COMMANDS.ndjson"
        commands = [] if not commands_path.exists() else [
            json.loads(line) for line in commands_path.read_text().splitlines() if line.strip()
        ]
        acceptance_commands = [item for item in commands if item.get("acceptance") is True]
        if strategy["completion_gate"]["acceptance_commands_pass"] and (
            not acceptance_commands
            or any(item.get("exit_code") != 0 or item.get("timed_out") for item in acceptance_commands)
        ):
            raise SystemExit("acceptance command completion gate failed")
        if not strategy["completion_gate"]["optional_workflows_may_timeout"] and any(
            record.get("event") == "workflow_timed_out" for record in records
        ):
            raise SystemExit("workflow timeout is forbidden by the completion gate")
    atomic_text(path / "EVIDENCE.md", "# Evidence\n\n## Commands Run\n" + "".join(f"- `{item}`\n" for item in args.command))
    atomic_text(path / "HANDOFF.md", f"# Handoff\n\n## Summary\n\n{args.summary}\n")
    request = {
        "_template": False,
        "requested_state": args.state,
        "summary": args.summary,
        "commands_run": args.command,
        "files_changed": args.file,
        "known_limitations": args.limitation,
        "needs_coordinator": args.state == "blocked",
        "blocker_type": args.blocker_type,
        "blocking_reason": args.blocking_reason,
    }
    temporary = path / "HANDOFF.json.tmp"
    write_json(temporary, request)
    os.replace(temporary, path / "HANDOFF.json")
    (path / "HANDOFF.complete").write_text(utc_now() + "\n")
    print(json.dumps(request))
    return 0


def execute_command(args: argparse.Namespace) -> int:
    attempt, _task, strategy = active_execution_attempt(args.attempt_dir)
    definitions = {item["workflow_id"]: item for item in strategy["workflows"]}
    definition = definitions.get(args.workflow_id)
    if definition is None:
        raise SystemExit(f"workflow is not approved: {args.workflow_id}")
    records = workflow_events(attempt)
    active = {
        record.get("instance_id")
        for record in records
        if record.get("event") == "workflow_started" and record.get("workflow_id") == args.workflow_id
    }
    for record in records:
        if record.get("event") in {"workflow_completed", "workflow_timed_out", "workflow_cancelled"}:
            active.discard(record.get("instance_id"))
    if args.instance_id not in active:
        raise SystemExit("rdo exec requires an active approved workflow instance")
    if args.timeout > definition["budget"]["command_seconds"]:
        raise SystemExit("command timeout exceeds approved workflow command budget")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise SystemExit("rdo exec requires a command after --")
    result = run_supervised(
        command,
        timeout_seconds=args.timeout,
        cwd=Path(args.cwd).resolve() if args.cwd else None,
        stdin=0,
        stdout=1,
        stderr=2,
    )
    record = {
        "at": utc_now(),
        "event": "command_completed",
        "workflow_id": args.workflow_id,
        "instance_id": args.instance_id,
        "command": command,
        "timeout_seconds": args.timeout,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "elapsed_seconds": result.elapsed_seconds,
        "surviving_pids": list(result.surviving_pids),
        "acceptance": args.acceptance,
    }
    runtime = attempt / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "COMMANDS.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return result.exit_code


def status_action(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    attempt_id = status.get("current_attempt_id")
    payload: dict[str, Any] = {
        "status": status,
        "attempt": None,
        "supervisor": None,
        "workflows": [],
        "backend_profile": None,
        "agents": None,
        "backend_events": [],
        "governance_violations": [],
    }
    if attempt_id:
        attempt_dir = path / "attempts" / str(attempt_id)
        attempt_path = attempt_dir / "ATTEMPT.json"
        if attempt_path.exists():
            payload["attempt"] = load_json(attempt_path)
        supervisor_path = attempt_dir / "runtime" / "supervisor.json"
        if supervisor_path.exists():
            payload["supervisor"] = load_json(supervisor_path)
        payload["workflows"] = workflow_events(attempt_dir)
        runtime_dir = attempt_dir / "runtime"
        for key, filename in (
            ("backend_profile", "BACKEND_PROFILE.json"),
            ("agents", "AGENTS.json"),
        ):
            path = runtime_dir / filename
            if path.exists():
                payload[key] = load_json(path)
        for key, filename in (
            ("backend_events", "BACKEND_EVENTS.ndjson"),
            ("governance_violations", "VIOLATIONS.ndjson"),
        ):
            path = runtime_dir / filename
            if path.exists():
                payload[key] = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    print(json.dumps(payload, indent=2))
    return 0


def control(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    if status.get("state") not in {"planning", "running"}:
        raise SystemExit("worker control requires an active planning or execution attempt")
    attempt_id = status.get("current_attempt_id")
    lock = path / ".dispatch-lock"
    if args.worker_action in {"message", "interrupt"}:
        session_file = lock / "tmux_session"
        if not session_file.exists():
            raise SystemExit("worker control requires an active tmux session")
        session = session_file.read_text().strip()
        if args.worker_action == "message":
            subprocess.run(["tmux", "send-keys", "-t", session, "-l", args.text], check=True)
            subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=True)
            name, result = "worker_instruction_submitted", {"status": "submitted", "session": session}
        else:
            subprocess.run(["tmux", "send-keys", "-t", session, "C-c"], check=True)
            name, result = "worker_interrupted", {"status": "interrupt_sent", "session": session}
    else:
        metadata = path / "attempts" / str(attempt_id) / "runtime" / "supervisor.json"
        if not metadata.exists():
            raise SystemExit("attempt supervisor metadata is unavailable")
        runtime = load_json(metadata)
        survivors = terminate_processes(set(runtime.get("observed_pgids", [])), set(runtime.get("observed_pids", [])))
        name, result = "worker_terminated", {"status": "terminated", "surviving_pids": list(survivors)}
    event(path, name, "coordinator", attempt_id=attempt_id, **result)
    print(json.dumps(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RDO local command surface")
    areas = parser.add_subparsers(dest="area", required=True)
    strategy = areas.add_parser("strategy").add_subparsers(dest="strategy_action", required=True)
    for name in ("submit", "revise"):
        command = strategy.add_parser(name); command.add_argument("--task-dir", required=True); command.add_argument("--file", required=True); command.set_defaults(func=strategy_submit)
    for name in ("approve", "changes"):
        command = strategy.add_parser(name); command.add_argument("--task-dir", required=True); command.add_argument("--revision", type=int, required=True); command.add_argument("--reviewer", required=True); command.add_argument("--note", action="append", default=[]); command.set_defaults(func=strategy_review)
    workflows = areas.add_parser("workflow").add_subparsers(dest="workflow_action", required=True)
    for name in ("start", "heartbeat", "complete"):
        command = workflows.add_parser(name); command.add_argument("--attempt-dir", required=True); command.add_argument("--workflow-id", required=True); command.add_argument("--instance-id", required=True); command.set_defaults(func=workflow_action)
    command = areas.add_parser("handoff"); command.add_argument("--task-dir", required=True); command.add_argument("--state", required=True); command.add_argument("--summary", required=True); command.add_argument("--command", action="append", default=[]); command.add_argument("--file", action="append", default=[]); command.add_argument("--limitation", action="append", default=[]); command.add_argument("--blocker-type", default=""); command.add_argument("--blocking-reason", default=""); command.set_defaults(func=handoff)
    command = areas.add_parser("exec"); command.add_argument("--attempt-dir", required=True); command.add_argument("--workflow-id", required=True); command.add_argument("--instance-id", required=True); command.add_argument("--timeout", type=float, required=True); command.add_argument("--cwd", default=""); command.add_argument("--acceptance", action="store_true"); command.add_argument("command", nargs=argparse.REMAINDER); command.set_defaults(func=execute_command)
    command = areas.add_parser("status"); command.add_argument("--task-dir", required=True); command.set_defaults(func=status_action)
    workers = areas.add_parser("worker").add_subparsers(dest="worker_action", required=True)
    command = workers.add_parser("message"); command.add_argument("--task-dir", required=True); command.add_argument("--text", required=True); command.set_defaults(func=control)
    for name in ("interrupt", "terminate"):
        command = workers.add_parser(name); command.add_argument("--task-dir", required=True); command.set_defaults(func=control)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        return args.func(args)
    except StrategyValidationError as exc:
        print(f"strategy error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
