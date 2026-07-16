#!/usr/bin/env python3
"""Narrow CLI for dispatch protocol operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from artifact_bundle import (
    ArtifactBundleError,
    artifact_binding,
    file_sha256,
    load_bundle,
    load_command_records,
    safe_ref,
    validate_required_output_bindings,
    validate_task_inputs_binding,
)
from protocol import (
    EventJournalError,
    append_event as append_event_line,
    artifact_protocol_version,
    load_json,
    parse_iso,
    read_event_journal,
    utc_now,
    write_json,
)
from task_contract import (
    ImmutableArtifactError,
    TaskContractError,
    assert_resume_inputs_unchanged,
    build_task_inputs_from_readiness,
    evaluate_task_readiness,
    parse_acceptance_markdown,
    parse_execution_policy,
    validate_task_inputs_payload,
    write_task_inputs_immutable,
)
from validation import (
    HandoffValidationResult,
    parse_exit_code,
    validate_attempt_schema,
    validate_event,
    validate_state_history,
    validate_task_profile_binding,
    validate_worker_handoff,
)


def _task_protocol_version(task_dir: Path) -> int:
    try:
        status = load_json(task_dir / "STATUS.json")
        version = artifact_protocol_version(task_dir, status)
    except Exception as exc:
        raise TaskContractError(f"cannot determine artifact protocol version: {exc}") from exc
    if version not in {1, 2}:
        raise TaskContractError("task has an unknown artifact protocol version")
    return version


def _load_merged_event(run_dir: Path, task_id: str) -> Mapping[str, Any] | None:
    try:
        records, _warning = read_event_journal(
            run_dir,
            tolerate_interrupted_tail=True,
        )
    except EventJournalError as exc:
        raise TaskContractError(f"cannot resolve dependencies: {exc}") from exc
    match: Mapping[str, Any] | None = None
    for event in records:
        if (
            event.get("event") == "task_merged"
            and event.get("task_id") == task_id
        ):
            match = event
    return match


def _dependency_resolver(run_dir: Path):
    tasks_dir = run_dir / "tasks"

    def resolve(task_id: str) -> Mapping[str, Any] | None:
        status_path = tasks_dir / task_id / "STATUS.json"
        if not status_path.exists():
            return None
        try:
            status = load_json(status_path)
        except Exception as exc:
            raise TaskContractError(
                f"dependency {task_id!r} STATUS.json is unreadable: {exc}"
            ) from exc
        if not isinstance(status, dict):
            raise TaskContractError(f"dependency {task_id!r} STATUS.json must be an object")
        event = _load_merged_event(run_dir, task_id)
        state = status.get("state")
        if (
            status.get("artifact_protocol_version") == 2
            and event is not None
            and (
                not isinstance(event.get("verification"), dict)
                or event["verification"].get("passed") is not True
            )
        ):
            state = "merged_unverified"
        return {
            "state": state,
            "commit": event.get("commit") if event is not None else None,
        }

    return resolve


def _readiness(task_dir: Path, run_dir: Path, task_id: str, profile: str):
    try:
        status = load_json(task_dir / "STATUS.json")
    except Exception as exc:
        raise TaskContractError(f"STATUS.json is unreadable: {exc}") from exc
    if not isinstance(status, dict):
        raise TaskContractError("STATUS.json must contain a JSON object")
    try:
        events, _warning = read_event_journal(
            run_dir,
            tolerate_interrupted_tail=True,
        )
    except EventJournalError as exc:
        raise TaskContractError(f"EVENTS.ndjson is invalid: {exc}") from exc
    binding_errors = validate_task_profile_binding(status, events, task_id)
    if binding_errors:
        raise TaskContractError(binding_errors[0])
    declared_profile = status.get("profile")
    if declared_profile != profile:
        raise TaskContractError(
            "requested execution profile "
            f"{profile!r} does not match explicit STATUS.profile {declared_profile!r}"
        )
    return evaluate_task_readiness(
        task_dir,
        task_id=task_id,
        profile=profile,
        dependency_resolver=_dependency_resolver(run_dir),
        context_root=run_dir.parents[2],
    )


def _git_output(repo_root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=repo_root,
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.output.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise TaskContractError(f"git {' '.join(args)} failed: {detail}") from exc


def _prior_task_inputs(task_dir: Path, current_attempt_id: str) -> list[dict[str, Any]]:
    candidates: list[tuple[str, dict[str, Any]]] = []
    attempts_dir = task_dir / "attempts"
    if not attempts_dir.exists():
        return []
    for path in attempts_dir.iterdir():
        if not path.is_dir() or path.name == current_attempt_id:
            continue
        inputs_path = path / "TASK_INPUTS.json"
        attempt_path = path / "ATTEMPT.json"
        if not inputs_path.exists() or not attempt_path.exists():
            continue
        try:
            binding = validate_task_inputs_binding(
                path,
                expected_attempt_id=path.name,
            )
            payload = validate_task_inputs_payload(binding.task_inputs)
        except Exception as exc:
            raise TaskContractError(
                f"prior v2 input snapshot is invalid ({inputs_path}): {exc}"
            ) from exc
        candidates.append((path.name, payload))
    candidates.sort(key=lambda item: item[0])
    return [payload for _, payload in candidates]


def _initial_task_base_commit(task_dir: Path, run_dir: Path, repo_root: Path) -> str:
    status = load_json(task_dir / "STATUS.json")
    run = load_json(run_dir / "RUN.json")
    if not isinstance(status, dict) or not isinstance(run, dict):
        raise TaskContractError("STATUS.json and RUN.json must be objects")
    task_branch = status.get("branch")
    target_branch = run.get("target_branch")
    if not isinstance(task_branch, str) or not task_branch:
        raise TaskContractError("STATUS.json branch is missing")
    if not isinstance(target_branch, str) or not target_branch:
        raise TaskContractError("RUN.json target_branch is missing")
    branch_exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{task_branch}"],
        cwd=repo_root,
        check=False,
    ).returncode == 0
    if branch_exists:
        return _git_output(repo_root, "merge-base", task_branch, target_branch)
    return _git_output(repo_root, "rev-parse", target_branch)


def _verify_dependencies_in_base(
    repo_root: Path,
    task_base_commit: str,
    dependencies: Sequence[Mapping[str, str]],
) -> None:
    for dependency in dependencies:
        commit = dependency["commit"]
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, task_base_commit],
            cwd=repo_root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise TaskContractError(
                f"task base commit {task_base_commit} does not contain dependency "
                f"{dependency['task_id']} commit {commit}"
            )


def reset_status_to_active(status: dict[str, Any], active_state: str) -> None:
    """Discard worker-written state and restore the dispatch-owned active state."""

    history = status.get("state_history")
    if not isinstance(history, list):
        status["state_history"] = []
        status["state"] = active_state
        status["previous_state"] = None
        return
    running_index = None
    for idx in range(len(history) - 1, -1, -1):
        item = history[idx]
        if isinstance(item, dict) and item.get("to") == active_state and item.get("actor") == "dispatch":
            running_index = idx
            break
    if running_index is not None:
        del history[running_index + 1 :]
        item = history[running_index]
        status["previous_state"] = item.get("from")
    status["state"] = active_state


def apply_dispatch_terminal_transition(
    status_path: Path,
    *,
    target_state: str,
    actor: str = "dispatch",
    summary: str = "",
    needs_coordinator: bool = False,
    blocker_type: str = "",
    blocking_reason: str = "",
    commands_run: list[Any] | None = None,
    trusted_status: Mapping[str, Any] | None = None,
) -> None:
    status = load_json(status_path)
    if not isinstance(status, dict):
        raise ValueError("STATUS.json must be a JSON object")
    trusted_status = trusted_status or {}
    for field in (
        "task_id",
        "artifact_protocol_version",
        "profile",
        "branch",
        "worktree",
        "current_attempt_id",
    ):
        if trusted_status.get(field) is not None:
            status[field] = trusted_status[field]
    current_state = status.get("state")
    trusted_active = trusted_status.get("active_state")
    active_state = (
        str(trusted_active)
        if trusted_active in {"planning", "running"}
        else current_state if current_state in {"planning", "running"} else "running"
    )
    reset_status_to_active(status, active_state)
    now = utc_now()
    status["previous_state"] = active_state
    status["state"] = target_state
    status["updated_at"] = now
    status["owner"] = "worker"
    status["summary"] = summary or status.get("summary", "")
    status["needs_coordinator"] = needs_coordinator
    status["blocker_type"] = blocker_type
    status["blocking_reason"] = blocking_reason
    evidence = status.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {"commands_run": [], "logs": [], "passed": None}
    if commands_run is not None:
        evidence["commands_run"] = [str(command) for command in commands_run]
    status["evidence"] = evidence
    status.setdefault("state_history", []).append({
        "from": active_state,
        "to": target_state,
        "actor": actor,
        "at": now,
    })
    write_json(status_path, status)


def add_common_event_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)


def cmd_check_dispatch_transition(args: argparse.Namespace) -> int:
    status = load_json(Path(args.status_path))
    fsm = load_json(Path(args.fsm_path))
    state = status.get("state")
    target = "planning" if args.phase == "planning" else "running"
    allowed = fsm["transitions"].get(state, {}).get(target, [])
    if "dispatch" not in allowed:
        raise SystemExit(f"illegal dispatch transition: {state!r} -> {target!r}")
    return 0


def cmd_task_protocol_version(args: argparse.Namespace) -> int:
    try:
        version = _task_protocol_version(Path(args.task_dir))
    except TaskContractError as exc:
        raise SystemExit(str(exc)) from exc
    print(version)
    return 0


def cmd_check_task_readiness(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        version = _task_protocol_version(task_dir)
        if version == 1:
            payload = {"artifact_protocol_version": 1, "ready": True, "legacy": True}
        else:
            result = _readiness(
                task_dir,
                Path(args.run_dir).resolve(),
                args.task_id,
                args.profile,
            )
            result.require_ready()
            payload = {
                "artifact_protocol_version": 2,
                "ready": True,
                "resolved_dependencies": list(result.resolved_dependencies),
            }
    except TaskContractError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(payload, sort_keys=True))
    return 0


def cmd_freeze_task_inputs(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    run_dir = Path(args.run_dir).resolve()
    repo_root = Path(args.repo_root).resolve()
    attempt_dir = Path(args.attempt_dir).resolve()
    try:
        if _task_protocol_version(task_dir) != 2:
            raise TaskContractError("freeze-task-inputs is only valid for artifact protocol v2")
        readiness = _readiness(task_dir, run_dir, args.task_id, args.profile)
        readiness.require_ready()
        prior = _prior_task_inputs(task_dir, args.attempt_id)
        task_base_commit = (
            prior[0]["task_base_commit"]
            if prior
            else _initial_task_base_commit(task_dir, run_dir, repo_root)
        )
        _verify_dependencies_in_base(
            repo_root,
            task_base_commit,
            readiness.resolved_dependencies,
        )
        payload = build_task_inputs_from_readiness(
            readiness,
            task_id=args.task_id,
            attempt_id=args.attempt_id,
            task_base_commit=task_base_commit,
            generated_at=utc_now(),
        )
        for previous in prior:
            assert_resume_inputs_unchanged(previous, payload)
        path = attempt_dir / "TASK_INPUTS.json"
        digest = write_task_inputs_immutable(path, payload)
    except (TaskContractError, ImmutableArtifactError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "path": str(path),
                "sha256": digest,
                "contract_sha256": payload["contract_sha256"],
                "task_base_commit": task_base_commit,
            },
            sort_keys=True,
        )
    )
    return 0


def cmd_append_event(args: argparse.Namespace) -> int:
    dispatch_events = {
        "task_dispatched",
        "worker_process_started",
        "prompt_dispatched",
        "worker_started",
        "worker_waiting_for_user",
        "worker_startup_failed",
        "worker_exit_without_valid_status",
        "worker_blocked",
        "worker_review_ready",
        "worker_verified",
        "strategy_review_ready",
    }
    payload: dict[str, Any] = {
        "at": utc_now(),
        "actor": "dispatch" if args.event_name in dispatch_events else "coordinator",
        "event": args.event_name,
        "run_id": args.run_id,
        "task_id": args.task_id,
        "attempt_id": args.attempt_id,
    }
    if args.event_name == "task_dispatched":
        payload["worker"] = args.agent_name
        payload["worker_backend"] = getattr(args, "worker_backend", "")
        status = load_json(Path(args.status_path))
        assigned = status.get("assigned_worker") or {}
        payload["worker_id"] = assigned.get("worker_id", "")
        payload["execution_mode"] = getattr(args, "execution_mode", "")
    if args.event_name == "worker_blocked":
        status = load_json(Path(args.status_path))
        payload["blocker_type"] = status.get("blocker_type", "")
        payload["blocking_reason"] = status.get("blocking_reason", "")
    append_event_line(Path(args.run_dir), payload)
    return 0


def cmd_create_attempt(args: argparse.Namespace) -> int:
    command = args.command
    command_parts = shlex.split(command)
    runtime: dict[str, Any] = {
        "backend": args.runtime_backend,
        "runtime_backend": args.runtime_backend,
        "io_mode": args.io_mode,
        "model": os.environ.get("CLAUDE_MODEL"),
        "cli": command_parts[0] if command_parts else command,
        "command": command,
        "cwd": args.cwd,
    }
    if args.supervisor_command:
        runtime["supervisor_command"] = args.supervisor_command
    if args.runtime_backend == "tmux":
        runtime["tmux_session"] = args.tmux_session
        runtime["attach_command"] = args.attach_command

    payload = {
        "attempt_id": args.attempt_id,
        "task_id": args.task_id,
        "role": "worker",
        "phase": args.phase,
        "strategy_id": args.strategy_id or None,
        "strategy_revision": int(args.strategy_revision) if args.strategy_revision else None,
        "strategy_sha256": args.strategy_sha256 or None,
        "backend_profile_sha256": args.backend_profile_sha256 or None,
        "backend_settings_sha256": args.backend_settings_sha256 or None,
        "read_policy_sha256": args.read_policy_sha256 or None,
        "backend_id": args.worker_backend,
        "agent": args.worker_backend,
        "agent_name": args.agent_name,
        "backend_session_id": args.session_id,
        "session_id": args.session_id,
        "worker_id": args.worker_id,
        "parent_attempt_id": args.parent_attempt_id or None,
        "execution_mode": args.execution_mode,
        "resume_reason": args.resume_reason,
        "permission_mode": args.permission_mode,
        "state": "created",
        "handoff_valid": None,
        "handoff_state": None,
        "started_at": utc_now(),
        "ended_at": None,
        "exit_code": None,
        "runtime": runtime,
    }
    if args.artifact_protocol_version == 2:
        if args.task_inputs_ref != "TASK_INPUTS.json":
            raise SystemExit("v2 ATTEMPT.task_inputs_ref must be 'TASK_INPUTS.json'")
        if not isinstance(args.task_inputs_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", args.task_inputs_sha256
        ):
            raise SystemExit("v2 ATTEMPT.task_inputs_sha256 must be a SHA-256 digest")
        inputs_path = Path(args.path).parent / args.task_inputs_ref
        try:
            validate_task_inputs_payload(load_json(inputs_path))
            actual_digest = hashlib.sha256(inputs_path.read_bytes()).hexdigest()
        except Exception as exc:
            raise SystemExit(f"cannot bind TASK_INPUTS.json: {exc}") from exc
        if actual_digest != args.task_inputs_sha256:
            raise SystemExit("ATTEMPT task_inputs_sha256 does not match TASK_INPUTS.json")
        payload.update(
            {
                "schema_version": 2,
                "artifact_protocol_version": 2,
                "task_inputs_ref": args.task_inputs_ref,
                "task_inputs_sha256": args.task_inputs_sha256,
            }
        )
    elif args.artifact_protocol_version != 1:
        raise SystemExit("artifact protocol version must be 1 or 2")
    write_json(Path(args.path), payload)
    return 0


def cmd_transition_running(args: argparse.Namespace) -> int:
    status_path = Path(args.status_path)
    status = load_json(status_path)
    fsm = load_json(Path(args.fsm_path))
    state = status.get("state")
    target = "planning" if args.phase == "planning" else "running"
    allowed = fsm["transitions"].get(state, {}).get(target, [])
    if "dispatch" not in allowed:
        raise SystemExit(f"illegal dispatch transition: {state!r} -> {target!r}")
    now = utc_now()
    status["previous_state"] = state
    status["state"] = target
    status["owner"] = "worker"
    status["updated_at"] = now
    status["needs_coordinator"] = False
    status["blocking_reason"] = ""
    status["blocker_type"] = ""
    status["current_attempt_id"] = args.attempt_id
    previous_worker = status.get("assigned_worker") if isinstance(status.get("assigned_worker"), dict) else {}
    status["assigned_worker"] = {
        "worker_id": args.worker_id,
        "backend_id": args.worker_backend,
        "agent": args.worker_backend,
        "agent_name": args.agent_name,
        "backend_session_id": args.session_id,
        "session_id": args.session_id,
        "first_attempt_id": previous_worker.get("first_attempt_id") or args.attempt_id,
        "latest_attempt_id": args.attempt_id,
        "role": "worker",
    }
    status.setdefault("state_history", []).append({
        "from": state,
        "to": target,
        "actor": "dispatch",
        "at": now,
    })
    write_json(status_path, status)
    return 0


def cmd_set_attempt_running(args: argparse.Namespace) -> int:
    path = Path(args.attempt_path)
    attempt = load_json(path)
    attempt["state"] = "running"
    write_json(path, attempt)
    return 0


def cmd_record_session(args: argparse.Namespace) -> int:
    session_path = Path(args.session_path)
    if not session_path.exists():
        return 0
    session = load_json(session_path)
    session_id = session.get("session_id") if isinstance(session, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return 0
    attempt_path = Path(args.attempt_path)
    attempt = load_json(attempt_path)
    attempt["backend_session_id"] = session_id
    attempt["session_id"] = session_id
    write_json(attempt_path, attempt)
    status_path = Path(args.status_path)
    status = load_json(status_path)
    assigned = status.get("assigned_worker")
    if isinstance(assigned, dict) and assigned.get("worker_id") == attempt.get("worker_id"):
        assigned["backend_session_id"] = session_id
        assigned["session_id"] = session_id
        write_json(status_path, status)
    return 0


def _v2_governance_reasons(
    attempt_dir: Path,
    attempt: Mapping[str, Any],
    expected_dispatch: Mapping[str, Any] | None = None,
) -> list[str]:
    reasons: list[str] = []
    expected_dispatch = expected_dispatch or {}
    profile_path = attempt_dir / "runtime" / "BACKEND_PROFILE.json"
    profile: Mapping[str, Any] | None = None
    expected_profile_sha = expected_dispatch.get("backend_profile_sha256")
    if expected_profile_sha and attempt.get("backend_profile_sha256") != expected_profile_sha:
        reasons.append("ATTEMPT backend profile digest differs from the dispatcher-frozen value")
    if attempt.get("backend_profile_sha256"):
        try:
            loaded = load_json(profile_path)
            if not isinstance(loaded, dict):
                raise ValueError("profile must be an object")
            unsigned = dict(loaded)
            declared = unsigned.pop("profile_sha256", None)
            from strategy import canonical_digest

            actual = canonical_digest(unsigned)
            profile = loaded
            if declared != actual or actual != attempt.get("backend_profile_sha256"):
                reasons.append("backend profile digest changed during the attempt")
        except Exception as exc:
            reasons.append(f"backend profile is unreadable during handoff: {exc}")
    elif expected_profile_sha:
        reasons.append("ATTEMPT backend profile digest is missing")

    if isinstance(profile, Mapping):
        expected_profile = expected_dispatch.get("profile")
        expected_phase = expected_dispatch.get("phase")
        expected_backend = expected_dispatch.get("worker_backend")
        if expected_profile and profile.get("task_profile") != expected_profile:
            reasons.append("backend profile task profile differs from dispatch")
        if expected_phase and profile.get("phase") != expected_phase:
            reasons.append("backend profile phase differs from dispatch")
        if expected_backend and profile.get("backend_id") != expected_backend:
            reasons.append("backend profile backend_id differs from dispatch")

        for field in ("strategy_id", "strategy_revision", "strategy_sha256"):
            expected = expected_dispatch.get(field)
            if expected is not None and profile.get(field) != expected:
                reasons.append(
                    f"backend profile {field} differs from the dispatcher-frozen strategy"
                )

    for field in ("strategy_id", "strategy_revision", "strategy_sha256"):
        expected = expected_dispatch.get(field)
        if expected is not None and attempt.get(field) != expected:
            reasons.append(
                f"ATTEMPT {field} differs from the dispatcher-frozen strategy"
            )

    expected_settings_sha = expected_dispatch.get("backend_settings_sha256") or None
    if expected_settings_sha and attempt.get("backend_settings_sha256") != expected_settings_sha:
        reasons.append("ATTEMPT backend settings digest differs from the dispatcher-frozen value")
    if attempt.get("backend_settings_sha256"):
        generated = profile.get("generated_files", []) if isinstance(profile, Mapping) else []
        settings_files = [
            item
            for item in generated
            if isinstance(item, str) and item and item != "READ_POLICY.json"
        ]
        settings_path = attempt_dir / "runtime" / (
            settings_files[0] if len(settings_files) == 1 else "claude-settings.json"
        )
        try:
            actual = hashlib.sha256(settings_path.read_bytes()).hexdigest()
        except OSError as exc:
            reasons.append(f"backend settings are unreadable during handoff: {exc}")
        else:
            if actual != attempt.get("backend_settings_sha256"):
                reasons.append("backend settings changed during the attempt")
    elif expected_settings_sha:
        reasons.append("ATTEMPT backend settings digest is missing")

    expected_read_policy_sha = expected_dispatch.get("read_policy_sha256")
    if expected_read_policy_sha and attempt.get("read_policy_sha256") != expected_read_policy_sha:
        reasons.append("ATTEMPT read policy digest differs from the dispatcher-frozen value")
    if attempt.get("read_policy_sha256"):
        try:
            actual = hashlib.sha256(
                (attempt_dir / "runtime" / "READ_POLICY.json").read_bytes()
            ).hexdigest()
        except OSError as exc:
            reasons.append(f"read policy is unreadable during handoff: {exc}")
        else:
            if actual != attempt.get("read_policy_sha256"):
                reasons.append("read policy changed during the attempt")
    elif expected_read_policy_sha:
        reasons.append("ATTEMPT read policy digest is missing")
    violations_path = attempt_dir / "runtime" / "VIOLATIONS.ndjson"
    if violations_path.exists():
        try:
            violations = [
                json.loads(line)
                for line in violations_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            reasons.append(f"backend governance violations are unreadable: {exc}")
        else:
            hard = [
                item
                for item in violations
                if isinstance(item, dict) and item.get("hard") is True
            ]
            if hard:
                reasons.append(f"attempt has {len(hard)} hard backend governance violation(s)")
    return reasons


def _v2_full_completion_reasons(task_dir: Path, attempt_dir: Path) -> list[str]:
    reasons: list[str] = []
    try:
        approved = _v2_bound_strategy(task_dir, attempt_dir)
        records_path = attempt_dir / "runtime" / "WORKFLOWS.ndjson"
        records = []
        if records_path.exists():
            records = [
                json.loads(line)
                for line in records_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        if approved["completion_gate"]["required_workflows_complete"]:
            completed = {
                record.get("workflow_id")
                for record in records
                if record.get("event") in {"workflow_completed", "workflow_carried_forward"}
            }
            missing = sorted(
                workflow["workflow_id"]
                for workflow in approved["workflows"]
                if workflow["required"] and workflow["workflow_id"] not in completed
            )
            if missing:
                reasons.append(f"required workflows are incomplete: {missing}")
        if not approved["completion_gate"]["optional_workflows_may_timeout"] and any(
            record.get("event") == "workflow_timed_out" for record in records
        ):
            reasons.append("workflow timeout is forbidden by the completion gate")
    except Exception as exc:
        reasons.append(f"review handoff cannot validate approved strategy completion: {exc}")
    return reasons


def _v2_bound_strategy(task_dir: Path, attempt_dir: Path) -> dict[str, Any]:
    """Load a Full attempt's exact launch-bound approved strategy."""

    from strategy import load_bound_approved_strategy

    attempt = load_json(attempt_dir / "ATTEMPT.json")
    profile = load_json(attempt_dir / "runtime" / "BACKEND_PROFILE.json")
    if not isinstance(attempt, dict) or not isinstance(profile, dict):
        raise ValueError("attempt strategy metadata must be JSON objects")
    for field in ("strategy_id", "strategy_revision", "strategy_sha256"):
        if attempt.get(field) != profile.get(field):
            raise ValueError(f"ATTEMPT.{field} does not match BACKEND_PROFILE.json")
    strategy, _review = load_bound_approved_strategy(
        task_dir,
        strategy_id=attempt.get("strategy_id"),
        strategy_sha256=attempt.get("strategy_sha256"),
        revision=attempt.get("strategy_revision"),
    )
    return strategy


def _v2_frozen_acceptance(task_dir: Path, bundle: Any) -> dict[str, Any]:
    """Revalidate task-root canonical inputs against the exact attempt snapshot."""

    inputs = validate_task_inputs_payload(bundle.task_inputs_binding.task_inputs)
    canonical = {
        "task": "TASK.md",
        "context": "CONTEXT.md",
        "acceptance": "ACCEPTANCE.md",
        "execution_policy": "EXECUTION_POLICY.json",
    }
    root = task_dir.resolve()
    for name, filename in canonical.items():
        descriptor = inputs["inputs"][name]
        raw_path = task_dir / filename
        resolved = raw_path.resolve()
        if descriptor["ref"] != filename or resolved.parent != root:
            raise TaskContractError(
                f"TASK_INPUTS.json {filename} ref does not identify the canonical task input"
            )
        if raw_path.is_symlink() or not resolved.is_file():
            raise TaskContractError(f"canonical task input is missing or unsafe: {filename}")
        if hashlib.sha256(resolved.read_bytes()).hexdigest() != descriptor["sha256"]:
            raise TaskContractError(
                f"task input contract drifted after dispatch: {filename}; create a revision task"
            )
    try:
        return parse_acceptance_markdown(
            (task_dir / "ACCEPTANCE.md").read_text(encoding="utf-8")
        )["contract"]
    except (OSError, UnicodeError, TaskContractError) as exc:
        raise TaskContractError(f"frozen ACCEPTANCE.md is invalid: {exc}") from exc


def _v2_log_binding_valid(attempt_dir: Path, record: Mapping[str, Any], prefix: str) -> bool:
    ref = record.get(f"{prefix}_ref")
    digest = record.get(f"{prefix}_sha256")
    if not isinstance(ref, str) or not isinstance(digest, str):
        return False
    try:
        return file_sha256(safe_ref(attempt_dir, ref)) == digest
    except ArtifactBundleError:
        return False


def _v2_acceptance_reasons(
    task_dir: Path,
    attempt_dir: Path,
    bundle: Any,
    *,
    worktree: Path | None,
    expected_branch: str | None,
    profile: str,
) -> list[str]:
    """Independently enforce frozen checks/outputs after bundle publication."""

    reasons: list[str] = []
    try:
        contract = _v2_frozen_acceptance(task_dir, bundle)
        records = load_command_records(attempt_dir, required=False)
    except (TaskContractError, ArtifactBundleError) as exc:
        return [f"frozen acceptance evidence is invalid: {exc}"]
    by_id = {record.record_id: record.payload for record in records}
    indexed = bundle.evidence.get("command_records")
    if not isinstance(indexed, list):
        return ["EVIDENCE.json command_records must be an array"]
    binding = bundle.task_inputs_binding
    acceptance_sha256 = binding.task_inputs["inputs"]["acceptance"]["sha256"]

    worktree_evidence = bundle.evidence.get("worktree")
    expected_snapshots = {
        "before": "runtime/worktree-before.json",
        "after": "runtime/worktree-after.json",
    }
    if not isinstance(worktree_evidence, dict) or any(
        not isinstance(worktree_evidence.get(name), dict)
        or worktree_evidence[name].get("ref") != ref
        for name, ref in expected_snapshots.items()
    ):
        reasons.append("EVIDENCE.json must bind before/after worktree snapshots")
    for definition in contract["required_commands"]:
        passing = False
        for index in indexed:
            if not isinstance(index, dict):
                continue
            record = by_id.get(index.get("record_id"))
            if not isinstance(record, dict):
                continue
            if not (
                record.get("artifact_protocol_version") == 2
                and record.get("schema_version") == 2
                and record.get("task_id") == binding.task_id
                and record.get("attempt_id") == binding.attempt_id
                and record.get("task_inputs_sha256") == binding.task_inputs_sha256
                and record.get("acceptance_contract_sha256") == acceptance_sha256
                and record.get("category") == "required_commands"
                and record.get("check_id") == definition["id"]
                and record.get("argv") == definition["argv"]
                and record.get("cwd") == definition["cwd"]
                and record.get("timeout_seconds") == definition["timeout_seconds"]
                and record.get("exit_code") == 0
                and record.get("timed_out") is False
                and record.get("surviving_processes") == []
                and _v2_log_binding_valid(attempt_dir, record, "stdout")
                and _v2_log_binding_valid(attempt_dir, record, "stderr")
            ):
                continue
            passing = True
            break
        if not passing:
            reasons.append(
                f"required check {definition['id']!r} has no exact successful record "
                "for the frozen acceptance contract"
            )

    if worktree is None or not worktree.is_dir():
        reasons.append("required outputs cannot be checked without the task worktree")
        return reasons
    root = worktree.resolve()
    source_commit = bundle.handoff.get("source_commit")
    task_base_commit = binding.task_inputs.get("task_base_commit")
    if isinstance(source_commit, str):
        try:
            validate_required_output_bindings(
                root,
                source_commit,
                bundle.evidence.get("required_outputs"),
                expected_paths=contract["required_outputs"],
            )
        except ArtifactBundleError as exc:
            reasons.append(f"required output binding is invalid: {exc}")
    if isinstance(source_commit, str) and isinstance(task_base_commit, str):
        changed = subprocess.run(
            [
                "git",
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                task_base_commit,
                source_commit,
                "--",
            ],
            cwd=root,
            check=False,
            capture_output=True,
        )
        if changed.returncode != 0:
            reasons.append(
                "cannot verify evidence changed_paths: "
                + changed.stderr.decode("utf-8", errors="replace").strip()
            )
        else:
            actual_paths = sorted(
                item.decode("utf-8", errors="surrogateescape")
                for item in changed.stdout.split(b"\0")
                if item
            )
            if bundle.evidence.get("changed_paths") != actual_paths:
                reasons.append("EVIDENCE.json changed_paths does not match frozen Git commits")
            try:
                policy = parse_execution_policy(
                    (task_dir / "EXECUTION_POLICY.json").read_bytes(),
                    profile=profile,
                )
                allowed = list(policy["allowed_paths"])
                if profile == "full":
                    strategy = _v2_bound_strategy(task_dir, attempt_dir)
                    allowed = sorted(
                        {
                            path
                            for workflow in strategy["workflows"]
                            if workflow["executor"]["write_access"]
                            for path in workflow["executor"]["allowed_paths"]
                        }
                    )
                forbidden = list(policy["forbidden_paths"])

                def contains(parent: str, child: str) -> bool:
                    parent = parent.replace("\\", "/").rstrip("/") or "."
                    child = child.replace("\\", "/").rstrip("/") or "."
                    return parent == "." or child == parent or child.startswith(parent + "/")

                violations = [
                    path
                    for path in actual_paths
                    if not any(contains(root_path, path) for root_path in allowed)
                    or any(contains(root_path, path) for root_path in forbidden)
                ]
                if violations:
                    reasons.append(
                        "frozen Git diff violates the execution write policy: "
                        f"{sorted(violations)}"
                    )
            except Exception as exc:
                reasons.append(f"cannot validate frozen execution write policy: {exc}")
    try:
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.output.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        reasons.append(f"cannot verify clean task worktree: {detail}")
    else:
        if dirty:
            reasons.append("verified/review handoff requires a clean task worktree")
    if isinstance(expected_branch, str) and expected_branch:
        try:
            actual_branch = subprocess.check_output(
                ["git", "branch", "--show-current"],
                cwd=root,
                text=True,
                stderr=subprocess.STDOUT,
            ).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = exc.output.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            reasons.append(f"cannot verify task worktree branch: {detail}")
        else:
            if actual_branch != expected_branch:
                reasons.append(
                    f"task worktree branch {actual_branch!r} does not match {expected_branch!r}"
                )
    return reasons


def _v2_planning_worktree_reasons(
    attempt_dir: Path,
    bundle: Any,
    worktree: Path | None,
    expected_base: Any,
) -> list[str]:
    """Enforce that Full planning remains byte-, mode-, and commit-read-only."""

    reasons: list[str] = []
    task_base = expected_base or bundle.task_inputs_binding.task_inputs.get("task_base_commit")
    source_commit = bundle.handoff.get("source_commit")
    if source_commit != task_base:
        reasons.append("planning handoff source_commit differs from the frozen task base")
    if worktree is None or not worktree.is_dir():
        reasons.append("planning handoff cannot verify the task worktree")
        return reasons
    try:
        live_head = _git_output(worktree, "rev-parse", "HEAD")
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=worktree,
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (TaskContractError, OSError, subprocess.CalledProcessError) as exc:
        detail = exc.output.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        reasons.append(f"cannot verify planning worktree: {detail}")
    else:
        if live_head != task_base:
            reasons.append("planning worktree HEAD differs from the frozen task base")
        if dirty:
            reasons.append("planning handoff requires a clean task worktree")
    try:
        before = load_json(attempt_dir / "runtime" / "worktree-before.json")
        after = load_json(attempt_dir / "runtime" / "worktree-after.json")
    except Exception as exc:
        reasons.append(f"planning worktree snapshots are unreadable: {exc}")
    else:
        if before != after:
            reasons.append("planning attempt changed worktree content or file modes")
    return reasons


def _v2_execution_strategy_reasons(
    bundle: Any,
    worktree: Path | None,
) -> list[str]:
    """Validate a Full execution attempt that pauses for strategy revision."""

    reasons: list[str] = []
    if worktree is None or not worktree.is_dir():
        return ["execution strategy handoff cannot verify the task worktree"]
    source_commit = bundle.handoff.get("source_commit")
    task_base = bundle.task_inputs_binding.task_inputs.get("task_base_commit")
    try:
        live_head = _git_output(worktree, "rev-parse", "HEAD")
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=worktree,
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (TaskContractError, OSError, subprocess.CalledProcessError) as exc:
        detail = exc.output.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        return [f"cannot verify execution strategy worktree: {detail}"]
    if live_head != source_commit:
        reasons.append("execution strategy handoff source_commit differs from worktree HEAD")
    if dirty:
        reasons.append("execution strategy handoff requires a clean task worktree")
    if isinstance(source_commit, str) and isinstance(task_base, str):
        changed = subprocess.run(
            [
                "git",
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                task_base,
                source_commit,
                "--",
            ],
            cwd=worktree,
            capture_output=True,
            check=False,
        )
        if changed.returncode != 0:
            reasons.append("cannot derive execution strategy changed paths")
        else:
            paths = sorted(
                item.decode("utf-8", errors="surrogateescape")
                for item in changed.stdout.split(b"\0")
                if item
            )
            if bundle.evidence.get("changed_paths") != paths:
                reasons.append(
                    "execution strategy evidence changed_paths do not match frozen Git commits"
                )
    return reasons


def _validate_v2_handoff(
    status: Any,
    *,
    task_dir: Path,
    attempt_id: str,
    exit_code_raw: str,
    worktree: Path | None,
    expected_dispatch: Mapping[str, Any] | None = None,
    allow_completed_attempt: bool = False,
) -> HandoffValidationResult:
    reasons: list[str] = []
    expected_dispatch = expected_dispatch or {}
    exit_code, exit_code_error = parse_exit_code(exit_code_raw)
    if exit_code_error:
        reasons.append(exit_code_error)
    attempt_dir = task_dir / "attempts" / attempt_id
    try:
        attempt = load_json(attempt_dir / "ATTEMPT.json")
    except Exception as exc:
        attempt = None
        reasons.append(f"ATTEMPT.json is unreadable during v2 handoff: {exc}")
    try:
        bundle = load_bundle(
            attempt_dir,
            expected_task_id=(
                str(expected_dispatch["task_id"])
                if expected_dispatch.get("task_id")
                else status.get("task_id") if isinstance(status, dict) else None
            ),
            expected_attempt_id=attempt_id,
        )
    except ArtifactBundleError as exc:
        bundle = None
        reasons.append(f"v2 handoff bundle is invalid: {exc}")
    request = bundle.handoff if bundle is not None else None
    requested_state = request.get("requested_state") if isinstance(request, dict) else None
    if bundle is not None:
        binding = bundle.task_inputs_binding
        expected_inputs_sha = expected_dispatch.get("task_inputs_sha256")
        if expected_inputs_sha and binding.task_inputs_sha256 != expected_inputs_sha:
            reasons.append("TASK_INPUTS.json changed after dispatcher freeze")
        expected_base = expected_dispatch.get("task_base_commit")
        if expected_base and binding.task_inputs.get("task_base_commit") != expected_base:
            reasons.append("TASK_INPUTS task_base_commit changed after dispatcher freeze")
        expected_before_sha = expected_dispatch.get("worktree_before_sha256")
        if expected_before_sha:
            before_path = attempt_dir / "runtime" / "worktree-before.json"
            try:
                actual_before_sha = hashlib.sha256(before_path.read_bytes()).hexdigest()
            except OSError as exc:
                reasons.append(f"worktree-before snapshot is unreadable: {exc}")
            else:
                if actual_before_sha != expected_before_sha:
                    reasons.append("worktree-before snapshot changed after worker launch")

    if not isinstance(status, dict):
        reasons.append("STATUS.json must be a JSON object")
    else:
        state = status.get("state")
        allowed_active_states = (
            {"running"} if requested_state in {"verified", "review"} else {"planning", "running"}
        )
        if allow_completed_attempt and requested_state in {
            "strategy_review",
            "verified",
            "review",
            "blocked",
        }:
            allowed_active_states.add(str(requested_state))
        if state not in allowed_active_states:
            reasons.append(
                f"STATUS.state must remain active until dispatch applies handoff, got {state!r}"
            )
        if status.get("current_attempt_id") != attempt_id:
            reasons.append("STATUS.current_attempt_id does not match the supervised attempt")
        if expected_dispatch.get("task_id") and status.get("task_id") != expected_dispatch["task_id"]:
            reasons.append("STATUS.task_id changed after dispatch")
        if (
            expected_dispatch.get("artifact_protocol_version")
            and status.get("artifact_protocol_version")
            != expected_dispatch["artifact_protocol_version"]
        ):
            reasons.append("STATUS.artifact_protocol_version changed after dispatch")
        if expected_dispatch.get("profile") and status.get("profile") != expected_dispatch["profile"]:
            reasons.append("STATUS.profile changed after dispatch")
        if expected_dispatch.get("branch") and status.get("branch") != expected_dispatch["branch"]:
            reasons.append("STATUS.branch changed after dispatch")
        history = status.get("state_history") if isinstance(status.get("state_history"), list) else []
        last = history[-1] if history else None
        if not (
            isinstance(last, dict)
            and last.get("to") == state
            and last.get("actor") == "dispatch"
        ):
            reasons.append("state_history does not end with the active dispatch transition")

    if isinstance(attempt, dict):
        reasons.extend(_v2_governance_reasons(attempt_dir, attempt, expected_dispatch))
        completed_recovery = (
            allow_completed_attempt
            and attempt.get("state") == "completed"
            and attempt.get("handoff_valid") is True
            and attempt.get("handoff_state") == requested_state
        )
        if attempt.get("state") not in {"created", "running"} and not completed_recovery:
            reasons.append("v2 handoff must reference an active or recoverable ATTEMPT.json")
        phase = attempt.get("phase")
        if expected_dispatch.get("phase") and phase != expected_dispatch["phase"]:
            reasons.append("ATTEMPT.phase changed after dispatch")
        if (
            expected_dispatch.get("worker_backend")
            and attempt.get("backend_id") != expected_dispatch["worker_backend"]
        ):
            reasons.append("ATTEMPT.backend_id changed after dispatch")
    else:
        phase = None

    expected_worktree = expected_dispatch.get("worktree")
    if expected_worktree and (
        worktree is None or worktree.resolve() != Path(str(expected_worktree)).resolve()
    ):
        reasons.append("task worktree identity changed after dispatch")

    task_profile = status.get("profile", "full") if isinstance(status, dict) else "full"
    if requested_state == "strategy_review":
        if phase not in {"planning", "execution"} or task_profile != "full":
            reasons.append("strategy_review requires a Full planning or execution attempt")
        if exit_code != 0:
            reasons.append(f"strategy_review handoff requires exit_code 0, got {exit_code!r}")
        if not list((task_dir / "strategy").glob("STRATEGY-v*.json")):
            reasons.append("strategy_review handoff has no immutable strategy revision")
        if bundle is not None and phase == "planning":
            reasons.extend(
                _v2_planning_worktree_reasons(
                    attempt_dir,
                    bundle,
                    worktree,
                    expected_dispatch.get("task_base_commit"),
                )
            )
        elif bundle is not None and phase == "execution":
            reasons.extend(_v2_execution_strategy_reasons(bundle, worktree))
    elif requested_state in {"verified", "review"}:
        if phase != "execution":
            reasons.append(f"{requested_state} handoff requires an execution attempt")
        expected = "verified" if task_profile == "direct" else "review"
        if requested_state != expected:
            reasons.append(f"profile {task_profile!r} requires {expected!r} handoff")
        if exit_code != 0:
            reasons.append(f"{requested_state} handoff requires exit_code 0, got {exit_code!r}")
        if requested_state == "verified" and isinstance(request, dict):
            review = request.get("direct_self_review", request.get("self_review"))
            review_passed = isinstance(review, dict) and (
                review.get("performed") is True or review.get("passed") is True
            )
            if not review_passed:
                reasons.append("Direct verified handoff requires a performed self-review")
        if bundle is not None:
            reasons.extend(
                _v2_acceptance_reasons(
                    task_dir,
                    attempt_dir,
                    bundle,
                    worktree=worktree,
                    expected_branch=(
                        status.get("branch") if isinstance(status, dict) else None
                    ),
                    profile=task_profile,
                )
            )
        if task_profile == "full":
            reasons.extend(_v2_full_completion_reasons(task_dir, attempt_dir))
    elif requested_state == "blocked":
        if phase not in {"planning", "execution"}:
            reasons.append("blocked handoff requires an active planning or execution attempt")
        if phase == "planning" and bundle is not None:
            reasons.extend(
                _v2_planning_worktree_reasons(
                    attempt_dir,
                    bundle,
                    worktree,
                    expected_dispatch.get("task_base_commit"),
                )
            )
    elif request is not None:
        reasons.append(f"unsupported v2 requested_state {requested_state!r}")

    return HandoffValidationResult(
        valid=not reasons,
        handoff_state=(
            requested_state
            if requested_state in {"strategy_review", "verified", "review", "blocked"}
            else None
        ),
        exit_code=exit_code,
        reasons=reasons,
        request=request,
    )


_COMPLETED_REPLAY_STATES = {
    "strategy_review": {"strategy_review", "changes_requested"},
    "review": {"review", "changes_requested", "approved", "merged"},
    "verified": {"verified", "merged"},
    "blocked": {"blocked"},
}


def _completed_replay_history_reasons(
    status: Mapping[str, Any],
    attempt: Mapping[str, Any],
    handoff_state: str,
) -> list[str]:
    """Prove that dispatch applied the handoff and coordinators own later states."""

    reasons: list[str] = []
    try:
        fsm = load_json(Path(__file__).resolve().parents[1] / "references" / "state-machine.json")
    except Exception as exc:
        return [f"completed replay cannot load the task state machine: {exc}"]
    reasons.extend(
        reason.replace("completed-replay: ", "completed replay ", 1)
        for reason in validate_state_history(dict(status), fsm, "completed-replay")
    )

    history = status.get("state_history")
    if not isinstance(history, list):
        return reasons
    phase = attempt.get("phase")
    active_state = "planning" if phase == "planning" else "running"
    dispatch_indices = [
        index
        for index, item in enumerate(history)
        if isinstance(item, dict)
        and item.get("from") == active_state
        and item.get("to") == handoff_state
        and item.get("actor") == "dispatch"
    ]
    if not dispatch_indices:
        reasons.append(
            f"completed replay has no {active_state}->{handoff_state} dispatch transition"
        )
        return reasons

    expected_suffixes: dict[tuple[str, str], list[tuple[str, str, str]]] = {
        ("strategy_review", "strategy_review"): [],
        ("strategy_review", "changes_requested"): [
            ("strategy_review", "changes_requested", "coordinator")
        ],
        ("review", "review"): [],
        ("review", "changes_requested"): [
            ("review", "changes_requested", "coordinator")
        ],
        ("review", "approved"): [("review", "approved", "coordinator")],
        ("review", "merged"): [
            ("review", "approved", "coordinator"),
            ("approved", "merged", "coordinator"),
        ],
        ("verified", "verified"): [],
        ("verified", "merged"): [("verified", "merged", "coordinator")],
        ("blocked", "blocked"): [],
    }
    current_state = status.get("state")
    expected_suffix = expected_suffixes.get((handoff_state, str(current_state)))
    if expected_suffix is None:
        reasons.append(
            f"completed replay has no valid provenance chain for {handoff_state}->{current_state}"
        )
        return reasons

    dispatch_index = dispatch_indices[-1]
    actual_suffix: list[tuple[Any, Any, Any]] = []
    malformed_suffix = False
    for item in history[dispatch_index + 1 :]:
        if not isinstance(item, dict):
            malformed_suffix = True
            break
        actual_suffix.append((item.get("from"), item.get("to"), item.get("actor")))
    if malformed_suffix or actual_suffix != expected_suffix:
        reasons.append(
            "completed replay downstream history is not the exact coordinator-owned chain "
            f"from {handoff_state!r} to {current_state!r}"
        )
    expected_owner = "worker" if current_state == handoff_state else "coordinator"
    if status.get("owner") != expected_owner:
        reasons.append(
            f"completed replay STATUS.owner must be {expected_owner!r} in state {current_state!r}"
        )
    return reasons


def _completed_replay_journal(
    task_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Load and schema-check coordinator evidence used by completed replay."""

    run_dir = task_dir.parent.parent
    reasons: list[str] = []
    try:
        run = load_json(run_dir / "RUN.json")
    except Exception as exc:
        return [], {}, [f"completed replay RUN.json is unreadable: {exc}"]
    if not isinstance(run, dict):
        return [], {}, ["completed replay RUN.json must be an object"]
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        reasons.append("completed replay RUN.json has no run_id")
        run_id = run_dir.name
    try:
        records, _warning = read_event_journal(
            run_dir,
            tolerate_interrupted_tail=True,
        )
    except EventJournalError as exc:
        return [], run, [f"completed replay event journal is invalid: {exc}"]
    for line_number, record in enumerate(records, start=1):
        event_reasons, _warnings = validate_event(record, str(run_id), line_number)
        reasons.extend(f"completed replay {reason}" for reason in event_reasons)
    return records, run, reasons


def _load_completed_task_review(
    task_dir: Path,
    *,
    task_id: str,
    expected_decision: str,
    expected_binding: Mapping[str, Any],
    expected_commit: str | None,
    expected_source_branch: Any,
    expected_target_branch: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate the immutable coordinator review selected by its digest pointer."""

    reasons: list[str] = []
    pointer_path = task_dir / "reviews" / "CURRENT_TASK_REVIEW.json"
    if pointer_path.is_symlink():
        return None, ["completed replay task review pointer must not be a symlink"]
    try:
        pointer = load_json(pointer_path)
    except Exception as exc:
        return None, [f"completed replay task review pointer is unreadable: {exc}"]
    if not isinstance(pointer, dict):
        return None, ["completed replay task review pointer must be an object"]
    revision = pointer.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        return None, ["completed replay task review revision must be a positive integer"]
    expected_ref = f"reviews/DECISION-v{revision:03d}.json"
    if pointer.get("decision_path") != expected_ref:
        reasons.append("completed replay task review decision_path is not canonical")
    decision_path = task_dir / expected_ref
    if (task_dir / "reviews").is_symlink() or decision_path.is_symlink():
        reasons.append("completed replay task review decision must not traverse a symlink")
    declared_digest = pointer.get("decision_sha256")
    if not isinstance(declared_digest, str) or re.fullmatch(r"[0-9a-f]{64}", declared_digest) is None:
        reasons.append("completed replay task review pointer has no valid decision_sha256")
    try:
        decision_bytes = decision_path.read_bytes()
        decision = json.loads(decision_bytes)
    except Exception as exc:
        return None, [*reasons, f"completed replay task review decision is unreadable: {exc}"]
    if not isinstance(decision, dict):
        return None, [*reasons, "completed replay task review decision must be an object"]
    if declared_digest != hashlib.sha256(decision_bytes).hexdigest():
        reasons.append("completed replay task review decision digest does not match its pointer")
    expected_fields = {
        "schema_version": 2,
        "artifact_protocol_version": 2,
        "task_id": task_id,
        "revision": revision,
        "decision": expected_decision,
    }
    for field, expected in expected_fields.items():
        if decision.get(field) != expected:
            reasons.append(f"completed replay task review {field} does not match")
    if decision.get("artifact_binding") != dict(expected_binding):
        reasons.append("completed replay task review artifact_binding does not match the handoff")
    if expected_decision == "approved":
        if decision.get("approved_commit") != expected_commit:
            reasons.append("completed replay approved_commit does not match the handoff")
        if decision.get("source_branch") != expected_source_branch:
            reasons.append("completed replay approved source_branch does not match STATUS")
        if decision.get("target_branch") != expected_target_branch:
            reasons.append("completed replay approved target_branch does not match RUN.json")
    return decision, reasons


def _completed_strategy_review_reasons(
    *,
    task_dir: Path,
    task_id: str,
    attempt_id: str,
    bundle: Any,
    records: Sequence[Mapping[str, Any]],
    require_coordinator_review: bool,
) -> list[str]:
    """Bind strategy handoff provenance; optionally require coordinator review."""

    from strategy import (
        StrategyValidationError,
        canonical_digest,
        load_execution_policy,
        validate_strategy,
    )

    reasons: list[str] = []
    submission_ref = "runtime/STRATEGY_SUBMISSION.json"
    submission_path = bundle.attempt_dir / submission_ref
    artifacts = bundle.evidence.get("artifacts")
    try:
        submission_sha256 = file_sha256(submission_path)
    except ArtifactBundleError as exc:
        submission_sha256 = None
        reasons.append(f"completed replay strategy submission bytes are unavailable: {exc}")
    if submission_sha256 is None or not isinstance(artifacts, list) or not any(
        isinstance(item, dict)
        and item.get("ref") == submission_ref
        and item.get("sha256") == submission_sha256
        for item in artifacts
    ):
        reasons.append(
            "completed replay strategy handoff does not bind STRATEGY_SUBMISSION.json"
        )
    try:
        submission = load_json(submission_path)
    except Exception as exc:
        return [
            *reasons,
            f"completed replay STRATEGY_SUBMISSION.json is unreadable: {exc}",
        ]
    if not isinstance(submission, dict):
        return [
            *reasons,
            "completed replay STRATEGY_SUBMISSION.json must be an object",
        ]
    revision = submission.get("strategy_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        reasons.append("completed replay strategy revision must be a positive integer")
        return reasons
    strategy_id = submission.get("strategy_id")
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        reasons.append("completed replay strategy submission has no strategy_id")
    declared_strategy_sha = submission.get("strategy_sha256")
    if (
        not isinstance(declared_strategy_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", declared_strategy_sha) is None
    ):
        reasons.append("completed replay strategy submission has no valid strategy_sha256")
    strategy_name = f"STRATEGY-v{revision:03d}.json"
    expected_submission = {
        "schema_version": 2,
        "artifact_protocol_version": 2,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "strategy_ref": f"../../strategy/{strategy_name}",
    }
    expected_submission_keys = {
        *expected_submission,
        "strategy_revision",
        "strategy_id",
        "strategy_sha256",
    }
    if set(submission) != expected_submission_keys:
        reasons.append("completed replay strategy submission fields are not canonical")
    for field, expected in expected_submission.items():
        if submission.get(field) != expected:
            reasons.append(f"completed replay strategy submission {field} does not match")

    strategy_path = task_dir / "strategy" / strategy_name
    review_path = task_dir / "strategy" / f"REVIEW-v{revision:03d}.json"
    if (task_dir / "strategy").is_symlink() or strategy_path.is_symlink():
        reasons.append("completed replay strategy provenance must not traverse a symlink")
    if not strategy_path.is_file():
        reasons.append("completed replay submitted strategy is not a regular file")
    try:
        strategy = load_json(strategy_path)
    except Exception as exc:
        strategy = None
        reasons.append(f"completed replay submitted strategy is unreadable: {exc}")
    strategy_digest = canonical_digest(strategy) if isinstance(strategy, dict) else None
    if isinstance(strategy, dict):
        if strategy.get("task_id") != task_id:
            reasons.append("completed replay submitted strategy task_id does not match")
        if strategy.get("revision") != revision:
            reasons.append("completed replay submitted strategy revision does not match")
        if strategy.get("strategy_id") != submission.get("strategy_id"):
            reasons.append("completed replay submitted strategy_id does not match")
        if (
            strategy.get("backend_id")
            != bundle.task_inputs_binding.attempt.get("backend_id")
        ):
            reasons.append(
                "completed replay submitted strategy backend_id does not match the attempt"
            )
        try:
            policy_ref = bundle.task_inputs_binding.task_inputs["inputs"][
                "execution_policy"
            ]
            policy_path = task_dir / "EXECUTION_POLICY.json"
            if (
                policy_ref.get("ref") != "EXECUTION_POLICY.json"
                or policy_path.is_symlink()
                or not policy_path.is_file()
                or hashlib.sha256(policy_path.read_bytes()).hexdigest()
                != policy_ref.get("sha256")
            ):
                raise StrategyValidationError(
                    "EXECUTION_POLICY.json differs from the frozen task input"
                )
            validate_strategy(
                strategy,
                load_execution_policy(task_dir),
                task_id=task_id,
            )
        except (KeyError, OSError, StrategyValidationError) as exc:
            reasons.append(
                f"completed replay submitted strategy is invalid under the frozen policy: {exc}"
            )
    if strategy_digest != submission.get("strategy_sha256"):
        reasons.append("completed replay submitted strategy digest does not match")

    if require_coordinator_review:
        if review_path.is_symlink():
            reasons.append("completed replay strategy review must not be a symlink")
        if not review_path.is_file():
            reasons.append("completed replay immutable strategy review is not a regular file")
        try:
            review = load_json(review_path)
        except Exception as exc:
            review = None
            reasons.append(f"completed replay immutable strategy review is unreadable: {exc}")
        if isinstance(review, dict):
            expected_review = {
                "schema_version": 1,
                "strategy_id": submission.get("strategy_id"),
                "strategy_sha256": strategy_digest,
                "decision": "changes_requested",
            }
            for field, expected in expected_review.items():
                if review.get(field) != expected:
                    reasons.append(f"completed replay immutable strategy review {field} does not match")
            if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
                reasons.append("completed replay immutable strategy review has no reviewer")
            if parse_iso(review.get("reviewed_at")) is None:
                reasons.append("completed replay immutable strategy review has invalid reviewed_at")
            if not isinstance(review.get("notes"), list) or not all(
                isinstance(note, str) for note in review.get("notes", [])
            ):
                reasons.append("completed replay immutable strategy review notes must be a string list")

    submitted_matches = [
        (index, event)
        for index, event in enumerate(records)
        if event.get("event") == "strategy_submitted"
        and event.get("task_id") == task_id
        and event.get("revision") == revision
        and event.get("strategy_id") == submission.get("strategy_id")
        and event.get("strategy_sha256") == strategy_digest
    ]
    if not submitted_matches:
        reasons.append("completed replay has no matching strategy_submitted event")
    else:
        _submitted_index, submitted_event = submitted_matches[-1]
        if submitted_event.get("actor") != "worker":
            reasons.append("completed replay strategy_submitted event is not worker-owned")
    if require_coordinator_review:
        reviewed_matches = [
            (index, event)
            for index, event in enumerate(records)
            if event.get("event") == "strategy_reviewed"
            and event.get("task_id") == task_id
            and event.get("revision") == revision
        ]
        if not reviewed_matches:
            reasons.append("completed replay has no matching strategy_reviewed coordinator event")
        else:
            reviewed_index, reviewed_event = reviewed_matches[-1]
            if (
                reviewed_event.get("actor") != "coordinator"
                or reviewed_event.get("decision") != "changes_requested"
                or reviewed_event.get("strategy_sha256") != strategy_digest
            ):
                reasons.append(
                    "completed replay strategy_reviewed event does not match the immutable review"
                )
            if submitted_matches and submitted_matches[-1][0] >= reviewed_index:
                reasons.append("completed replay strategy events are out of order")
    return reasons


def _completed_replay_event_reasons(
    *,
    status: Mapping[str, Any],
    attempt: Mapping[str, Any],
    task_dir: Path,
    bundle: Any,
) -> list[str]:
    """Validate coordinator artifacts/events for states beyond the worker handoff."""

    handoff_state = str(attempt.get("handoff_state"))
    current_state = str(status.get("state"))
    if (current_state == handoff_state and handoff_state != "strategy_review") or current_state == "blocked":
        return []
    records, run, reasons = _completed_replay_journal(task_dir)
    task_id = str(status.get("task_id"))
    attempt_id = str(status.get("current_attempt_id"))
    binding = artifact_binding(bundle)
    source_commit = bundle.handoff.get("source_commit")

    approval_event_index: int | None = None
    if handoff_state == "review" and current_state in {
        "changes_requested",
        "approved",
        "merged",
    }:
        decision_name = "approved" if current_state in {"approved", "merged"} else "changes_requested"
        decision, review_reasons = _load_completed_task_review(
            task_dir,
            task_id=task_id,
            expected_decision=decision_name,
            expected_binding=binding,
            expected_commit=source_commit,
            expected_source_branch=status.get("branch"),
            expected_target_branch=run.get("target_branch"),
        )
        reasons.extend(review_reasons)
        revision = decision.get("revision") if isinstance(decision, dict) else None
        required_events = ["coordinator_reviewed", "task_approved" if decision_name == "approved" else "changes_requested"]
        indices: list[int] = []
        for event_name in required_events:
            matches = [
                (index, event)
                for index, event in enumerate(records)
                if event.get("event") == event_name and event.get("task_id") == task_id
            ]
            if not matches:
                reasons.append(f"completed replay has no {event_name} coordinator event")
                continue
            index, event = matches[-1]
            indices.append(index)
            if event.get("actor") != "coordinator":
                reasons.append(f"completed replay {event_name} event is not coordinator-owned")
            if event.get("review_revision") != revision:
                reasons.append(f"completed replay {event_name} review_revision does not match")
            if event_name == "coordinator_reviewed" and event.get("decision") != decision_name:
                reasons.append("completed replay coordinator_reviewed decision does not match")
        if len(indices) == 2:
            if indices[0] >= indices[1]:
                reasons.append("completed replay coordinator review events are out of order")
            approval_event_index = indices[1]

    if handoff_state == "strategy_review":
        reasons.extend(
            _completed_strategy_review_reasons(
                task_dir=task_dir,
                task_id=task_id,
                attempt_id=attempt_id,
                bundle=bundle,
                records=records,
                require_coordinator_review=current_state == "changes_requested",
            )
        )

    if current_state == "merged":
        matches = [
            (index, event)
            for index, event in enumerate(records)
            if event.get("event") == "task_merged" and event.get("task_id") == task_id
        ]
        if not matches:
            reasons.append("completed replay merged state has no task_merged event")
        else:
            merge_index, event = matches[-1]
            expected_fields = {
                "actor": "coordinator",
                "attempt_id": attempt_id,
                "commit": source_commit,
                "source_branch": status.get("branch"),
                "target_branch": run.get("target_branch"),
            }
            for field, expected in expected_fields.items():
                if event.get(field) != expected:
                    reasons.append(f"completed replay task_merged {field} does not match")
            if event.get("artifact_binding") != binding:
                reasons.append("completed replay task_merged artifact_binding does not match the handoff")
            verification = event.get("verification")
            if not isinstance(verification, dict) or not isinstance(verification.get("passed"), bool):
                reasons.append("completed replay task_merged verification is invalid")
            coordinator_id = event.get("coordinator_id")
            if not isinstance(coordinator_id, str) or not coordinator_id:
                reasons.append("completed replay task_merged coordinator_id is missing")
            if approval_event_index is not None and approval_event_index >= merge_index:
                reasons.append("completed replay task_merged event precedes coordinator approval")
    return reasons


def _validate_completed_replay(
    *,
    version: int,
    status: Mapping[str, Any],
    attempt: Mapping[str, Any],
    task_dir: Path,
    attempt_id: str,
    expected_task_id: str | None,
    expected_dispatch: Mapping[str, Any] | None = None,
) -> list[str]:
    """Validate an already-applied dispatcher replay without mutating newer FSM state."""

    reasons: list[str] = []
    expected_dispatch = expected_dispatch or {}
    handoff_state = attempt.get("handoff_state")
    if handoff_state not in _COMPLETED_REPLAY_STATES:
        return ["completed attempt has an invalid handoff_state"]
    if status.get("current_attempt_id") != attempt_id:
        return ["completed dispatcher replay is stale relative to STATUS.current_attempt_id"]
    if status.get("state") not in _COMPLETED_REPLAY_STATES[str(handoff_state)]:
        return [
            "completed dispatcher replay cannot be applied over task state "
            f"{status.get('state')!r}"
        ]
    if version == 2:
        reasons.extend(
            reason.replace("completed-replay: ", "completed replay ", 1)
            for reason in validate_attempt_schema(
                dict(attempt),
                dict(status),
                attempt_id,
                "completed-replay",
            )
        )
        reasons.extend(
            _completed_replay_history_reasons(
                status,
                attempt,
                str(handoff_state),
            )
        )
        profile = status.get("profile")
        if handoff_state == "verified" and profile != "direct":
            reasons.append("completed verified replay requires profile='direct'")
        if handoff_state == "review" and profile not in {"delegated", "full"}:
            reasons.append("completed review replay requires Delegated or Full profile")
        if handoff_state == "strategy_review" and profile != "full":
            reasons.append("completed strategy_review replay requires Full profile")
        status_fields = {
            "task_id": "task_id",
            "artifact_protocol_version": "artifact_protocol_version",
            "profile": "profile",
            "branch": "branch",
            "worktree": "worktree",
        }
        for expected_field, status_field in status_fields.items():
            expected = expected_dispatch.get(expected_field)
            if expected is not None and status.get(status_field) != expected:
                reasons.append(
                    f"completed replay STATUS.{status_field} differs from dispatcher freeze"
                )
        if expected_dispatch.get("phase") is not None and attempt.get("phase") != expected_dispatch["phase"]:
            reasons.append("completed replay ATTEMPT.phase differs from dispatcher freeze")
        if (
            expected_dispatch.get("worker_backend") is not None
            and attempt.get("backend_id") != expected_dispatch["worker_backend"]
        ):
            reasons.append("completed replay ATTEMPT.backend_id differs from dispatcher freeze")
        expected_worktree = expected_dispatch.get("worktree")
        runtime = attempt.get("runtime") if isinstance(attempt.get("runtime"), dict) else {}
        if expected_worktree is not None:
            actual_cwd = runtime.get("cwd")
            if (
                not isinstance(actual_cwd, str)
                or Path(actual_cwd).resolve() != Path(str(expected_worktree)).resolve()
            ):
                reasons.append("completed replay ATTEMPT.runtime.cwd differs from dispatcher freeze")
        reasons.extend(
            _v2_governance_reasons(
                task_dir / "attempts" / attempt_id,
                attempt,
                expected_dispatch,
            )
        )
        try:
            bundle = load_bundle(
                task_dir / "attempts" / attempt_id,
                expected_task_id=expected_task_id or str(status.get("task_id")),
                expected_attempt_id=attempt_id,
                expected_requested_state=str(handoff_state),
            )
        except ArtifactBundleError as exc:
            return [f"completed v2 handoff bundle is invalid: {exc}"]
        expected_inputs_sha = expected_dispatch.get("task_inputs_sha256")
        if (
            expected_inputs_sha is not None
            and bundle.task_inputs_binding.task_inputs_sha256 != expected_inputs_sha
        ):
            reasons.append("completed replay TASK_INPUTS digest differs from dispatcher freeze")
        expected_base = expected_dispatch.get("task_base_commit")
        if (
            expected_base is not None
            and bundle.task_inputs_binding.task_inputs.get("task_base_commit") != expected_base
        ):
            reasons.append("completed replay task base commit differs from dispatcher freeze")
        expected_before_sha = expected_dispatch.get("worktree_before_sha256")
        if expected_before_sha is not None:
            before_path = task_dir / "attempts" / attempt_id / "runtime" / "worktree-before.json"
            try:
                actual_before_sha = hashlib.sha256(before_path.read_bytes()).hexdigest()
            except OSError as exc:
                reasons.append(f"completed replay worktree-before is unreadable: {exc}")
            else:
                if actual_before_sha != expected_before_sha:
                    reasons.append("completed replay worktree-before differs from dispatcher freeze")
        frozen_commit = bundle.handoff.get("source_commit")
        if handoff_state in {"verified", "review"}:
            if attempt.get("source_commit") != frozen_commit:
                reasons.append(
                    "completed ATTEMPT.source_commit is missing or differs from the frozen handoff"
                )
        elif attempt.get("source_commit") is not None and attempt.get("source_commit") != frozen_commit:
            reasons.append("completed ATTEMPT.source_commit differs from the frozen handoff")
        if handoff_state == "verified":
            if attempt.get("verified_commit") != frozen_commit:
                reasons.append(
                    "completed ATTEMPT.verified_commit is missing or differs from the frozen handoff"
                )
        elif attempt.get("verified_commit") is not None and attempt.get("verified_commit") != frozen_commit:
            reasons.append("completed ATTEMPT.verified_commit differs from the frozen handoff")
        reasons.extend(
            _completed_replay_event_reasons(
                status=status,
                attempt=attempt,
                task_dir=task_dir,
                bundle=bundle,
            )
        )
    else:
        try:
            request = load_json(task_dir / "HANDOFF.json")
        except Exception as exc:
            return [f"completed legacy handoff is unreadable: {exc}"]
        if not isinstance(request, dict) or request.get("requested_state") != handoff_state:
            reasons.append("completed legacy handoff no longer matches ATTEMPT.handoff_state")
    return reasons


def cmd_validate_handoff(args: argparse.Namespace) -> int:
    attempt_path = Path(args.attempt_path)
    task_dir = Path(args.task_dir)
    status_error = None
    try:
        status = load_json(Path(args.status_path))
    except json.JSONDecodeError as exc:
        status = None
        status_error = f"invalid STATUS.json: {exc}"
    try:
        initial_attempt = load_json(attempt_path)
    except Exception:
        initial_attempt = None
    allow_completed_attempt = bool(
        isinstance(initial_attempt, dict)
        and initial_attempt.get("state") == "completed"
        and initial_attempt.get("handoff_valid") is True
    )
    expected_version = getattr(args, "expected_artifact_protocol_version", None)
    expected_dispatch = {
        "task_id": getattr(args, "expected_task_id", "") or None,
        "artifact_protocol_version": expected_version,
        "profile": getattr(args, "expected_profile", "") or None,
        "phase": getattr(args, "expected_phase", "") or None,
        "branch": getattr(args, "expected_branch", "") or None,
        "worktree": getattr(args, "expected_worktree", "") or None,
        "worker_backend": getattr(args, "expected_worker_backend", "") or None,
        "strategy_id": getattr(args, "expected_strategy_id", "") or None,
        "strategy_revision": (
            int(args.expected_strategy_revision)
            if getattr(args, "expected_strategy_revision", "")
            else None
        ),
        "strategy_sha256": getattr(args, "expected_strategy_sha256", "") or None,
        "backend_profile_sha256": (
            getattr(args, "expected_backend_profile_sha256", "") or None
        ),
        "backend_settings_sha256": (
            getattr(args, "expected_backend_settings_sha256", "") or None
        ),
        "read_policy_sha256": (
            getattr(args, "expected_read_policy_sha256", "") or None
        ),
        "task_inputs_sha256": (
            getattr(args, "expected_task_inputs_sha256", "") or None
        ),
        "task_base_commit": (
            getattr(args, "expected_task_base_commit", "") or None
        ),
        "worktree_before_sha256": (
            getattr(args, "expected_worktree_before_sha256", "") or None
        ),
    }
    try:
        version = (
            int(expected_version)
            if expected_version in {1, 2}
            else _task_protocol_version(task_dir)
        )
    except (TaskContractError, TypeError, ValueError) as exc:
        version = None
        result = HandoffValidationResult(
            valid=False,
            handoff_state=None,
            exit_code=parse_exit_code(args.exit_code_raw)[0],
            reasons=[str(exc)],
        )
    else:
        if allow_completed_attempt and isinstance(status, dict):
            replay_phase = expected_dispatch.get("phase") or initial_attempt.get("phase")
            active_state = "planning" if replay_phase == "planning" else "running"
            if (
                status.get("current_attempt_id") != args.attempt_id
                or status.get("state") != active_state
            ):
                replay_reasons = _validate_completed_replay(
                    version=version,
                    status=status,
                    attempt=initial_attempt,
                    task_dir=task_dir,
                    attempt_id=args.attempt_id,
                    expected_task_id=expected_dispatch.get("task_id"),
                    expected_dispatch=expected_dispatch,
                )
                if replay_reasons:
                    print("completed_dispatch_replay_rejected", file=sys.stderr)
                    for reason in replay_reasons:
                        print(f"- {reason}", file=sys.stderr)
                    return 4
                return 0
        result = (
            _validate_v2_handoff(
                status,
                task_dir=task_dir,
                attempt_id=args.attempt_id,
                exit_code_raw=args.exit_code_raw,
                worktree=(Path(args.worktree).resolve() if args.worktree else None),
                expected_dispatch=expected_dispatch,
                allow_completed_attempt=allow_completed_attempt,
            )
            if version == 2
            else validate_worker_handoff(status, args.attempt_id, task_dir, args.exit_code_raw)
        )
    if status_error:
        result = HandoffValidationResult(
            valid=False,
            handoff_state=None,
            exit_code=result.exit_code,
            reasons=[status_error, *result.reasons],
        )

    verified_commit = None
    if result.handoff_state in {"verified", "review"}:
        frozen_commit = result.request.get("source_commit") if isinstance(result.request, dict) else None
        if not isinstance(frozen_commit, str) or not frozen_commit:
            result = HandoffValidationResult(
                valid=False,
                handoff_state=result.handoff_state,
                exit_code=result.exit_code,
                reasons=[
                    *result.reasons,
                    f"{result.handoff_state} handoff is missing its finalize-time source_commit",
                ],
                request=result.request,
            )
        elif not args.worktree:
            result = HandoffValidationResult(
                valid=False,
                handoff_state=result.handoff_state,
                exit_code=result.exit_code,
                reasons=[
                    *result.reasons,
                    f"{result.handoff_state} handoff is missing the task worktree needed to check its Git commit"
                ],
                request=result.request,
            )
        else:
            try:
                actual_commit = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=Path(args.worktree).resolve(),
                    text=True,
                    stderr=subprocess.STDOUT,
                ).strip()
            except (OSError, subprocess.CalledProcessError) as exc:
                detail = exc.output.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
                result = HandoffValidationResult(
                    valid=False,
                    handoff_state=result.handoff_state,
                    exit_code=result.exit_code,
                    reasons=[
                        *result.reasons,
                        f"could not check frozen handoff against task worktree HEAD: {detail}"
                    ],
                    request=result.request,
                )
            else:
                if actual_commit != frozen_commit:
                    result = HandoffValidationResult(
                        valid=False,
                        handoff_state=result.handoff_state,
                        exit_code=result.exit_code,
                        reasons=[
                            *result.reasons,
                            "task branch HEAD changed after handoff finalization",
                        ],
                        request=result.request,
                    )
                elif result.valid:
                    verified_commit = frozen_commit

    trusted_status: dict[str, Any] = {}
    if version == 2:
        for field in (
            "task_id",
            "artifact_protocol_version",
            "profile",
            "branch",
            "worktree",
        ):
            if expected_dispatch.get(field) is not None:
                trusted_status[field] = expected_dispatch[field]
        trusted_status["current_attempt_id"] = args.attempt_id
        if expected_dispatch.get("phase") in {"planning", "execution"}:
            trusted_status["active_state"] = (
                "planning" if expected_dispatch["phase"] == "planning" else "running"
            )

    try:
        attempt = load_json(attempt_path)
    except json.JSONDecodeError as exc:
        print("worker_exit_without_valid_status", file=sys.stderr)
        print(f"- invalid ATTEMPT.json: {exc}", file=sys.stderr)
        for reason in result.reasons:
            print(f"- {reason}", file=sys.stderr)
        return 4
    if not isinstance(attempt, dict):
        print("worker_exit_without_valid_status", file=sys.stderr)
        print("- ATTEMPT.json must be a JSON object", file=sys.stderr)
        for reason in result.reasons:
            print(f"- {reason}", file=sys.stderr)
        return 4
    if (
        result.valid
        and
        isinstance(status, dict)
        and attempt.get("state") == "completed"
        and attempt.get("handoff_valid") is True
        and status.get("current_attempt_id") == args.attempt_id
        and status.get("state") == attempt.get("handoff_state")
    ):
        if version == 2:
            try:
                request = load_bundle(
                    attempt_path.parent,
                    expected_task_id=status.get("task_id"),
                    expected_attempt_id=args.attempt_id,
                ).handoff
            except Exception:
                request = None
        else:
            try:
                request = load_json(task_dir / "HANDOFF.json")
            except Exception:
                request = None
        if isinstance(request, dict) and request.get("requested_state") == attempt.get("handoff_state"):
            return 0
    attempt["ended_at"] = utc_now()
    attempt["exit_code"] = result.exit_code

    if not result.valid:
        startup_failure = None
        if args.startup_path:
            startup_path = Path(args.startup_path)
            if startup_path.exists():
                try:
                    startup = load_json(startup_path)
                except json.JSONDecodeError:
                    startup = None
                if isinstance(startup, dict) and startup.get("state") in {
                    "worker_startup_failed",
                    "tui_startup_failed",
                }:
                    failure = startup.get("failure")
                    startup_failure = failure if isinstance(failure, dict) else {}
                    if not startup_failure:
                        evidence = startup.get("startup_evidence")
                        startup_failure = {
                            "code": str(startup.get("state")),
                            "message": str(evidence or "worker startup failed"),
                        }
        attempt["state"] = "invalid_handoff"
        attempt["handoff_valid"] = False
        attempt["handoff_state"] = None
        if startup_failure is not None:
            attempt["startup_failure"] = startup_failure
        write_json(attempt_path, attempt)
        try:
            if startup_failure is not None:
                failure_code = str(startup_failure.get("code") or "worker_startup_failed")
                failure_message = str(startup_failure.get("message") or "worker startup failed")
                summary = "Worker startup failed"
                blocker_type = "environment"
                blocking_reason = f"{failure_code}: {failure_message}"
            else:
                summary = "Invalid worker handoff"
                blocker_type = "needs_coordinator"
                blocking_reason = "invalid worker handoff: " + "; ".join(result.reasons[:3])
            apply_dispatch_terminal_transition(
                Path(args.status_path),
                target_state="blocked",
                summary=summary,
                needs_coordinator=True,
                blocker_type=blocker_type,
                blocking_reason=blocking_reason,
                trusted_status=trusted_status,
            )
        except Exception as exc:
            print(f"- failed to mark task blocked after invalid handoff: {exc}", file=sys.stderr)
        print("worker_exit_without_valid_status", file=sys.stderr)
        for reason in result.reasons:
            print(f"- {reason}", file=sys.stderr)
        return 4

    attempt["state"] = "completed"
    attempt["handoff_valid"] = True
    attempt["handoff_state"] = result.handoff_state
    if verified_commit is not None:
        attempt["source_commit"] = verified_commit
        if result.handoff_state == "verified":
            attempt["verified_commit"] = verified_commit
    write_json(attempt_path, attempt)
    request = result.request or {}
    if result.handoff_state == "blocked":
        blocker_type = str(request.get("blocker_type") or "")
        blocking_reason = str(request.get("blocking_reason") or "")
        needs_coordinator = bool(request.get("needs_coordinator", True))
    else:
        blocker_type = ""
        blocking_reason = ""
        needs_coordinator = bool(request.get("needs_coordinator", False))
    commands_run = (
        request.get("commands_run")
        if version == 1 and isinstance(request.get("commands_run"), list)
        else None
    )
    blocker = request.get("conditional_blocker") if isinstance(request, dict) else None
    if version == 2 and result.handoff_state == "blocked" and isinstance(blocker, dict):
        blocker_type = str(blocker.get("blocker_type") or "")
        blocking_reason = str(blocker.get("reason") or "")
        needs_coordinator = True
    apply_dispatch_terminal_transition(
        Path(args.status_path),
        target_state=str(result.handoff_state),
        summary=str(request.get("summary") or ""),
        needs_coordinator=needs_coordinator,
        blocker_type=blocker_type,
        blocking_reason=blocking_reason,
        commands_run=commands_run,
        trusted_status=trusted_status,
    )
    return 0


def cmd_write_dispatch_diagnostics(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    diagnostics_dir = run_dir / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = diagnostics_dir / f"dispatch-failure-{args.task_id}-{stamp}.md"
    path.write_text(
        "\n".join(
            [
                "# Dispatch Failure",
                "",
                f"- run_id: {args.run_id}",
                f"- task_id: {args.task_id}",
                f"- exit_code: {args.exit_code}",
                f"- status_updated: {args.status_updated}",
                f"- attempt_id: {args.attempt_id or ''}",
                f"- lock_path: {args.lock_path}",
                f"- dispatch_lock_dir: {args.dispatch_lock_dir}",
                f"- time: {utc_now()}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return 0


def cmd_write_tmux_timeout_diagnostics(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    diagnostics_dir = run_dir / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = diagnostics_dir / f"tmux-wait-timeout-{args.task_id}-{stamp}.json"
    md_path = diagnostics_dir / f"tmux-wait-timeout-{args.task_id}-{stamp}.md"
    payload = {
        "at": utc_now(),
        "reason": "tmux_wait_timeout",
        "run_id": args.run_id,
        "task_id": args.task_id,
        "attempt_id": args.attempt_id,
        "tmux_session": args.tmux_session,
        "attach_command": args.attach_command,
        "timeout_seconds": int(args.timeout_seconds),
        "dispatch_exit_code": 5,
        "worker_exit_code": None,
        "dispatch_lock_retained": True,
        "dispatch_lock_dir": args.dispatch_lock_dir,
        "attempt_dir": args.attempt_dir,
    }
    write_json(json_path, payload, sort_keys=True)
    md_path.write_text(
        "\n".join(
            [
                "# Tmux Wait Timeout",
                "",
                f"- run_id: {args.run_id}",
                f"- task_id: {args.task_id}",
                f"- attempt_id: {args.attempt_id}",
                "- reason: tmux_wait_timeout",
                "- dispatch_exit_code: 5",
                "- worker_exit_code: null",
                "- dispatch_lock_retained: true",
                f"- tmux_session: {args.tmux_session}",
                f"- attach_command: {args.attach_command}",
                f"- timeout_seconds: {args.timeout_seconds}",
                f"- time: {utc_now()}",
                "",
                "Dispatch lost supervision before the attempt-local exit_code file appeared.",
                "Do not assume the worker stopped. Use Lock Recovery Review before removing .dispatch-lock.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal protocol operations for dispatch.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-dispatch-transition")
    check.add_argument("--status-path", required=True)
    check.add_argument("--fsm-path", required=True)
    check.add_argument("--phase", choices=["planning", "execution"], required=True)
    check.set_defaults(func=cmd_check_dispatch_transition)

    protocol_version = sub.add_parser("task-protocol-version")
    protocol_version.add_argument("--task-dir", required=True)
    protocol_version.set_defaults(func=cmd_task_protocol_version)

    readiness = sub.add_parser("check-task-readiness")
    readiness.add_argument("--task-dir", required=True)
    readiness.add_argument("--run-dir", required=True)
    readiness.add_argument("--task-id", required=True)
    readiness.add_argument("--profile", choices=["direct", "delegated", "full"], required=True)
    readiness.set_defaults(func=cmd_check_task_readiness)

    freeze_inputs = sub.add_parser("freeze-task-inputs")
    freeze_inputs.add_argument("--task-dir", required=True)
    freeze_inputs.add_argument("--run-dir", required=True)
    freeze_inputs.add_argument("--repo-root", required=True)
    freeze_inputs.add_argument("--task-id", required=True)
    freeze_inputs.add_argument("--attempt-id", required=True)
    freeze_inputs.add_argument("--attempt-dir", required=True)
    freeze_inputs.add_argument("--profile", choices=["direct", "delegated", "full"], required=True)
    freeze_inputs.add_argument(
        "--execution-mode",
        choices=["start", "resume", "replace"],
        default="start",
        help="recorded for dispatch diagnostics; all later v2 attempts enforce the frozen contract",
    )
    freeze_inputs.set_defaults(func=cmd_freeze_task_inputs)

    event = sub.add_parser("append-event")
    add_common_event_args(event)
    event.add_argument("--event-name", required=True)
    event.add_argument("--agent-name", required=True)
    event.add_argument("--worker-backend", default="")
    event.add_argument("--execution-mode", default="")
    event.add_argument("--status-path", required=True)
    event.set_defaults(func=cmd_append_event)

    attempt = sub.add_parser("create-attempt")
    attempt.add_argument("--path", required=True)
    attempt.add_argument("--artifact-protocol-version", type=int, choices=[1, 2], default=1)
    attempt.add_argument("--task-inputs-ref", default="")
    attempt.add_argument("--task-inputs-sha256", default="")
    attempt.add_argument("--attempt-id", required=True)
    attempt.add_argument("--task-id", required=True)
    attempt.add_argument("--agent-name", required=True)
    attempt.add_argument("--worker-id", required=True)
    attempt.add_argument("--parent-attempt-id", default="")
    attempt.add_argument("--session-id", default="")
    attempt.add_argument("--worker-backend", default="claude-code")
    attempt.add_argument("--execution-mode", default="start")
    attempt.add_argument("--resume-reason", default="")
    attempt.add_argument("--phase", choices=["planning", "execution"], required=True)
    attempt.add_argument("--strategy-id", default="")
    attempt.add_argument("--strategy-revision", default="")
    attempt.add_argument("--strategy-sha256", default="")
    attempt.add_argument("--backend-profile-sha256", default="")
    attempt.add_argument("--backend-settings-sha256", default="")
    attempt.add_argument("--read-policy-sha256", default="")
    attempt.add_argument("--permission-mode", default="auto")
    attempt.add_argument("--io-mode", default="machine")
    attempt.add_argument("--command", required=True)
    attempt.add_argument("--supervisor-command", default="")
    attempt.add_argument("--cwd", required=True)
    attempt.add_argument("--backend", dest="runtime_backend", required=True)
    attempt.add_argument("--runtime-backend", dest="runtime_backend", default=None)
    attempt.add_argument("--tmux-session", default="")
    attempt.add_argument("--attach-command", default="")
    attempt.set_defaults(func=cmd_create_attempt)

    transition = sub.add_parser("transition-running")
    transition.add_argument("--status-path", required=True)
    transition.add_argument("--fsm-path", required=True)
    transition.add_argument("--attempt-id", required=True)
    transition.add_argument("--worker-id", required=True)
    transition.add_argument("--agent-name", required=True)
    transition.add_argument("--session-id", default="")
    transition.add_argument("--worker-backend", default="claude-code")
    transition.add_argument("--phase", choices=["planning", "execution"], required=True)
    transition.set_defaults(func=cmd_transition_running)

    set_running = sub.add_parser("set-attempt-running")
    set_running.add_argument("--attempt-path", required=True)
    set_running.set_defaults(func=cmd_set_attempt_running)

    record_session = sub.add_parser("record-session")
    record_session.add_argument("--status-path", required=True)
    record_session.add_argument("--attempt-path", required=True)
    record_session.add_argument("--session-path", required=True)
    record_session.set_defaults(func=cmd_record_session)

    validate = sub.add_parser("validate-handoff")
    validate.add_argument("--status-path", required=True)
    validate.add_argument("--attempt-id", required=True)
    validate.add_argument("--task-dir", required=True)
    validate.add_argument("--attempt-path", required=True)
    validate.add_argument("--exit-code-raw", required=True)
    validate.add_argument("--startup-path", default="")
    validate.add_argument("--worktree", default="")
    validate.add_argument("--expected-profile", default="")
    validate.add_argument("--expected-task-id", default="")
    validate.add_argument("--expected-artifact-protocol-version", type=int, choices=[1, 2])
    validate.add_argument("--expected-phase", default="")
    validate.add_argument("--expected-branch", default="")
    validate.add_argument("--expected-worktree", default="")
    validate.add_argument("--expected-worker-backend", default="")
    validate.add_argument("--expected-strategy-id", default="")
    validate.add_argument("--expected-strategy-revision", default="")
    validate.add_argument("--expected-strategy-sha256", default="")
    validate.add_argument("--expected-backend-profile-sha256", default="")
    validate.add_argument("--expected-backend-settings-sha256", default="")
    validate.add_argument("--expected-read-policy-sha256", default="")
    validate.add_argument("--expected-task-inputs-sha256", default="")
    validate.add_argument("--expected-task-base-commit", default="")
    validate.add_argument("--expected-worktree-before-sha256", default="")
    validate.set_defaults(func=cmd_validate_handoff)

    diagnostics = sub.add_parser("write-dispatch-diagnostics")
    diagnostics.add_argument("--run-dir", required=True)
    diagnostics.add_argument("--run-id", required=True)
    diagnostics.add_argument("--task-id", required=True)
    diagnostics.add_argument("--attempt-id", default="")
    diagnostics.add_argument("--exit-code", required=True)
    diagnostics.add_argument("--status-updated", required=True)
    diagnostics.add_argument("--lock-path", required=True)
    diagnostics.add_argument("--dispatch-lock-dir", required=True)
    diagnostics.set_defaults(func=cmd_write_dispatch_diagnostics)

    tmux_timeout = sub.add_parser("write-tmux-timeout-diagnostics")
    tmux_timeout.add_argument("--run-dir", required=True)
    tmux_timeout.add_argument("--run-id", required=True)
    tmux_timeout.add_argument("--task-id", required=True)
    tmux_timeout.add_argument("--attempt-id", required=True)
    tmux_timeout.add_argument("--tmux-session", required=True)
    tmux_timeout.add_argument("--attach-command", required=True)
    tmux_timeout.add_argument("--timeout-seconds", required=True)
    tmux_timeout.add_argument("--dispatch-lock-dir", required=True)
    tmux_timeout.add_argument("--attempt-dir", required=True)
    tmux_timeout.set_defaults(func=cmd_write_tmux_timeout_diagnostics)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
