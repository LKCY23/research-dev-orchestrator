#!/usr/bin/env python3
"""Role-safe local command surface for coordinator and worker actions."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from completion import write_completion
from protocol import SKILL_ROOT, append_event, load_json, parse_iso, repo_root, utc_now, write_json
from strategy import StrategyValidationError, canonical_digest, load_approved_strategy, review_strategy, submit_strategy
from supervisor import run_supervised, terminate_processes
from worktree_fingerprint import fingerprint


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


def validate_transition(path: Path, target: str, actor: str) -> str:
    status = load_json(path / "STATUS.json")
    source = status.get("state")
    fsm = load_json(SKILL_ROOT / "references" / "state-machine.json")
    if actor not in fsm.get("transitions", {}).get(source, {}).get(target, []):
        raise SystemExit(f"illegal transition: {source!r} -> {target!r} by {actor}")
    return str(source)


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def derive_task_changed_files(task: Path, attempt_path: Path, cwd: Path) -> list[str]:
    before_paths = sorted((attempt_path.parent).glob("*/runtime/worktree-before.json"))
    if not before_paths:
        raise SystemExit("cannot derive task changes: no worktree-before fingerprint exists")
    before_payload = load_json(before_paths[0])
    before = {item["path"]: item["sha256"] for item in before_payload.get("entries", [])}
    after_payload = fingerprint(cwd)
    after = {item["path"]: item["sha256"] for item in after_payload.get("entries", [])}
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def require_clean_task_worktree(cwd: Path, expected_branch: str) -> None:
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=cwd, text=True, capture_output=True, check=False
    )
    if branch.returncode != 0 or branch.stdout.strip() != expected_branch:
        raise SystemExit(
            f"task worktree must be on assigned branch {expected_branch!r}, got {branch.stdout.strip()!r}"
        )
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode != 0:
        raise SystemExit(f"cannot inspect task worktree status: {status.stderr.strip()}")
    if status.stdout.strip():
        raise SystemExit("task worktree must be committed and clean before final handoff")


def require_clean_target_worktree(cwd: Path, expected_branch: str) -> None:
    branch = git_output(cwd, "branch", "--show-current")
    if branch != expected_branch:
        raise SystemExit(
            f"target worktree must be on run target branch {expected_branch!r}, got {branch!r}"
        )
    raw = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if raw.returncode != 0:
        raise SystemExit("cannot inspect target worktree status")
    dirty: list[str] = []
    for entry in raw.stdout.split(b"\0"):
        if not entry:
            continue
        text = entry.decode("utf-8", errors="replace")
        path = text[3:] if len(text) >= 4 else text
        if path.startswith(".agent-collab/") or path.startswith(".agent-worktrees/"):
            continue
        dirty.append(path)
    if dirty:
        raise SystemExit(f"target worktree has non-RDO changes: {sorted(dirty)}")


def git_output(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SystemExit(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def resolve_worktree(root: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise SystemExit(f"{label} is missing")
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not resolved.is_dir():
        raise SystemExit(f"{label} does not exist: {resolved}")
    return resolved


def require_same_repository(root: Path, worktree: Path) -> None:
    root_common_raw = Path(git_output(root, "rev-parse", "--git-common-dir"))
    worktree_common_raw = Path(git_output(worktree, "rev-parse", "--git-common-dir"))
    root_common = (
        root_common_raw.resolve()
        if root_common_raw.is_absolute()
        else (root / root_common_raw).resolve()
    )
    worktree_common = (
        worktree_common_raw.resolve()
        if worktree_common_raw.is_absolute()
        else (worktree / worktree_common_raw).resolve()
    )
    if root_common != worktree_common:
        raise SystemExit("target and task worktrees must belong to the same Git repository")


def current_task_review(path: Path) -> dict[str, Any]:
    pointer_path = path / "reviews" / "CURRENT_TASK_REVIEW.json"
    if not pointer_path.exists():
        raise SystemExit("approved task is missing CURRENT_TASK_REVIEW.json")
    pointer = load_json(pointer_path)
    relative = pointer.get("decision_path") if isinstance(pointer, dict) else None
    if not isinstance(relative, str) or not relative:
        raise SystemExit("CURRENT_TASK_REVIEW.json has no decision_path")
    decision_path = (path / relative).resolve()
    try:
        decision_path.relative_to((path / "reviews").resolve())
    except ValueError as exc:
        raise SystemExit("task review decision must be inside the reviews directory") from exc
    decision = load_json(decision_path)
    if decision.get("decision") != "approved":
        raise SystemExit("current task review decision is not approved")
    if decision.get("task_id") != load_json(path / "STATUS.json").get("task_id"):
        raise SystemExit("task review decision task_id does not match STATUS.json")
    return decision


def approval_git_binding(path: Path, status: dict[str, Any]) -> dict[str, str]:
    root = repo_root(path)
    task_worktree = resolve_worktree(root, status.get("worktree"), label="task worktree")
    require_same_repository(root, task_worktree)
    source_branch = str(status.get("branch") or "")
    require_clean_task_worktree(task_worktree, source_branch)
    run = load_json(run_dir(path) / "RUN.json")
    target_branch = run.get("target_branch")
    if not isinstance(target_branch, str) or not target_branch:
        raise SystemExit("RUN.json target_branch is missing")
    approved_commit = git_output(task_worktree, "rev-parse", "HEAD")
    target_commit = git_output(root, "rev-parse", target_branch)
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", target_commit, approved_commit],
        cwd=root,
        check=False,
    ).returncode != 0:
        raise SystemExit("task branch is not fast-forward mergeable from the reviewed target commit")
    return {
        "approved_commit": approved_commit,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "target_commit_at_review": target_commit,
        "evidence_sha256": hashlib.sha256((path / "EVIDENCE.md").read_bytes()).hexdigest(),
        "handoff_sha256": hashlib.sha256((path / "HANDOFF.json").read_bytes()).hexdigest(),
    }


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


def task_review(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    if status.get("state") != "review":
        raise SystemExit("task review requires review state")

    target_by_decision = {
        "approved": "approved",
        "changes_requested": "changes_requested",
        "failed": "failed",
    }
    event_by_decision = {
        "approved": "task_approved",
        "changes_requested": "changes_requested",
        "failed": "task_failed",
    }
    target = target_by_decision[args.decision]
    validate_transition(path, target, "coordinator")

    findings_path = Path(args.findings_file).resolve()
    try:
        findings_relative = findings_path.relative_to(path)
    except ValueError as exc:
        raise SystemExit("findings file must be inside the task directory") from exc
    if not findings_path.is_file():
        raise SystemExit(f"findings file does not exist: {findings_path}")
    findings = findings_path.read_text(encoding="utf-8")
    if not findings.strip():
        raise SystemExit("findings file must be non-empty")

    reviews = path / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    revision = len(list(reviews.glob("DECISION-v*.json"))) + 1
    decision_path = reviews / f"DECISION-v{revision:03d}.json"
    if decision_path.exists():
        raise SystemExit(f"refusing to overwrite task review decision: {decision_path}")
    payload = {
        "schema_version": 1,
        "task_id": load_json(path / "STATUS.json")["task_id"],
        "revision": revision,
        "decision": args.decision,
        "reviewer": args.reviewer,
        "reviewed_at": utc_now(),
        "findings_path": findings_relative.as_posix(),
        "findings_sha256": hashlib.sha256(findings.encode("utf-8")).hexdigest(),
        "notes": args.note,
    }
    if args.decision == "approved":
        payload.update(approval_git_binding(path, status))
    write_json(decision_path, payload)
    write_json(
        reviews / "CURRENT_TASK_REVIEW.json",
        {
            "revision": revision,
            "decision_path": decision_path.relative_to(path).as_posix(),
        },
    )
    transition(path, target, "coordinator")
    event(
        path,
        "coordinator_reviewed",
        "coordinator",
        decision=args.decision,
        review_revision=revision,
        findings_path=findings_relative.as_posix(),
    )
    event(
        path,
        event_by_decision[args.decision],
        "coordinator",
        review_revision=revision,
        findings_path=findings_relative.as_posix(),
    )
    print(json.dumps(payload, indent=2))
    return 0


def merge_source_commit(path: Path, status: dict[str, Any], root: Path) -> tuple[str, str, str]:
    source_branch = str(status.get("branch") or "")
    task_worktree = resolve_worktree(root, status.get("worktree"), label="task worktree")
    require_same_repository(root, task_worktree)
    require_clean_task_worktree(task_worktree, source_branch)
    source_head = git_output(task_worktree, "rev-parse", "HEAD")
    run = load_json(run_dir(path) / "RUN.json")
    target_branch = run.get("target_branch")
    if not isinstance(target_branch, str) or not target_branch:
        raise SystemExit("RUN.json target_branch is missing")

    profile = status.get("profile", "full")
    if status.get("state") in {"approved", "merged"} and profile != "direct":
        decision = current_task_review(path)
        required = {
            "approved_commit", "source_branch", "target_branch",
            "target_commit_at_review", "evidence_sha256", "handoff_sha256",
        }
        missing = sorted(field for field in required if not decision.get(field))
        if missing:
            raise SystemExit(f"approved task review is missing Git binding fields: {missing}")
        if decision["source_branch"] != source_branch or decision["target_branch"] != target_branch:
            raise SystemExit("approved task review branch binding no longer matches task/run metadata")
        if decision["approved_commit"] != source_head:
            raise SystemExit("task branch HEAD changed after coordinator approval")
        if decision["evidence_sha256"] != hashlib.sha256((path / "EVIDENCE.md").read_bytes()).hexdigest():
            raise SystemExit("EVIDENCE.md changed after coordinator approval")
        if decision["handoff_sha256"] != hashlib.sha256((path / "HANDOFF.json").read_bytes()).hexdigest():
            raise SystemExit("HANDOFF.json changed after coordinator approval")
        return source_head, source_branch, target_branch

    if profile != "direct" or status.get("state") not in {"verified", "merged"}:
        raise SystemExit("task merge requires an approved task or a verified Direct task")
    attempt_id = status.get("current_attempt_id")
    attempt = path / "attempts" / str(attempt_id)
    metadata = load_json(attempt / "ATTEMPT.json")
    if (
        metadata.get("state") != "completed"
        or metadata.get("handoff_valid") is not True
        or metadata.get("handoff_state") != "verified"
    ):
        raise SystemExit("verified Direct task does not have a valid completed attempt")
    verified_commit = metadata.get("verified_commit")
    if not isinstance(verified_commit, str) or not verified_commit:
        raise SystemExit("verified Direct task attempt is missing its verified Git commit")
    if verified_commit != source_head:
        raise SystemExit("Direct task branch HEAD changed after verified handoff")
    after_path = attempt / "runtime" / "worktree-after.json"
    if not after_path.exists():
        raise SystemExit("verified Direct task is missing worktree-after fingerprint")
    if load_json(after_path).get("sha256") != fingerprint(task_worktree).get("sha256"):
        raise SystemExit("Direct task worktree changed after verified handoff")
    return source_head, source_branch, target_branch


def existing_task_merged_event(path: Path, commit: str | None = None) -> dict[str, Any] | None:
    events_path = run_dir(path) / "EVENTS.ndjson"
    if not events_path.exists():
        return None
    task_id = load_json(path / "STATUS.json").get("task_id")
    matches: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if (
            record.get("event") == "task_merged"
            and record.get("task_id") == task_id
            and (commit is None or record.get("commit") == commit)
        ):
            matches.append(record)
    return matches[-1] if matches else None


def run_merge_verification(
    path: Path,
    target_worktree: Path,
    commands: list[str],
    timeout_seconds: float,
) -> dict[str, Any] | None:
    if not commands:
        return None
    if timeout_seconds <= 0:
        raise SystemExit("verification timeout must be positive")
    log_path = path / "logs" / "post-merge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    with log_path.open("a", encoding="utf-8") as log:
        for raw in commands:
            argv = shlex.split(raw)
            if not argv:
                raise SystemExit("post-merge verification command cannot be empty")
            log.write(f"\n[{utc_now()}] $ {shlex.join(argv)}\n")
            log.flush()
            try:
                completed = run_supervised(
                    argv,
                    timeout_seconds=timeout_seconds,
                    cwd=target_worktree,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    grace_seconds=0.5,
                )
                exit_code = completed.exit_code
                timed_out = completed.timed_out
                surviving_pids = list(completed.surviving_pids)
                elapsed_seconds = completed.elapsed_seconds
                if timed_out:
                    log.write(f"command timed out after {timeout_seconds:g} seconds\n")
                if surviving_pids:
                    log.write(f"command left surviving processes: {surviving_pids}\n")
            except OSError as exc:
                exit_code = 127
                timed_out = False
                surviving_pids = []
                elapsed_seconds = 0.0
                log.write(f"command could not start: {exc}\n")
            result = {
                "command": argv,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "elapsed_seconds": elapsed_seconds,
                "surviving_pids": surviving_pids,
            }
            results.append(result)
            if exit_code != 0 or surviving_pids:
                break
    return {
        "passed": all(
            item["exit_code"] == 0 and not item["surviving_pids"]
            for item in results
        ),
        "results": results,
        "log": log_path.relative_to(path).as_posix(),
    }


def task_merge(args: argparse.Namespace) -> int:
    path = task_dir(args.task_dir)
    status = load_json(path / "STATUS.json")
    if status.get("state") not in {"approved", "verified", "merged"}:
        raise SystemExit("task merge requires approved, verified, or already merged state")
    if (path / ".dispatch-lock").exists():
        raise SystemExit("task merge is forbidden while a dispatch lock exists")

    root = repo_root(path)
    run = load_json(run_dir(path) / "RUN.json")
    configured_target = run.get("target_branch")
    if not isinstance(configured_target, str) or not configured_target:
        raise SystemExit("RUN.json target_branch is missing")
    target_worktree = Path(args.target_worktree).resolve()
    if not target_worktree.is_dir():
        raise SystemExit(f"target worktree does not exist: {target_worktree}")
    require_same_repository(root, target_worktree)
    require_clean_target_worktree(target_worktree, configured_target)

    if status.get("state") == "merged":
        recorded = existing_task_merged_event(path)
        if recorded is not None:
            source_commit = recorded.get("commit")
            if not isinstance(source_commit, str) or not source_commit:
                raise SystemExit("task_merged event is missing commit")
            if recorded.get("target_branch") != configured_target:
                raise SystemExit("task_merged target branch does not match RUN.json")
            if args.expected_commit:
                expected = git_output(root, "rev-parse", args.expected_commit)
                if expected != source_commit:
                    raise SystemExit(
                        f"expected commit {expected} does not match merged commit {source_commit}"
                    )
            target_head = git_output(target_worktree, "rev-parse", "HEAD")
            if subprocess.run(
                ["git", "merge-base", "--is-ancestor", source_commit, target_head],
                cwd=target_worktree,
                check=False,
            ).returncode != 0:
                raise SystemExit("STATUS is merged but target branch does not contain the merged commit")
            print(json.dumps(recorded, indent=2))
            verification = recorded.get("verification")
            return 0 if not isinstance(verification, dict) or verification.get("passed") is not False else 1

    source_commit, source_branch, target_branch = merge_source_commit(path, status, root)
    if args.expected_commit:
        expected = git_output(root, "rev-parse", args.expected_commit)
        if expected != source_commit:
            raise SystemExit(
                f"expected commit {expected} does not match approved source commit {source_commit}"
            )
    target_head = git_output(target_worktree, "rev-parse", "HEAD")
    contains_source = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_commit, target_head],
        cwd=target_worktree,
        check=False,
    ).returncode == 0
    if not contains_source:
        fast_forwardable = subprocess.run(
            ["git", "merge-base", "--is-ancestor", target_head, source_commit],
            cwd=target_worktree,
            check=False,
        ).returncode == 0
        if not fast_forwardable:
            raise SystemExit("task commit cannot be fast-forward merged into the target branch")
        merge = subprocess.run(
            ["git", "merge", "--ff-only", source_commit],
            cwd=target_worktree,
            text=True,
            capture_output=True,
            check=False,
        )
        if merge.returncode != 0:
            detail = merge.stderr.strip() or merge.stdout.strip()
            raise SystemExit(f"git merge --ff-only failed: {detail}")
        target_head = git_output(target_worktree, "rev-parse", "HEAD")
        if subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_commit, target_head],
            cwd=target_worktree,
            check=False,
        ).returncode != 0:
            raise SystemExit("target branch does not contain the task commit after merge")

    verification = run_merge_verification(
        path,
        target_worktree,
        list(args.verify_command),
        float(args.verification_timeout),
    )
    if status.get("state") != "merged":
        transition(path, "merged", "coordinator")
    if verification is not None:
        updated = load_json(path / "STATUS.json")
        evidence = updated.setdefault("evidence", {})
        commands_run = evidence.setdefault("commands_run", [])
        for result in verification["results"]:
            rendered = shlex.join(result["command"])
            if rendered not in commands_run:
                commands_run.append(rendered)
        logs = evidence.setdefault("logs", [])
        if verification["log"] not in logs:
            logs.append(verification["log"])
        evidence["passed"] = verification["passed"]
        write_json(path / "STATUS.json", updated)

    payload: dict[str, Any] = {
        "commit": source_commit,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "coordinator_id": args.coordinator,
    }
    if verification is not None:
        payload["verification"] = verification
    event(path, "task_merged", "coordinator", **payload)
    result = {
        "task_id": status.get("task_id"),
        "state": "merged",
        **payload,
    }
    print(json.dumps(result, indent=2))
    return 0 if verification is None or verification["passed"] else 1


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
    source_commit = None
    if profile == "direct" and args.state == "verified" and not getattr(args, "auto_derive", False):
        raise SystemExit("direct verified handoff requires rdo finalize")
    if getattr(args, "auto_derive", False):
        recorded = [item for item in command_events(attempt_path) if item.get("acceptance") is True]
        if recorded:
            commands = [" ".join(map(str, item.get("command", []))) for item in recorded]
        metadata = load_json(attempt_path / "ATTEMPT.json")
        cwd = metadata.get("runtime", {}).get("cwd") if isinstance(metadata.get("runtime"), dict) else None
        if isinstance(cwd, str) and cwd:
            cwd_path = Path(cwd).resolve()
            require_clean_task_worktree(cwd_path, str(status.get("branch") or ""))
            if profile == "direct" and args.state == "verified":
                source_commit = git_output(cwd_path, "rev-parse", "HEAD")
            derived_files = derive_task_changed_files(path, attempt_path, cwd_path)
            if files and sorted(set(files)) != derived_files:
                raise SystemExit(
                    "explicit --file values do not match the task worktree diff: "
                    f"explicit={sorted(set(files))}, derived={derived_files}"
                )
            files = derived_files
    if profile == "direct" and args.state == "verified" and not commands:
        raise SystemExit("direct verified handoff requires at least one recorded acceptance command")
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
    if source_commit is not None:
        request["source_commit"] = source_commit
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
        source_commit=source_commit,
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
    tasks = areas.add_parser("task").add_subparsers(dest="task_action", required=True)
    command = tasks.add_parser("review"); command.add_argument("--task-dir", required=True); command.add_argument("--decision", choices=("approved", "changes_requested", "failed"), required=True); command.add_argument("--reviewer", required=True); command.add_argument("--findings-file", required=True); command.add_argument("--note", action="append", default=[]); command.set_defaults(func=task_review)
    command = tasks.add_parser("merge"); command.add_argument("--task-dir", required=True); command.add_argument("--target-worktree", required=True); command.add_argument("--expected-commit", default=""); command.add_argument("--verify-command", action="append", default=[]); command.add_argument("--verification-timeout", type=float, default=300); command.add_argument("--coordinator", required=True); command.set_defaults(func=task_merge)
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
