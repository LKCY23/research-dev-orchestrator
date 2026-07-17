#!/usr/bin/env python3
"""Deterministic task-level cumulative budget accounting for protocol v2."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from supervisor import validate_attempt_deadline_payload


TASK_BUDGET_FIELDS = {
    "max_attempts",
    "max_execution_seconds",
    "max_cost_usd",
}


class TaskBudgetError(ValueError):
    """Raised when cumulative budget policy or evidence is unsafe."""


def _canonical_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise TaskBudgetError(f"unsafe or missing budget evidence: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise TaskBudgetError(f"unreadable budget evidence {path}: {exc}") from exc


def _json_file(path: Path) -> tuple[dict[str, Any], str]:
    raw = _regular_bytes(path)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskBudgetError(f"invalid JSON budget evidence {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TaskBudgetError(f"budget evidence must be an object: {path}")
    return payload, hashlib.sha256(raw).hexdigest()


def normalize_task_budget(value: Any) -> dict[str, int | float] | None:
    """Validate the optional v2 cumulative task budget object."""

    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise TaskBudgetError("task_budget must be null or a non-empty object")
    unknown = sorted(set(value) - TASK_BUDGET_FIELDS)
    if unknown:
        raise TaskBudgetError(f"task_budget has unknown fields: {unknown}")
    normalized: dict[str, int | float] = {}
    for field in ("max_attempts", "max_execution_seconds"):
        if field not in value:
            continue
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise TaskBudgetError(f"task_budget.{field} must be a positive integer")
        normalized[field] = item
    if "max_cost_usd" in value:
        item = value["max_cost_usd"]
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or float(item) <= 0
        ):
            raise TaskBudgetError("task_budget.max_cost_usd must be finite and positive")
        normalized["max_cost_usd"] = float(item)
    return normalized


def _trusted_execution_seconds(attempt_dir: Path) -> tuple[float, dict[str, str]]:
    deadline_path = attempt_dir / "runtime" / "DEADLINE.json"
    supervisor_path = attempt_dir / "supervisor-result.json"
    deadline, deadline_sha = _json_file(deadline_path)
    receipt, receipt_sha = _json_file(supervisor_path)
    try:
        deadline = validate_attempt_deadline_payload(deadline)
    except ValueError as exc:
        raise TaskBudgetError(f"{attempt_dir.name}: invalid DEADLINE.json: {exc}") from exc
    if receipt.get("deadline_sha256") != deadline_sha:
        raise TaskBudgetError(f"{attempt_dir.name}: supervisor deadline binding is invalid")
    for field, expected in (
        ("attempt_started_at_epoch", deadline["started_at_epoch"]),
        ("execution_deadline_at_epoch", deadline["execution_deadline_at_epoch"]),
    ):
        actual = receipt.get(field)
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not math.isclose(float(actual), float(expected), rel_tol=0, abs_tol=1e-6)
        ):
            raise TaskBudgetError(f"{attempt_dir.name}: supervisor {field} binding is invalid")

    elapsed = receipt.get("execution_elapsed_seconds")
    if elapsed is None:
        # T1 receipts created before task budgets can still prove the stop point
        # through the coordinator-owned deadline state or finalization marker.
        deadline_state = receipt.get("deadline")
        if isinstance(deadline_state, dict):
            finalization_started = deadline_state.get("finalization_started_at_epoch")
            remaining = deadline_state.get("remaining_seconds")
            if isinstance(finalization_started, (int, float)) and not isinstance(
                finalization_started, bool
            ):
                elapsed = float(finalization_started) - float(deadline["started_at_epoch"])
            elif (
                deadline_state.get("phase") == "execution"
                and isinstance(remaining, (int, float))
                and not isinstance(remaining, bool)
            ):
                # Legacy T1 state rounded remaining time to milliseconds.
                # Charge the rounding interval conservatively rather than
                # allowing a cumulative hard limit to undercount it.
                elapsed = min(
                    float(deadline["attempt_wall_seconds"]),
                    float(deadline["attempt_wall_seconds"]) - float(remaining) + 0.001,
                )
        if elapsed is None:
            marker_path = attempt_dir / "runtime" / "FINALIZATION.json"
            if marker_path.exists():
                marker, _marker_sha = _json_file(marker_path)
                if marker.get("deadline_sha256") != deadline_sha:
                    raise TaskBudgetError(
                        f"{attempt_dir.name}: finalization deadline binding is invalid"
                    )
                started = marker.get("started_at_epoch")
                if isinstance(started, (int, float)) and not isinstance(started, bool):
                    elapsed = float(started) - float(deadline["started_at_epoch"])
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(float(elapsed))
        or float(elapsed) < 0
        or float(elapsed) > float(deadline["attempt_wall_seconds"]) + 1e-3
    ):
        raise TaskBudgetError(f"{attempt_dir.name}: trusted execution elapsed is unavailable")
    return round(float(elapsed), 6), {
        "deadline_sha256": deadline_sha,
        "supervisor_sha256": receipt_sha,
    }


def _last_usage_record(runtime: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = runtime / "USAGE.ndjson"
    if not path.exists():
        return None, None
    raw = _regular_bytes(path)
    records: list[dict[str, Any]] = []
    try:
        for line in raw.splitlines():
            if line.strip():
                item = json.loads(line)
                if isinstance(item, dict):
                    records.append(item)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskBudgetError(f"invalid usage telemetry {path}: {exc}") from exc
    return (records[-1] if records else None), hashlib.sha256(raw).hexdigest()


def _trusted_cost(attempt_dir: Path, *, backend_id: str) -> tuple[float, dict[str, str]]:
    receipt, receipt_sha = _json_file(attempt_dir / "supervisor-result.json")
    usage = receipt.get("usage")
    source: dict[str, str] = {"supervisor_sha256": receipt_sha}
    if not isinstance(usage, dict):
        usage, usage_sha = _last_usage_record(attempt_dir / "runtime")
        if usage_sha is not None:
            source["usage_sha256"] = usage_sha
    totals = usage.get("totals") if isinstance(usage, dict) else None
    observed = usage.get("observed_metrics") if isinstance(usage, dict) else None
    source_events = usage.get("source_events") if isinstance(usage, dict) else None
    if not isinstance(source_events, list):
        source_event = usage.get("source_event") if isinstance(usage, dict) else None
        source_events = [source_event] if isinstance(source_event, str) and source_event else []
    trusted_source = bool(source_events)
    if backend_id == "claude-code":
        trusted_source = "result" in source_events
    elif backend_id != "opencode":
        trusted_source = False
    cost = totals.get("cost_usd") if isinstance(totals, dict) else None
    if (
        not isinstance(observed, list)
        or "cost_usd" not in observed
        or not trusted_source
        or isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or float(cost) < 0
    ):
        raise TaskBudgetError(f"{attempt_dir.name}: trusted cost observation is unavailable")
    return float(cost), source


def assess_task_budget(
    task_dir: Path,
    *,
    requested_attempt_wall_seconds: float | None = None,
    next_attempt_id: str | None = None,
    artifact_protocol_version: int = 2,
) -> dict[str, Any]:
    """Derive one deterministic admission snapshot from frozen attempt evidence."""

    task_dir = Path(task_dir).resolve()
    policy, policy_sha = _json_file(task_dir / "EXECUTION_POLICY.json")
    if (
        artifact_protocol_version != 2
        or policy.get("schema_version") != 2
        or "task_budget" not in policy
    ):
        limits = None
    else:
        limits = normalize_task_budget(policy.get("task_budget"))
    if limits is None:
        return {
            "schema_version": 1,
            "artifact_protocol_version": artifact_protocol_version,
            "enabled": False,
            "limits": None,
            "consumed": None,
            "remaining": None,
            "observation_missing": [],
            "source_attempts": [],
            "admission": {
                "allowed": True,
                "reasons": [],
                "blocker_type": None,
                "blocking_reason": None,
                "attempt_wall_seconds": requested_attempt_wall_seconds,
                "max_cost_usd": None,
            },
            "execution_policy_sha256": policy_sha,
            "next_attempt_id": next_attempt_id,
        }

    attempts_root = task_dir / "attempts"
    sources: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    attempts_consumed = 0
    execution_consumed = 0.0
    cost_consumed = 0.0
    if attempts_root.exists():
        for attempt_dir in sorted(attempts_root.iterdir(), key=lambda path: path.name):
            attempt_path = attempt_dir / "ATTEMPT.json"
            if not attempt_dir.is_dir() or attempt_dir.is_symlink() or not attempt_path.exists():
                continue
            attempts_consumed += 1
            source: dict[str, Any] = {"attempt_id": attempt_dir.name}
            try:
                attempt, attempt_sha = _json_file(attempt_path)
                source["attempt_sha256"] = attempt_sha
                if attempt.get("attempt_id") != attempt_dir.name:
                    raise TaskBudgetError("ATTEMPT attempt_id does not match its directory")
            except TaskBudgetError as exc:
                missing.append(
                    {"attempt_id": attempt_dir.name, "dimension": "attempt", "reason": str(exc)}
                )
                sources.append(source)
                continue
            if "max_execution_seconds" in limits:
                try:
                    elapsed, evidence = _trusted_execution_seconds(attempt_dir)
                    execution_consumed += elapsed
                    source["execution_seconds"] = elapsed
                    source.update(evidence)
                except TaskBudgetError as exc:
                    missing.append(
                        {"attempt_id": attempt_dir.name, "dimension": "execution", "reason": str(exc)}
                    )
            if "max_cost_usd" in limits:
                try:
                    cost, evidence = _trusted_cost(
                        attempt_dir,
                        backend_id=str(attempt.get("backend_id") or attempt.get("agent") or ""),
                    )
                    cost_consumed += cost
                    source["cost_usd"] = cost
                    source.update(evidence)
                except TaskBudgetError as exc:
                    missing.append(
                        {"attempt_id": attempt_dir.name, "dimension": "cost", "reason": str(exc)}
                    )
            sources.append(source)

    consumed: dict[str, int | float | None] = {}
    remaining: dict[str, int | float | None] = {}
    if "max_attempts" in limits:
        consumed["attempts"] = attempts_consumed
        remaining["attempts"] = max(0, int(limits["max_attempts"]) - attempts_consumed)
    if "max_execution_seconds" in limits:
        execution_missing = any(item["dimension"] == "execution" for item in missing)
        consumed["execution_seconds"] = None if execution_missing else round(execution_consumed, 6)
        remaining["execution_seconds"] = (
            None
            if execution_missing
            else max(0.0, round(float(limits["max_execution_seconds"]) - execution_consumed, 6))
        )
    if "max_cost_usd" in limits:
        cost_missing = any(item["dimension"] == "cost" for item in missing)
        consumed["cost_usd"] = None if cost_missing else round(cost_consumed, 9)
        remaining["cost_usd"] = (
            None
            if cost_missing
            else max(0.0, round(float(limits["max_cost_usd"]) - cost_consumed, 9))
        )

    reasons: list[dict[str, Any]] = []
    for item in missing:
        reasons.append({"code": "observation_missing", **item})
    if remaining.get("attempts") == 0:
        reasons.append({"code": "task_budget_exhausted", "dimension": "attempts"})
    if remaining.get("execution_seconds") == 0:
        reasons.append({"code": "task_budget_exhausted", "dimension": "execution_seconds"})
    if remaining.get("cost_usd") == 0:
        reasons.append({"code": "task_budget_exhausted", "dimension": "cost_usd"})

    effective_wall = requested_attempt_wall_seconds
    execution_remaining = remaining.get("execution_seconds")
    if isinstance(execution_remaining, (int, float)) and execution_remaining > 0:
        effective_wall = (
            float(execution_remaining)
            if effective_wall is None
            else min(float(effective_wall), float(execution_remaining))
        )
    cost_remaining = remaining.get("cost_usd")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_protocol_version": 2,
        "enabled": True,
        "limits": limits,
        "consumed": consumed,
        "remaining": remaining,
        "observation_missing": missing,
        "source_attempts": sources,
        "admission": {
            "allowed": not reasons,
            "reasons": reasons,
            "blocker_type": "budget" if reasons else None,
            "blocking_reason": (
                "task cumulative budget denied: "
                + "; ".join(
                    f"{item['code']}:{item.get('dimension', 'unknown')}"
                    + (f":{item['attempt_id']}" if item.get("attempt_id") else "")
                    for item in reasons
                )
                if reasons
                else None
            ),
            "attempt_wall_seconds": effective_wall,
            "max_cost_usd": cost_remaining if isinstance(cost_remaining, (int, float)) else None,
        },
        "execution_policy_sha256": policy_sha,
        "next_attempt_id": next_attempt_id,
    }
    payload["assessment_sha256"] = _digest(payload)
    return payload


def validate_assessment(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise TaskBudgetError("task budget assessment has an unsupported schema")
    if payload.get("artifact_protocol_version") != 2:
        raise TaskBudgetError("task budget assessment requires artifact protocol v2")
    if payload.get("enabled") is not True:
        raise TaskBudgetError("only enabled task budget assessments can be frozen")
    expected = payload.get("assessment_sha256")
    unsigned = dict(payload)
    unsigned.pop("assessment_sha256", None)
    if not isinstance(expected, str) or expected != _digest(unsigned):
        raise TaskBudgetError("task budget assessment digest is invalid")
    return dict(payload)


def attempt_budget_binding_reasons(
    attempt_dir: Path,
    attempt: Mapping[str, Any],
) -> list[str]:
    """Validate the immutable assessment, ATTEMPT, inputs, and profile binding."""

    attempt_dir = Path(attempt_dir)
    ref = attempt.get("task_budget_ref")
    file_digest = attempt.get("task_budget_sha256")
    assessment_digest = attempt.get("task_budget_assessment_sha256")
    profile_budget: Any = None
    profile_path = attempt_dir / "runtime" / "BACKEND_PROFILE.json"
    try:
        if profile_path.exists():
            profile, _profile_sha = _json_file(profile_path)
            profile_budget = profile.get("task_budget")
    except TaskBudgetError as exc:
        return [str(exc)]
    present = [bool(ref), bool(file_digest), bool(assessment_digest)]
    enabled = isinstance(profile_budget, dict)
    if not any(present) and not enabled:
        return []
    if not all(present):
        return ["ATTEMPT task budget binding fields must be supplied together"]
    if ref != "runtime/TASK_BUDGET.json":
        return ["ATTEMPT task_budget_ref is invalid"]
    reasons: list[str] = []
    try:
        payload, actual_digest = _json_file(attempt_dir / str(ref))
        assessment = validate_assessment(payload)
    except TaskBudgetError as exc:
        return [str(exc)]
    if actual_digest != file_digest:
        reasons.append("TASK_BUDGET.json digest does not match ATTEMPT")
    if assessment.get("assessment_sha256") != assessment_digest:
        reasons.append("task budget assessment digest does not match ATTEMPT")
    if assessment.get("next_attempt_id") != attempt.get("attempt_id"):
        reasons.append("task budget assessment is bound to another attempt")
    if assessment.get("admission", {}).get("allowed") is not True:
        reasons.append("task budget assessment did not admit the attempt")
    try:
        inputs, _inputs_sha = _json_file(attempt_dir / "TASK_INPUTS.json")
    except TaskBudgetError as exc:
        reasons.append(str(exc))
    else:
        policy_digest = inputs.get("inputs", {}).get("execution_policy", {}).get("sha256")
        if assessment.get("execution_policy_sha256") != policy_digest:
            reasons.append("task budget assessment is not bound to frozen execution policy")
    if not enabled:
        reasons.append("backend profile is missing the task budget binding")
    elif profile_budget.get("assessment_sha256") != assessment_digest:
        reasons.append("backend profile task budget digest does not match ATTEMPT")
    return reasons


def attempt_budget_receipt_reasons(
    attempt_dir: Path,
    attempt: Mapping[str, Any],
) -> list[str]:
    """Require terminal evidence for each cumulative metered dimension."""

    ref = attempt.get("task_budget_ref")
    if ref != "runtime/TASK_BUDGET.json":
        return []
    try:
        assessment, _assessment_sha = _json_file(Path(attempt_dir) / str(ref))
        assessment = validate_assessment(assessment)
    except TaskBudgetError as exc:
        return [f"task budget assessment is unavailable: {exc}"]
    limits = assessment.get("limits")
    if not isinstance(limits, dict):
        return ["task budget limits are unavailable"]
    reasons: list[str] = []
    if "max_execution_seconds" in limits:
        try:
            _trusted_execution_seconds(Path(attempt_dir))
        except TaskBudgetError as exc:
            reasons.append(f"task budget execution observation is unavailable: {exc}")
    if "max_cost_usd" in limits:
        try:
            _trusted_cost(
                Path(attempt_dir),
                backend_id=str(attempt.get("backend_id") or attempt.get("agent") or ""),
            )
        except TaskBudgetError as exc:
            reasons.append(f"task budget cost observation is unavailable: {exc}")
    return reasons


def write_assessment_immutable(attempt_dir: Path, payload: Mapping[str, Any]) -> tuple[Path, str]:
    assessment = validate_assessment(payload)
    if assessment.get("admission", {}).get("allowed") is not True:
        raise TaskBudgetError("denied task budget assessment cannot be frozen")
    if assessment.get("next_attempt_id") != Path(attempt_dir).name:
        raise TaskBudgetError("task budget assessment is bound to another attempt")
    path = Path(attempt_dir) / "runtime" / "TASK_BUDGET.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise TaskBudgetError("TASK_BUDGET.json already exists")
    raw = _canonical_bytes(assessment)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".TASK_BUDGET.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path, hashlib.sha256(raw).hexdigest()
