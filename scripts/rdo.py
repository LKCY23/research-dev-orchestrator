#!/usr/bin/env python3
"""Role-safe local command surface for coordinator and worker actions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from completion import write_completion
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
    write_completion(
        path,
        attempt_id=str(attempt_id),
        phase=expected_phase,
        requested_state="strategy_review",
        strategy_sha256=digest,
    )
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


def command_events(attempt: Path) -> list[dict[str, Any]]:
    path = attempt / "runtime" / "COMMANDS.ndjson"
    return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def completed_workflows(records: list[dict[str, Any]]) -> set[str]:
    return {
        str(record.get("workflow_id"))
        for record in records
        if record.get("event") in {"workflow_completed", "workflow_carried_forward"}
        and record.get("workflow_id")
    }


def completion_gate_reasons(
    attempt: Path,
    strategy: dict[str, Any],
    *,
    completing_workflow: str | None = None,
) -> list[str]:
    """Validate task-level execution gates, optionally before appending completion."""

    records = workflow_events(attempt)
    completed = completed_workflows(records)
    if completing_workflow:
        completed.add(completing_workflow)
    gate = strategy["completion_gate"]
    reasons: list[str] = []
    if gate["required_workflows_complete"]:
        missing = sorted(
            item["workflow_id"]
            for item in strategy["workflows"]
            if item["required"] and item["workflow_id"] not in completed
        )
        if missing:
            reasons.append(f"required workflows are incomplete: {missing}")
    if gate["acceptance_commands_pass"]:
        acceptance = [item for item in command_events(attempt) if item.get("acceptance") is True]
        if not acceptance:
            reasons.append("acceptance command records are missing")
        elif any(item.get("exit_code") != 0 or item.get("timed_out") for item in acceptance):
            reasons.append("one or more acceptance commands failed or timed out")
    if not gate["optional_workflows_may_timeout"] and any(
        record.get("event") == "workflow_timed_out" for record in records
    ):
        reasons.append("workflow timeout is forbidden by the completion gate")
    return reasons


def observed_reviewer_ids(attempt: Path) -> set[str]:
    path = attempt / "runtime" / "BACKEND_EVENTS.ndjson"
    if not path.exists():
        return set()
    reviewers: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") not in {"subagent_started", "subagent-start", "backend_agent_started"}:
            continue
        if record.get("event") == "subagent-start" and record.get("result") != "started":
            continue
        identifier = record.get("session_id") or record.get("agent_id")
        if isinstance(identifier, str) and identifier:
            reviewers.add(identifier)
    return reviewers


def independent_review_evidence(attempt: Path, definition: dict[str, Any], values: list[str]) -> list[dict[str, str]]:
    review = definition.get("review", {})
    if review.get("mode") != "independent":
        if values:
            raise SystemExit("--review-evidence is only valid for an independent review workflow")
        return []
    evidence: list[dict[str, str]] = []
    evidence_root = (attempt / "runtime" / "reviews").resolve()
    for value in values:
        reviewer, separator, raw_path = value.partition("=")
        if not separator or not reviewer or not raw_path:
            raise SystemExit("review evidence must use REVIEWER_ID=ARTIFACT_PATH")
        artifact = Path(raw_path).resolve()
        if evidence_root not in artifact.parents:
            raise SystemExit(f"review artifact must be under {evidence_root}")
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise SystemExit(f"review artifact is missing or empty: {artifact}")
        evidence.append({
            "reviewer_id": reviewer,
            "artifact": str(artifact),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        })
    reviewer_ids = [item["reviewer_id"] for item in evidence]
    required = int(review["required_reviewers"])
    if len(set(reviewer_ids)) < required:
        raise SystemExit(f"independent review requires {required} distinct reviewer artifacts")
    unobserved = sorted(set(reviewer_ids) - observed_reviewer_ids(attempt))
    if unobserved:
        raise SystemExit(f"reviewers were not observed as backend agent instances: {unobserved}")
    return evidence


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
        elif record.get("event") == "workflow_carried_forward":
            completed.add(str(record["workflow_id"]))
        elif record.get("event") in {"workflow_completed", "workflow_timed_out", "workflow_cancelled"}:
            previous = active.pop(instance, None)
            if previous and record.get("event") == "workflow_completed":
                completed.add(previous["workflow_id"])
    definition = definitions[args.workflow_id]
    timed_out = False
    if args.workflow_action == "start":
        if args.workflow_id in completed:
            raise SystemExit("workflow is already satisfied by completion or a carried-forward checkpoint")
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
        reviews = independent_review_evidence(attempt, definition, getattr(args, "review_evidence", []))
        completed_after = completed | {args.workflow_id}
        required = {item["workflow_id"] for item in strategy["workflows"] if item["required"]}
        if required.issubset(completed_after):
            reasons = completion_gate_reasons(
                attempt,
                strategy,
                completing_workflow=args.workflow_id,
            )
            if reasons:
                raise SystemExit("workflow completion gate failed: " + "; ".join(reasons))
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
    if name == "workflow_completed" and reviews:
        record["reviews"] = reviews
    runtime = attempt / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "WORKFLOWS.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    if name == "workflow_completed":
        completed_after = completed | {args.workflow_id}
        required = {item["workflow_id"] for item in strategy["workflows"] if item["required"]}
        if required.issubset(completed_after):
            write_json(
                runtime / "FINALIZATION.json",
                {
                    "schema_version": 1,
                    "stage": "finalizing",
                    "attempt_id": attempt.name,
                    "started_at": utc_now(),
                    "deadline_seconds": 90,
                },
            )
    event(task, name, "worker", **{key: value for key, value in record.items() if key not in {"at", "event"}})
    print(json.dumps(record))
    if timed_out and definition["on_timeout"] != "continue_without_result":
        raise SystemExit(f"workflow timed out; policy action is {definition['on_timeout']}")
    return 0


def handoff(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    if args.state not in {"verified", "review", "blocked"}:
        raise SystemExit("handoff state must be verified, review, or blocked")
    if args.state == "blocked" and (not args.blocker_type or not args.blocking_reason):
        raise SystemExit("blocked handoff requires blocker type and reason")
    status = load_json(path / "STATUS.json")
    profile = status.get("profile", "full")
    expected_terminal = {"direct": "verified", "delegated": "review", "full": "review"}[profile]
    if args.state in {"verified", "review"} and status.get("state") != "running":
        raise SystemExit(f"{args.state} handoff requires running state")
    if args.state in {"verified", "review"} and args.state != expected_terminal:
        raise SystemExit(f"profile {profile!r} requires {expected_terminal!r} handoff")
    if args.state == "verified" and not args.self_review_passed:
        raise SystemExit("direct verified handoff requires --self-review-passed")
    if args.state == "blocked" and status.get("state") not in {"planning", "running"}:
        raise SystemExit("blocked handoff requires an active attempt")
    if args.state == "review" and profile == "full":
        attempt = path / "attempts" / str(status.get("current_attempt_id"))
        strategy, _ = load_approved_strategy(path)
        reasons = completion_gate_reasons(attempt, strategy)
        if reasons:
            raise SystemExit("handoff completion gate failed: " + "; ".join(reasons))
    summary = args.summary
    if getattr(args, "summary_file", ""):
        summary = Path(args.summary_file).read_text(encoding="utf-8").strip()
    if not summary:
        raise SystemExit("handoff requires a non-empty summary or --summary-file")
    attempt_id = str(status.get("current_attempt_id"))
    attempt_path = path / "attempts" / attempt_id
    commands = list(args.command)
    files = list(args.file)
    if getattr(args, "auto_derive", False):
        recorded = [item for item in command_events(attempt_path) if item.get("acceptance") is True]
        if recorded:
            commands = [" ".join(map(str, item.get("command", []))) for item in recorded]
        metadata = load_json(attempt_path / "ATTEMPT.json")
        cwd = metadata.get("runtime", {}).get("cwd") if isinstance(metadata.get("runtime"), dict) else None
        if isinstance(cwd, str) and cwd:
            result = subprocess.run(
                ["git", "diff", "--name-only"], cwd=cwd, text=True, capture_output=True, check=False
            )
            if result.returncode == 0:
                files = [line for line in result.stdout.splitlines() if line]
    evidence_lines = ["# Evidence", "", "## Commands Run", ""]
    evidence_lines.extend(f"- `{item}`" for item in commands)
    evidence_lines.extend(["", "## Files Changed", ""])
    evidence_lines.extend(f"- `{item}`" for item in files)
    atomic_text(path / "EVIDENCE.md", "\n".join(evidence_lines) + "\n")
    atomic_text(path / "HANDOFF.md", f"# Handoff\n\n## Summary\n\n{summary}\n")
    request = {
        "_template": False,
        "requested_state": args.state,
        "summary": summary,
        "commands_run": commands,
        "files_changed": files,
        "known_limitations": args.limitation,
        "self_review": {
            "acceptance_checked": bool(args.self_review_passed),
            "changed_paths_checked": bool(args.self_review_passed),
            "tests_passed": bool(args.self_review_passed),
            "diff_check_passed": bool(args.self_review_passed),
            "findings": args.self_review_finding,
            "fixes_applied": args.self_review_fix,
            "passed": bool(args.self_review_passed),
        },
        "needs_coordinator": args.state == "blocked",
        "blocker_type": args.blocker_type,
        "blocking_reason": args.blocking_reason,
    }
    temporary = path / "HANDOFF.json.tmp"
    write_json(temporary, request)
    os.replace(temporary, path / "HANDOFF.json")
    attempt_metadata = load_json(attempt_path / "ATTEMPT.json")
    write_completion(
        path,
        attempt_id=attempt_id,
        phase=str(attempt_metadata.get("phase")),
        requested_state=args.state,
        strategy_sha256=attempt_metadata.get("strategy_sha256"),
    )
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
        command = workflows.add_parser(name); command.add_argument("--attempt-dir", required=True); command.add_argument("--workflow-id", required=True); command.add_argument("--instance-id", required=True); command.add_argument("--review-evidence", action="append", default=[]); command.set_defaults(func=workflow_action)
    command = areas.add_parser("handoff"); command.add_argument("--task-dir", required=True); command.add_argument("--state", required=True); command.add_argument("--summary", required=True); command.add_argument("--command", action="append", default=[]); command.add_argument("--file", action="append", default=[]); command.add_argument("--limitation", action="append", default=[]); command.add_argument("--self-review-passed", action="store_true"); command.add_argument("--self-review-finding", action="append", default=[]); command.add_argument("--self-review-fix", action="append", default=[]); command.add_argument("--blocker-type", default=""); command.add_argument("--blocking-reason", default=""); command.set_defaults(func=handoff)
    command = areas.add_parser("finalize"); command.add_argument("--task-dir", required=True); command.add_argument("--state", required=True); command.add_argument("--summary", default=""); command.add_argument("--summary-file", default=""); command.add_argument("--command", action="append", default=[]); command.add_argument("--file", action="append", default=[]); command.add_argument("--limitation", action="append", default=[]); command.add_argument("--self-review-passed", action="store_true"); command.add_argument("--self-review-finding", action="append", default=[]); command.add_argument("--self-review-fix", action="append", default=[]); command.add_argument("--blocker-type", default=""); command.add_argument("--blocking-reason", default=""); command.set_defaults(func=handoff, auto_derive=True)
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
