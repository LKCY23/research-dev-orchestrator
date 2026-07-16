#!/usr/bin/env python3
"""Collect and validate orchestration run status without mutating protocol state."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from artifact_resolver import (
    ArtifactResolutionError,
    protocol_route,
    resolve_task_artifacts,
    validate_artifact_binding_for_task,
)
from config import load_config
from protocol import (  # noqa: E402
    EventJournalError,
    LEGACY_PROTOCOL_VERSIONS,
    PACKAGE_VERSION,
    PROTOCOL_VERSION,
    has_substantive_content,
    load_json,
    parse_iso,
    pid_is_alive,
    read_event_journal,
    repo_root,
    utc_now,
)
from validation import (
    load_handoff_request,
    validate_attempt_schema,
    validate_event,
    validate_state_history,
    validate_task_profile_binding,
    validate_status_schema,
)


def load_events(run_dir: Path, run_id: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    events_path = run_dir / "EVENTS.ndjson"
    if not events_path.exists():
        return [], ["run: missing required EVENTS.ndjson"], []
    violations: list[str] = []
    warnings: list[str] = []
    try:
        events, tail_warning = read_event_journal(
            run_dir,
            tolerate_interrupted_tail=True,
        )
    except EventJournalError as exc:
        return [], [str(exc)], []
    if tail_warning:
        warnings.append(tail_warning)
    for line_no, event in enumerate(events, start=1):
        event_violations, event_warnings = validate_event(event, run_id, line_no)
        violations.extend(event_violations)
        warnings.extend(event_warnings)
    return events, violations, warnings


def load_handoff_index(task_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Load HANDOFF.json for summaries; terminal-state validation is stricter."""

    path = task_dir / "HANDOFF.json"
    if not path.exists():
        return None, []
    try:
        payload = load_json(path)
    except json.JSONDecodeError as exc:
        return None, [f"{task_dir.name}: HANDOFF.json malformed JSON: {exc}"]
    if not isinstance(payload, dict):
        return None, [f"{task_dir.name}: HANDOFF.json must be a JSON object when present"]
    if payload.get("_template") is True:
        return {"template": True}, []

    warnings: list[str] = []
    requested_state = payload.get("requested_state")
    if requested_state not in {"", None, "strategy_review", "verified", "review", "blocked"}:
        warnings.append(
            f"{task_dir.name}: HANDOFF.json requested_state should be strategy_review, verified, review, or blocked"
        )
    for field in ("commands_run", "files_changed", "known_limitations"):
        if field in payload and not isinstance(payload.get(field), list):
            warnings.append(f"{task_dir.name}: HANDOFF.json {field} must be a list")
    if "needs_coordinator" in payload and not isinstance(payload.get("needs_coordinator"), bool):
        warnings.append(f"{task_dir.name}: HANDOFF.json needs_coordinator must be boolean")
    return {
        "template": False,
        "requested_state": requested_state,
        "summary": payload.get("summary", ""),
        "commands_run": payload.get("commands_run", []),
        "files_changed": payload.get("files_changed", []),
        "known_limitations": payload.get("known_limitations", []),
        "needs_coordinator": payload.get("needs_coordinator", False),
        "blocker_type": payload.get("blocker_type", ""),
        "blocking_reason": payload.get("blocking_reason", ""),
    }, warnings


def status_uses_v2(status: dict[str, Any]) -> bool:
    """Return true only for the explicit v2 status discriminator."""

    return status.get("artifact_protocol_version") == 2


def status_uses_legacy(status: dict[str, Any]) -> bool:
    """Return true only for a recognized legacy discriminator."""

    try:
        route, _ = protocol_route(status)
    except ArtifactResolutionError:
        return False
    return route.startswith("legacy-")


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_fsm() -> dict[str, Any]:
    return load_json(skill_root() / "references" / "state-machine.json")


def validate_attempt(
    task_dir: Path,
    status: dict[str, Any],
    stale_created_minutes: float,
    tmux_exit_code_grace_seconds: int,
) -> tuple[list[str], list[str], dict[str, Any] | None]:
    violations: list[str] = []
    warnings: list[str] = []
    state = status.get("state")
    profile = status.get("profile", "full")
    if state == "planning" and profile != "full":
        violations.append(
            f"{task_dir.name}: STATUS planning requires profile='full'"
        )
    artifact_legacy = status_uses_legacy(status)
    policy_path = task_dir / "EXECUTION_POLICY.json"
    if policy_path.exists():
        try:
            policy = load_json(policy_path)
        except Exception as exc:
            violations.append(f"{task_dir.name}: EXECUTION_POLICY.json is unreadable: {exc}")
        else:
            expected_strategy_required = profile == "full"
            if policy.get("strategy_required") is not expected_strategy_required:
                violations.append(
                    f"{task_dir.name}: EXECUTION_POLICY.strategy_required must be "
                    f"{expected_strategy_required} for profile {profile!r}"
                )
    attempt_id = status.get("current_attempt_id")
    if not attempt_id:
        return violations, warnings, None

    attempt_path = task_dir / "attempts" / str(attempt_id) / "ATTEMPT.json"
    if not attempt_path.exists():
        violations.append(f"{task_dir.name}: ATTEMPT.json missing for current_attempt_id {attempt_id}")
        return violations, warnings, None
    try:
        attempt = load_json(attempt_path)
    except json.JSONDecodeError as exc:
        violations.append(f"{task_dir.name}: invalid ATTEMPT.json for {attempt_id}: {exc}")
        return violations, warnings, None
    if not isinstance(attempt, dict):
        violations.append(f"{task_dir.name}: ATTEMPT.json must be a JSON object")
        return violations, warnings, None

    violations.extend(validate_attempt_schema(attempt, status, str(attempt_id), task_dir.name))
    runtime = attempt.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}

    attempt_state = attempt.get("state")
    if attempt_state == "created":
        started = parse_iso(attempt.get("started_at"))
        if started:
            age_minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
            if age_minutes > stale_created_minutes:
                warnings.append(f"{task_dir.name}: ATTEMPT.state created for {age_minutes:.1f} minutes")

    lock = task_dir / "LOCK"
    dispatch_lock = task_dir / ".dispatch-lock"
    if state in {"planning", "running"}:
        expected_phase = "planning" if state == "planning" else "execution"
        dispatch_pid_alive: bool | None = None
        dispatch_attempt_matches = False
        recoverable_completed = bool(
            status_uses_v2(status)
            and attempt_state == "completed"
            and attempt.get("handoff_valid") is True
        )
        if attempt_state not in {"created", "running"} and not recoverable_completed:
            violations.append(
                f"{task_dir.name}: STATUS {state} requires ATTEMPT.state created or running, got {attempt_state!r}"
            )
        if attempt.get("phase") != expected_phase:
            violations.append(f"{task_dir.name}: STATUS {state} requires ATTEMPT.phase={expected_phase}")
        if not lock.exists():
            violations.append(f"{task_dir.name}: STATUS {state} requires LOCK")
        if not dispatch_lock.is_dir():
            violations.append(f"{task_dir.name}: STATUS {state} requires active .dispatch-lock")
        else:
            dispatch_attempt = dispatch_lock / "attempt_id"
            if not dispatch_attempt.exists():
                violations.append(f"{task_dir.name}: .dispatch-lock missing attempt_id")
            elif dispatch_attempt.read_text(encoding="utf-8", errors="replace").strip() != str(attempt_id):
                violations.append(f"{task_dir.name}: .dispatch-lock attempt_id does not match STATUS current_attempt_id")
            else:
                dispatch_attempt_matches = True
            pid_path = dispatch_lock / "pid"
            if not pid_path.exists():
                violations.append(f"{task_dir.name}: .dispatch-lock missing pid while STATUS is {state}")
            else:
                pid_text = pid_path.read_text(encoding="utf-8", errors="replace").strip()
                try:
                    pid = int(pid_text)
                except ValueError:
                    violations.append(
                        f"{task_dir.name}: .dispatch-lock pid is not an integer while STATUS is {state}: {pid_text!r}"
                    )
                else:
                    dispatch_pid_alive = pid_is_alive(pid)
                    if not dispatch_pid_alive:
                        violations.append(f"{task_dir.name}: .dispatch-lock pid is not alive while STATUS is {state}: {pid}")
            if runtime.get("backend") == "tmux" and attempt_state == "running":
                exit_code_path = attempt_path.parent / "exit_code"
                if exit_code_path.exists():
                    exit_code_age = (datetime.now(timezone.utc).timestamp() - exit_code_path.stat().st_mtime)
                    if dispatch_pid_alive is True and exit_code_age <= tmux_exit_code_grace_seconds:
                        warnings.append(
                            f"{task_dir.name}: tmux exit_code file exists while dispatch appears alive; "
                            f"handoff validation may be in progress ({exit_code_age:.1f}s old)"
                        )
                    else:
                        violations.append(
                            f"{task_dir.name}: tmux exit_code file exists while STATUS and ATTEMPT still report running"
                        )
        if recoverable_completed:
            recovery_reasons: list[str] = []
            expected_handoffs = (
                {"strategy_review", "blocked"}
                if state == "planning"
                else (
                    {"verified", "blocked"}
                    if profile == "direct"
                    else (
                        {"review", "blocked", "strategy_review"}
                        if profile == "full"
                        else {"review", "blocked"}
                    )
                )
            )
            if attempt.get("handoff_state") not in expected_handoffs:
                recovery_reasons.append("handoff_state does not match the active task/profile")
            ended = parse_iso(attempt.get("ended_at"))
            if ended is None:
                recovery_reasons.append("completed attempt has no valid ended_at")
                recovery_age = None
            else:
                recovery_age = (
                    datetime.now(timezone.utc) - ended
                ).total_seconds()
                if recovery_age < 0:
                    recovery_reasons.append(
                        "completed attempt ended_at is in the future"
                    )
                elif recovery_age > tmux_exit_code_grace_seconds:
                    recovery_reasons.append(
                        f"dispatcher inter-write window is {recovery_age:.1f}s old"
                    )
            if not dispatch_attempt_matches:
                recovery_reasons.append("dispatch lock does not match the completed attempt")
            if dispatch_pid_alive is not True:
                recovery_reasons.append("dispatch pid is not alive")
            try:
                resolved = resolve_task_artifacts(
                    task_dir,
                    status,
                    require_publication=True,
                )
            except ArtifactResolutionError as exc:
                recovery_reasons.append(f"handoff publication is invalid: {exc}")
            else:
                bundle = resolved.bundle
                if (
                    bundle is None
                    or bundle.handoff.get("requested_state")
                    != attempt.get("handoff_state")
                ):
                    recovery_reasons.append(
                        "handoff publication does not match ATTEMPT.handoff_state"
                    )
            if recovery_reasons:
                violations.append(
                    f"{task_dir.name}: active STATUS with completed ATTEMPT is not a "
                    "recoverable dispatcher inter-write window: "
                    + "; ".join(recovery_reasons)
                )
            else:
                warnings.append(
                    f"{task_dir.name}: dispatcher inter-write handoff validation is between ATTEMPT and "
                    f"STATUS writes ({recovery_age:.1f}s old); replay may complete the transition"
                )
    elif dispatch_lock.exists():
        violations.append(f"{task_dir.name}: .dispatch-lock exists while STATUS state is {state!r}")
    if state == "strategy_review":
        if attempt_state != "completed" or attempt.get("handoff_valid") is not True or attempt.get("handoff_state") != "strategy_review":
            violations.append(
                f"{task_dir.name}: STATUS strategy_review requires completed attempt with handoff_state=strategy_review"
            )
        if attempt.get("exit_code") != 0:
            violations.append(f"{task_dir.name}: STATUS strategy_review requires worker exit_code=0")
        if artifact_legacy:
            request, request_reasons = load_handoff_request(task_dir)
            for reason in request_reasons:
                violations.append(f"{task_dir.name}: handoff request invalid: {reason}")
            if isinstance(request, dict) and request.get("requested_state") != "strategy_review":
                violations.append(
                    f"{task_dir.name}: STATUS strategy_review requires HANDOFF.json requested_state=strategy_review"
                )
    elif state == "verified":
        if profile != "direct":
            violations.append(f"{task_dir.name}: STATUS verified requires profile='direct'")
        if attempt_state != "completed" or attempt.get("handoff_valid") is not True or attempt.get("handoff_state") != "verified":
            violations.append(f"{task_dir.name}: STATUS verified requires completed attempt with handoff_state=verified")
        if attempt.get("exit_code") != 0:
            violations.append(f"{task_dir.name}: STATUS verified requires worker exit_code=0")
        if artifact_legacy:
            if not has_substantive_content(task_dir / "HANDOFF.md"):
                violations.append(f"{task_dir.name}: STATUS verified requires substantive HANDOFF.md")
            if not has_substantive_content(task_dir / "EVIDENCE.md"):
                violations.append(f"{task_dir.name}: STATUS verified requires substantive EVIDENCE.md")
            request, request_reasons = load_handoff_request(task_dir)
            for reason in request_reasons:
                violations.append(f"{task_dir.name}: handoff request invalid: {reason}")
            if isinstance(request, dict) and request.get("requested_state") != "verified":
                violations.append(f"{task_dir.name}: STATUS verified requires HANDOFF.json requested_state=verified")
    elif state == "review":
        if profile not in {"delegated", "full"}:
            violations.append(f"{task_dir.name}: STATUS review requires profile='delegated' or profile='full'")
        if attempt_state != "completed" or attempt.get("handoff_valid") is not True or attempt.get("handoff_state") != "review":
            violations.append(f"{task_dir.name}: STATUS review requires completed attempt with handoff_state=review")
        if attempt.get("exit_code") != 0:
            violations.append(f"{task_dir.name}: STATUS review requires worker exit_code=0")
        if artifact_legacy:
            if not has_substantive_content(task_dir / "HANDOFF.md"):
                violations.append(f"{task_dir.name}: STATUS review requires substantive HANDOFF.md")
            if not has_substantive_content(task_dir / "EVIDENCE.md"):
                violations.append(f"{task_dir.name}: STATUS review requires substantive EVIDENCE.md")
            request, request_reasons = load_handoff_request(task_dir)
            for reason in request_reasons:
                violations.append(f"{task_dir.name}: handoff request invalid: {reason}")
            if isinstance(request, dict) and request.get("requested_state") != "review":
                violations.append(f"{task_dir.name}: STATUS review requires HANDOFF.json requested_state=review")
    elif state == "blocked":
        if attempt_state == "completed":
            if attempt.get("handoff_valid") is not True or attempt.get("handoff_state") != "blocked":
                violations.append(f"{task_dir.name}: completed blocked task requires handoff_state=blocked")
            if artifact_legacy:
                request, request_reasons = load_handoff_request(task_dir)
                for reason in request_reasons:
                    violations.append(f"{task_dir.name}: handoff request invalid: {reason}")
                if isinstance(request, dict) and request.get("requested_state") != "blocked":
                    violations.append(f"{task_dir.name}: STATUS blocked requires HANDOFF.json requested_state=blocked")
        elif attempt_state == "invalid_handoff":
            if status.get("blocker_type") != "needs_coordinator":
                violations.append(f"{task_dir.name}: invalid_handoff blocked task requires blocker_type=needs_coordinator")
        else:
            violations.append(f"{task_dir.name}: STATUS blocked requires completed or invalid_handoff attempt")

    if profile == "full" and attempt.get("phase") == "execution":
        try:
            from strategy import canonical_digest, load_approved_strategy

            if state == "strategy_review":
                matching_review = False
                for review_path in (task_dir / "strategy").glob("REVIEW-v*.json"):
                    review = load_json(review_path)
                    if (
                        review.get("decision") == "approved"
                        and review.get("strategy_sha256") == attempt.get("strategy_sha256")
                    ):
                        matching_review = True
                        break
                if not matching_review:
                    violations.append(f"{task_dir.name}: revision attempt is not bound to a previously approved strategy")
            else:
                approved, review = load_approved_strategy(task_dir)
                digest = canonical_digest(approved)
                if attempt.get("strategy_id") != approved.get("strategy_id"):
                    violations.append(f"{task_dir.name}: execution attempt strategy_id does not match approved strategy")
                if attempt.get("strategy_sha256") != digest or review.get("strategy_sha256") != digest:
                    violations.append(f"{task_dir.name}: execution attempt strategy digest does not match approval")
        except Exception as exc:
            violations.append(f"{task_dir.name}: execution attempt has no valid approved strategy: {exc}")

    return violations, warnings, attempt


def validate_status(
    task_dir: Path,
    status: dict[str, Any],
    fsm: dict[str, Any],
    stale_created_minutes: float,
    tmux_exit_code_grace_seconds: int,
) -> tuple[list[str], list[str]]:
    violations: list[str] = []
    warnings: list[str] = []
    state = status.get("state")
    violations.extend(validate_status_schema(status, fsm, task_dir.name))
    violations.extend(validate_state_history(status, fsm, task_dir.name))

    if status_uses_legacy(status):
        evidence = status.get("evidence")
        if isinstance(evidence, dict):
            logs = evidence.get("logs", [])
            if isinstance(logs, list):
                for log_ref in logs:
                    log_path = task_dir / str(log_ref)
                    if not log_path.exists():
                        violations.append(f"{task_dir.name}: evidence log missing: {log_ref}")

        if state in {"approved", "merged"} and not has_substantive_content(task_dir / "EVIDENCE.md"):
            violations.append(f"{task_dir.name}: {state} task has missing or template-only EVIDENCE.md")

    attempt_id = status.get("current_attempt_id")
    if attempt_id and not (task_dir / "attempts" / str(attempt_id)).exists():
        violations.append(f"{task_dir.name}: current_attempt_id directory missing: {attempt_id}")
    attempt_violations, attempt_warnings, _ = validate_attempt(
        task_dir,
        status,
        stale_created_minutes,
        tmux_exit_code_grace_seconds,
    )
    violations.extend(attempt_violations)
    warnings.extend(attempt_warnings)

    lock = task_dir / "LOCK"
    if lock.exists() and attempt_id:
        text = lock.read_text(encoding="utf-8", errors="replace")
        if f"attempt_id: {attempt_id}" not in text:
            violations.append(f"{task_dir.name}: LOCK attempt_id does not match STATUS current_attempt_id")

    return violations, warnings


def validate_merged_task(
    root: Path,
    task_dir: Path,
    status: dict[str, Any],
    run_json: dict[str, Any],
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    if status.get("state") != "merged":
        return [], []
    task_id = status.get("task_id")
    matches = [
        item for item in events
        if item.get("event") == "task_merged" and item.get("task_id") == task_id
    ]
    if not matches:
        return [f"{task_dir.name}: merged task has no task_merged event"], []
    latest = matches[-1]
    commit = latest.get("commit")
    target_branch = latest.get("target_branch")
    violations: list[str] = []
    warnings: list[str] = []
    if not isinstance(commit, str) or not commit:
        violations.append(f"{task_dir.name}: task_merged event has no commit")
    if status_uses_v2(status):
        attempt_id = latest.get("attempt_id")
        current_attempt_id = status.get("current_attempt_id")
        binding = latest.get("artifact_binding")
        if not isinstance(attempt_id, str) or not attempt_id:
            violations.append(f"{task_dir.name}: v2 task_merged event has no attempt_id")
        elif attempt_id != current_attempt_id:
            violations.append(
                f"{task_dir.name}: v2 task_merged event attempt_id does not match STATUS current_attempt_id"
            )
        if not isinstance(binding, dict):
            violations.append(f"{task_dir.name}: v2 task_merged event has no artifact_binding")
        elif isinstance(commit, str) and commit:
            expected_state = "verified" if status.get("profile", "full") == "direct" else "review"
            try:
                validate_artifact_binding_for_task(
                    task_dir,
                    status,
                    binding,
                    expected_requested_state=expected_state,
                    expected_source_commit=commit,
                )
            except ArtifactResolutionError as exc:
                violations.append(
                    f"{task_dir.name}: v2 task_merged artifact binding is invalid: {exc}"
                )
        if latest.get("source_branch") != status.get("branch"):
            violations.append(
                f"{task_dir.name}: v2 task_merged source_branch does not match STATUS branch"
            )
        if status.get("profile", "full") != "direct":
            _, review_violations = _load_v2_approved_review(
                task_dir,
                status,
                expected_commit=commit if isinstance(commit, str) and commit else None,
            )
            violations.extend(review_violations)
    elif status.get("profile", "full") != "direct":
        try:
            pointer = load_json(task_dir / "reviews" / "CURRENT_TASK_REVIEW.json")
            decision_path = task_dir / str(pointer.get("decision_path") or "")
            decision = load_json(decision_path)
        except Exception as exc:
            violations.append(f"{task_dir.name}: merged task review binding is unreadable: {exc}")
        else:
            if decision.get("decision") != "approved" or decision.get("approved_commit") != commit:
                violations.append(
                    f"{task_dir.name}: merged commit does not match the current approved task review"
                )
    expected_target = run_json.get("target_branch")
    if target_branch != expected_target:
        violations.append(
            f"{task_dir.name}: task_merged target_branch {target_branch!r} "
            f"does not match RUN.json {expected_target!r}"
        )
    if isinstance(commit, str) and commit and isinstance(expected_target, str) and expected_target:
        contained = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, expected_target],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        if not contained:
            violations.append(
                f"{task_dir.name}: target branch {expected_target!r} does not contain merged commit {commit}"
            )
    verification = latest.get("verification")
    if isinstance(verification, dict) and verification.get("passed") is False:
        warnings.append(f"{task_dir.name}: post-merge verification failed; create a repair task")
    return violations, warnings


def _safe_v2_review_path(task_dir: Path, raw_ref: Any) -> Path:
    """Resolve one immutable review decision without permitting task escape/symlinks."""

    if not isinstance(raw_ref, str) or not raw_ref or "\\" in raw_ref:
        raise ValueError("decision_path must be a non-empty relative POSIX path")
    parts = raw_ref.split("/")
    if parts[0] != "reviews" or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("decision_path must be a normalized file below reviews/")
    candidate = task_dir.joinpath(*parts)
    current = task_dir
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("decision_path must not traverse a symlink")
    if not candidate.is_file():
        raise ValueError("decision_path does not name a regular file")
    return candidate


def _load_v2_approved_review(
    task_dir: Path,
    status: dict[str, Any],
    *,
    expected_commit: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate the pointer, digest, decision, Git commit, and v2 bundle binding."""

    label = f"{task_dir.name}: approved task review"
    try:
        pointer = load_json(task_dir / "reviews" / "CURRENT_TASK_REVIEW.json")
        if not isinstance(pointer, dict):
            raise ValueError("CURRENT_TASK_REVIEW.json must be a JSON object")
        decision_path = _safe_v2_review_path(task_dir, pointer.get("decision_path"))
        declared_digest = pointer.get("decision_sha256")
        if not isinstance(declared_digest, str) or len(declared_digest) != 64:
            raise ValueError("CURRENT_TASK_REVIEW.json decision_sha256 must be a SHA-256 digest")
        decision_bytes = decision_path.read_bytes()
        actual_digest = hashlib.sha256(decision_bytes).hexdigest()
        if declared_digest != actual_digest:
            raise ValueError("CURRENT_TASK_REVIEW.json decision_sha256 does not match the decision")
        decision = json.loads(decision_bytes)
        if not isinstance(decision, dict):
            raise ValueError("review decision must be a JSON object")
    except Exception as exc:
        return None, [f"{label} binding is unreadable: {exc}"]

    violations: list[str] = []
    if pointer.get("revision") != decision.get("revision"):
        violations.append(f"{label} pointer revision does not match the decision")
    if decision.get("schema_version") != 2 or decision.get("artifact_protocol_version") != 2:
        violations.append(f"{label} decision is not Artifact Protocol v2")
    if decision.get("task_id") != status.get("task_id"):
        violations.append(f"{label} decision task_id does not match STATUS")
    if decision.get("decision") != "approved":
        violations.append(f"{label} decision is not approved")

    approved_commit = decision.get("approved_commit")
    if not isinstance(approved_commit, str) or not approved_commit:
        violations.append(f"{label} decision has no approved_commit")
    elif expected_commit is not None and approved_commit != expected_commit:
        violations.append(f"{label} approved_commit does not match the merged commit")
    if decision.get("source_branch") != status.get("branch"):
        violations.append(f"{label} source_branch does not match STATUS branch")

    binding = decision.get("artifact_binding")
    if not isinstance(binding, dict):
        violations.append(f"{label} decision has no artifact_binding")
    elif isinstance(approved_commit, str) and approved_commit:
        try:
            validate_artifact_binding_for_task(
                task_dir,
                status,
                binding,
                expected_requested_state="review",
                expected_source_commit=approved_commit,
            )
        except ArtifactResolutionError as exc:
            violations.append(f"{label} artifact binding is invalid: {exc}")
    return decision, violations


def validate_approved_task(task_dir: Path, status: dict[str, Any]) -> list[str]:
    if status.get("state") != "approved" or status.get("profile", "full") == "direct":
        return []
    if status_uses_v2(status):
        _, violations = _load_v2_approved_review(task_dir, status)
        return violations
    if not status_uses_legacy(status):
        return []
    try:
        pointer = load_json(task_dir / "reviews" / "CURRENT_TASK_REVIEW.json")
        decision = load_json(task_dir / str(pointer.get("decision_path") or ""))
    except Exception as exc:
        return [f"{task_dir.name}: approved task review binding is unreadable: {exc}"]
    required = {
        "approved_commit", "source_branch", "target_branch",
        "target_commit_at_review", "evidence_sha256", "handoff_sha256",
    }
    missing = sorted(field for field in required if not decision.get(field))
    if decision.get("decision") != "approved" or missing:
        return [f"{task_dir.name}: approved task review is missing Git binding fields: {missing}"]
    return []


def collect(
    run_id: str,
    stale_lock_hours: float,
    stale_created_minutes: float = 10.0,
    tmux_exit_code_grace_seconds: int = 60,
    config_warnings: list[str] | None = None,
    config_errors: list[str] | None = None,
) -> dict[str, Any]:
    root = repo_root(Path.cwd())
    run_dir = root / ".agent-collab" / "runs" / run_id
    if not run_dir.exists():
        raise SystemExit(f"Run not found: {run_dir}")

    fsm = load_fsm()
    tasks: list[dict[str, Any]] = []
    violations: list[str] = []
    warnings: list[str] = []
    stale_locks: list[str] = []
    stale_dispatch_locks: list[str] = []
    invalid_status_files: list[str] = []
    now_ts = datetime.now(timezone.utc).timestamp()

    events, event_violations, event_warnings = load_events(run_dir, run_id)
    violations.extend(event_violations)
    warnings.extend(event_warnings)
    warnings.extend(f"config: {warning}" for warning in (config_warnings or []))
    violations.extend(f"config: {error}" for error in (config_errors or []))
    run_json: dict[str, Any] = {}
    run_json_path = run_dir / "RUN.json"
    if not run_json_path.exists():
        violations.append("run: missing required RUN.json")
    else:
        try:
            run_json = load_json(run_json_path)
        except json.JSONDecodeError as exc:
            violations.append(f"run: invalid RUN.json: {exc}")
        else:
            recorded_package = run_json.get("package_version")
            if not recorded_package:
                warnings.append("run: RUN.json package_version missing; run may have been created by an older package")
            elif recorded_package != PACKAGE_VERSION:
                warnings.append(
                    f"run: RUN.json package_version {recorded_package!r} differs from installed package version {PACKAGE_VERSION!r}"
                )
            recorded_protocol = run_json.get("protocol_version")
            if recorded_protocol in LEGACY_PROTOCOL_VERSIONS or recorded_protocol is None:
                warnings.append(
                    f"run: RUN.json protocol_version {recorded_protocol!r} differs from installed {PROTOCOL_VERSION!r}"
                )
            elif recorded_protocol != PROTOCOL_VERSION:
                violations.append(
                    f"run: RUN.json protocol_version {recorded_protocol!r} is unsupported; "
                    f"expected {PROTOCOL_VERSION!r} or a recognized legacy version"
                )
    if not (run_dir / "JOURNAL.md").exists():
        violations.append("run: missing required JOURNAL.md")

    for task_dir in sorted((run_dir / "tasks").glob("*")):
        if not task_dir.is_dir():
            continue
        status_path = task_dir / "STATUS.json"
        if not status_path.exists():
            invalid_status_files.append(str(status_path))
            violations.append(f"{task_dir.name}: missing STATUS.json")
            continue
        try:
            status = load_json(status_path)
        except json.JSONDecodeError as exc:
            invalid_status_files.append(str(status_path))
            violations.append(f"{task_dir.name}: invalid STATUS.json: {exc}")
            continue
        if not isinstance(status, dict):
            invalid_status_files.append(str(status_path))
            violations.append(f"{task_dir.name}: STATUS.json must be a JSON object")
            tasks.append(
                {
                    "task_id": task_dir.name,
                    "state": None,
                    "owner": None,
                    "branch": None,
                    "worktree": None,
                    "current_attempt_id": None,
                    "needs_coordinator": None,
                    "blocker_type": None,
                    "blocking_reason": None,
                    "summary": None,
                    "lock": None,
                    "dispatch_lock": None,
                    "handoff_index": None,
                    "artifact_resolution": {
                        "valid": False,
                        "error": "STATUS.json must be a JSON object",
                    },
                }
            )
            continue

        recorded_protocol = run_json.get("protocol_version")
        if (
            recorded_protocol == PROTOCOL_VERSION
            and status.get("artifact_protocol_version") != 2
        ):
            violations.append(
                f"{task_dir.name}: current run requires STATUS.artifact_protocol_version=2"
            )
        elif (
            recorded_protocol in LEGACY_PROTOCOL_VERSIONS
            and status.get("artifact_protocol_version") == 2
        ):
            violations.append(
                f"{task_dir.name}: legacy run cannot contain an artifact-protocol-v2 task"
            )

        status_violations, status_warnings = validate_status(
            task_dir,
            status,
            fsm,
            stale_created_minutes,
            tmux_exit_code_grace_seconds,
        )
        violations.extend(status_violations)
        warnings.extend(status_warnings)
        violations.extend(
            validate_task_profile_binding(status, events, task_dir.name)
        )
        violations.extend(validate_approved_task(task_dir, status))
        merge_violations, merge_warnings = validate_merged_task(
            root, task_dir, status, run_json, events
        )
        violations.extend(merge_violations)
        warnings.extend(merge_warnings)

        lock = task_dir / "LOCK"
        lock_info = None
        if lock.exists():
            age_hours = (now_ts - lock.stat().st_mtime) / 3600
            lock_info = {"path": str(lock), "age_hours": round(age_hours, 2)}
            if age_hours > stale_lock_hours:
                stale_locks.append(str(lock))
        dispatch_lock = task_dir / ".dispatch-lock"
        dispatch_lock_info = None
        if dispatch_lock.exists():
            age_hours = (now_ts - dispatch_lock.stat().st_mtime) / 3600
            dispatch_lock_info = {"path": str(dispatch_lock), "age_hours": round(age_hours, 2)}
            if age_hours > stale_lock_hours:
                stale_dispatch_locks.append(str(dispatch_lock))
            pid_path = dispatch_lock / "pid"
            if not pid_path.exists():
                if status.get("state") != "running":
                    warnings.append(f"{task_dir.name}: .dispatch-lock missing pid")
            else:
                pid_text = pid_path.read_text(encoding="utf-8", errors="replace").strip()
                try:
                    pid = int(pid_text)
                except ValueError:
                    if status.get("state") != "running":
                        warnings.append(f"{task_dir.name}: .dispatch-lock pid is not an integer: {pid_text!r}")
                else:
                    if not pid_is_alive(pid):
                        if status.get("state") != "running":
                            warnings.append(f"{task_dir.name}: .dispatch-lock pid is not alive: {pid}")

        handoff_index: dict[str, Any] | None = None
        artifact_resolution: dict[str, Any]
        try:
            resolved_artifacts = resolve_task_artifacts(task_dir, status)
        except ArtifactResolutionError as exc:
            violations.append(f"{task_dir.name}: artifact resolution failed: {exc}")
            artifact_resolution = {
                "valid": False,
                "protocol": status.get("artifact_protocol_version"),
                "error": str(exc),
            }
        else:
            handoff_index = resolved_artifacts.handoff_index
            artifact_resolution = {"valid": True, **resolved_artifacts.as_dict()}
            warnings.extend(
                f"{task_dir.name}: {warning}" for warning in resolved_artifacts.warnings
            )

        tasks.append(
            {
                "task_id": status.get("task_id", task_dir.name),
                "state": status.get("state"),
                "owner": status.get("owner"),
                "branch": status.get("branch"),
                "worktree": status.get("worktree"),
                "current_attempt_id": status.get("current_attempt_id"),
                "needs_coordinator": status.get("needs_coordinator"),
                "blocker_type": status.get("blocker_type"),
                "blocking_reason": status.get("blocking_reason"),
                "summary": status.get("summary"),
                "handoff_index": handoff_index,
                "artifact_resolution": artifact_resolution,
                "lock": lock_info,
                "dispatch_lock": dispatch_lock_info,
            }
        )

    counts = dict(Counter(task["state"] for task in tasks))
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "collected_at": utc_now(),
        "valid": not violations and not invalid_status_files,
        "counts": counts,
        "tasks": tasks,
        "blocked": [task for task in tasks if task["state"] == "blocked"],
        "ready_for_review": [task for task in tasks if task["state"] == "review"],
        "invalid_status_files": invalid_status_files,
        "stale_locks": stale_locks,
        "stale_dispatch_locks": stale_dispatch_locks,
        "protocol_violations": violations,
        "protocol_warnings": warnings,
        "recent_events": events[-10:],
    }


def render_human(report: dict[str, Any]) -> str:
    lines = [
        f"Run: {report['run_id']}",
        f"Collected: {report['collected_at']}",
        f"Valid: {report['valid']}",
        "",
        "Counts:",
    ]
    if report["counts"]:
        for state, count in sorted(report["counts"].items()):
            lines.append(f"  {state}: {count}")
    else:
        lines.append("  no tasks")

    lines.append("")
    lines.append("Tasks:")
    for task in report["tasks"]:
        blocker = f" blocker={task['blocker_type']}" if task.get("blocker_type") else ""
        handoff = ""
        if isinstance(task.get("handoff_index"), dict) and not task["handoff_index"].get("template"):
            handoff = " handoff_json=yes"
        lock = " lock=yes" if task.get("lock") else ""
        dispatch_lock = " dispatch_lock=yes" if task.get("dispatch_lock") else ""
        lines.append(
            f"  {task['task_id']}: {task['state']} attempt={task.get('current_attempt_id')}"
            f"{blocker}{handoff}{lock}{dispatch_lock}"
        )

    if report["protocol_violations"]:
        lines.append("")
        lines.append("Protocol violations:")
        for violation in report["protocol_violations"]:
            lines.append(f"  - {violation}")
    if report["protocol_warnings"]:
        lines.append("")
        lines.append("Protocol warnings:")
        for warning in report["protocol_warnings"]:
            lines.append(f"  - {warning}")

    if report["stale_locks"]:
        lines.append("")
        lines.append("Stale locks:")
        for lock in report["stale_locks"]:
            lines.append(f"  - {lock}")
    if report["stale_dispatch_locks"]:
        lines.append("")
        lines.append("Stale dispatch locks:")
        for lock in report["stale_dispatch_locks"]:
            lines.append(f"  - {lock}")

    if report["recent_events"]:
        lines.append("")
        lines.append("Recent events:")
        for event in report["recent_events"][-5:]:
            lines.append(f"  - {event.get('at', '')} {event.get('event', '')} {event.get('task_id', '')}")

    return "\n".join(lines) + "\n"


def render_summary(report: dict[str, Any]) -> str:
    rows = ["| Task | State | Owner | Attempt | Handoff | Blocker | Review |", "|---|---|---|---|---|---|---|"]
    for task in report["tasks"]:
        review = "ready" if task["state"] == "review" else ""
        handoff_summary = ""
        if isinstance(task.get("handoff_index"), dict) and not task["handoff_index"].get("template"):
            handoff_summary = str(task["handoff_index"].get("summary") or "")[:80]
        rows.append(
            f"| {task['task_id']} | {task['state']} | {task.get('owner') or ''} | "
            f"{task.get('current_attempt_id') or ''} | {handoff_summary} | {task.get('blocker_type') or ''} | {review} |"
        )

    blockers = ["| Task | Type | Reason |", "|---|---|---|"]
    for task in report["blocked"]:
        blockers.append(f"| {task['task_id']} | {task.get('blocker_type') or ''} | {task.get('blocking_reason') or ''} |")

    warnings = report["protocol_violations"] or ["None"]
    protocol_warnings = report["protocol_warnings"] or ["None"]
    return "\n".join(
        [
            "# Run Summary",
            "",
            "## Objective",
            "",
            "See `RUN.json`.",
            "",
            "## Current Status",
            "",
            f"- Collected: {report['collected_at']}",
            f"- Valid: {report['valid']}",
            f"- Counts: {report['counts']}",
            "",
            "## Task Board",
            "",
            *rows,
            "",
            "## Active Blockers",
            "",
            *blockers,
            "",
            "## Ready For Codex Review",
            "",
            *(f"- {task['task_id']}" for task in report["ready_for_review"]),
            "",
            "## Protocol Warnings",
            "",
            *(f"- {warning}" for warning in warnings),
            "",
            "## Protocol Non-Fatal Warnings",
            "",
            *(f"- {warning}" for warning in protocol_warnings),
            "",
            "## Recent Decisions",
            "",
            "## Recent Events",
            "",
            *(f"- {event.get('at', '')} `{event.get('event', '')}` {event.get('task_id', '')}" for event in report["recent_events"][-10:]),
            "",
            "## Experiment Results",
            "",
            "See `RESULT_LEDGER.md`.",
            "",
            "## Next Actions",
            "",
        ]
    )


def write_diagnostics(report: dict[str, Any]) -> None:
    run_dir = Path(report["run_dir"])
    diagnostics = run_dir / "diagnostics"
    diagnostics.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = diagnostics / f"collect-status-{stamp}.json"
    md_path = diagnostics / f"collect-status-{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_human(report), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect run status without mutating task state.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--write-summary", action="store_true", help="Write derived SUMMARY.md.")
    parser.add_argument("--write-diagnostics", action="store_true", help="Write derived diagnostics files.")
    parser.add_argument("--stale-lock-hours", type=float, default=None)
    parser.add_argument("--stale-created-minutes", type=float, default=None)
    args = parser.parse_args()

    root = repo_root(Path.cwd())
    config_result = load_config(root)
    config = config_result.config
    stale_lock_hours = args.stale_lock_hours if args.stale_lock_hours is not None else config.stale_lock_hours
    stale_created_minutes = (
        args.stale_created_minutes if args.stale_created_minutes is not None else config.stale_created_minutes
    )
    report = collect(
        args.run_id,
        stale_lock_hours,
        stale_created_minutes,
        config.tmux_exit_code_grace_seconds,
        config_result.warnings,
        config_result.errors,
    )

    if args.write_summary:
        Path(report["run_dir"], "SUMMARY.md").write_text(render_summary(report), encoding="utf-8")
    if args.write_diagnostics:
        write_diagnostics(report)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_human(report), end="")

    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
