#!/usr/bin/env python3
"""Deterministic execution-policy and strategy protocol helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from protocol import WORKER_BACKENDS, is_int_not_bool, is_non_empty_string, load_json, utc_now, write_json
from task_budget import TaskBudgetError, normalize_task_budget


class StrategyValidationError(ValueError):
    """Raised when policy, strategy, or review data violates the protocol."""


DEFAULT_EXECUTION_POLICY: dict[str, Any] = {
    "schema_version": 2,
    "strategy_required": True,
    "attempt_wall_seconds": 2700,
    "max_workflows": 6,
    "max_workflow_instances": 12,
    "max_parallel_workflows": 2,
    "max_subagents": 4,
    "max_parallel_subagents": 2,
    "default_command_seconds": 120,
    "max_enumerated_cases": 10000,
    "allow_unbounded_search": False,
    "allowed_paths": ["."],
    "read_paths": ["."],
    "forbidden_paths": [],
    "context_sources": [],
    "task_budget": None,
}

WORKFLOW_TIMEOUT_ACTIONS = {"block", "request_revision", "continue_without_result"}
WORKFLOW_EXECUTOR_MODES = {"primary_worker", "managed_subagents", "native_subagents"}
WORKFLOW_RESUME_MODES = {"reuse", "revalidate"}
RESOURCE_BUDGET_FIELDS = {
    "max_model_turns",
    "max_input_tokens",
    "max_output_tokens",
    "max_cost_usd",
    "max_context_tokens",
    "first_workflow_start_seconds",
    "max_no_progress_turns",
}


def _positive_int(payload: dict[str, Any], field: str, *, allow_zero: bool = False) -> int:
    value = payload.get(field)
    minimum = 0 if allow_zero else 1
    if not is_int_not_bool(value) or value < minimum:
        comparator = "non-negative" if allow_zero else "positive"
        raise StrategyValidationError(f"{field} must be a {comparator} integer")
    return value


def validate_execution_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise StrategyValidationError("execution policy must be a JSON object")
    schema_version = policy.get("schema_version")
    if schema_version not in {1, 2}:
        raise StrategyValidationError("execution policy schema_version must be 1 or 2")
    if not isinstance(policy.get("strategy_required"), bool):
        raise StrategyValidationError("strategy_required must be boolean")
    for field in (
        "attempt_wall_seconds",
        "max_workflows",
        "max_workflow_instances",
        "max_parallel_workflows",
        "max_subagents",
        "max_parallel_subagents",
        "default_command_seconds",
        "max_enumerated_cases",
    ):
        _positive_int(policy, field)
    if policy["max_parallel_workflows"] > policy["max_workflows"]:
        raise StrategyValidationError("max_parallel_workflows cannot exceed max_workflows")
    if policy["max_parallel_subagents"] > policy["max_subagents"]:
        raise StrategyValidationError("max_parallel_subagents cannot exceed max_subagents")
    if not isinstance(policy.get("allow_unbounded_search"), bool):
        raise StrategyValidationError("allow_unbounded_search must be boolean")
    for field in ("allowed_paths", "forbidden_paths"):
        values = policy.get(field)
        if not isinstance(values, list) or not all(is_non_empty_string(item) for item in values):
            raise StrategyValidationError(f"{field} must be a string list")
    read_paths = policy.get("read_paths", ["."])
    if not isinstance(read_paths, list) or not read_paths or not all(is_non_empty_string(item) for item in read_paths):
        raise StrategyValidationError("read_paths must be a non-empty string list")
    context_sources = policy.get("context_sources", [])
    if schema_version == 2 and "context_sources" not in policy:
        raise StrategyValidationError("schema 2 execution policy requires context_sources")
    if not isinstance(context_sources, list) or not all(
        is_non_empty_string(item) for item in context_sources
    ):
        raise StrategyValidationError("context_sources must be a string list")
    if schema_version == 1 and "task_budget" in policy:
        raise StrategyValidationError("task_budget is only supported by schema 2 execution policy")
    try:
        normalize_task_budget(policy.get("task_budget"))
    except TaskBudgetError as exc:
        raise StrategyValidationError(str(exc)) from exc
    return policy


def _normalize_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/") or "."
    if normalized.startswith("/") or normalized == ".." or normalized.startswith("../") or "/../" in normalized:
        raise StrategyValidationError(f"strategy paths must be relative and cannot traverse parents: {value!r}")
    return normalized


def _contains(parent: str, child: str) -> bool:
    return parent == "." or child == parent or child.startswith(parent + "/")


def load_execution_policy(task_dir: Path) -> dict[str, Any]:
    path = task_dir / "EXECUTION_POLICY.json"
    if not path.exists():
        raise StrategyValidationError(f"execution policy not found: {path}")
    return validate_execution_policy(load_json(path))


def _validate_workflow(workflow: Any, policy: dict[str, Any], backend_id: str) -> dict[str, Any]:
    if not isinstance(workflow, dict):
        raise StrategyValidationError("each workflow must be a JSON object")
    for field in ("workflow_id", "kind", "purpose"):
        if not is_non_empty_string(workflow.get(field)):
            raise StrategyValidationError(f"workflow {field} must be a non-empty string")
    if not isinstance(workflow.get("depends_on"), list) or not all(
        is_non_empty_string(item) for item in workflow["depends_on"]
    ):
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} depends_on must be a string list")
    if not isinstance(workflow.get("required"), bool):
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} required must be boolean")
    executor = workflow.get("executor")
    if not isinstance(executor, dict) or executor.get("mode") not in WORKFLOW_EXECUTOR_MODES:
        raise StrategyValidationError(
            f"workflow {workflow['workflow_id']} executor.mode must be one of {sorted(WORKFLOW_EXECUTOR_MODES)}"
        )
    if not isinstance(executor.get("write_access"), bool):
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} executor.write_access must be boolean")
    max_agents = executor.get("max_agents", 0)
    max_parallel = executor.get("max_parallel", 0)
    for name, value in (("max_agents", max_agents), ("max_parallel", max_parallel)):
        if not is_int_not_bool(value) or value < 0:
            raise StrategyValidationError(f"workflow {workflow['workflow_id']} executor.{name} must be non-negative")
    if max_agents > policy["max_subagents"] or max_parallel > policy["max_parallel_subagents"]:
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} exceeds subagent policy")
    if max_parallel > max_agents:
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} max_parallel exceeds max_agents")
    backend_options = executor.get("backend_options", {})
    if not isinstance(backend_options, dict):
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} backend_options must be an object")
    unknown_backends = sorted(set(backend_options) - {backend_id})
    if unknown_backends:
        raise StrategyValidationError(
            f"workflow {workflow['workflow_id']} has backend_options for another backend: {unknown_backends}"
        )
    selected_options = backend_options.get(backend_id, {})
    if not isinstance(selected_options, dict):
        raise StrategyValidationError(
            f"workflow {workflow['workflow_id']} backend_options.{backend_id} must be an object"
        )
    if backend_id == "claude-code":
        unknown_options = sorted(set(selected_options) - {"coordination"})
        if unknown_options:
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} has unsupported Claude options: {unknown_options}"
            )
        coordination = selected_options.get("coordination", "subagents")
        if coordination not in {"subagents", "agent_team"}:
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} Claude coordination must be subagents or agent_team"
            )
        if coordination == "agent_team" and (executor["mode"] != "native_subagents" or max_agents == 0):
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} agent_team requires native_subagents and max_agents > 0"
            )
    elif selected_options:
        raise StrategyValidationError(
            f"workflow {workflow['workflow_id']} backend {backend_id} has no implemented backend_options"
        )
    allowed_paths = executor.get("allowed_paths", [])
    if not isinstance(allowed_paths, list) or not all(is_non_empty_string(item) for item in allowed_paths):
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} allowed_paths must be a string list")
    policy_allowed = [_normalize_relative_path(item) for item in policy["allowed_paths"]]
    policy_forbidden = [_normalize_relative_path(item) for item in policy["forbidden_paths"]]
    for raw_path in allowed_paths:
        candidate = _normalize_relative_path(raw_path)
        if not any(_contains(parent, candidate) for parent in policy_allowed):
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} path {raw_path!r} is outside task allowed_paths"
            )
        if any(_contains(forbidden, candidate) or _contains(candidate, forbidden) for forbidden in policy_forbidden):
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} path {raw_path!r} overlaps task forbidden_paths"
            )
    budget = workflow.get("budget")
    if not isinstance(budget, dict):
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} budget must be an object")
    wall_seconds = _positive_int(budget, "wall_seconds")
    command_seconds = _positive_int(budget, "command_seconds")
    max_cases = _positive_int(budget, "max_enumerated_cases")
    max_instances = _positive_int(budget, "max_instances")
    if wall_seconds > policy["attempt_wall_seconds"]:
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} exceeds attempt wall budget")
    if max_cases > policy["max_enumerated_cases"]:
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} exceeds enumeration policy")
    if max_instances > policy["max_workflow_instances"]:
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} exceeds workflow instance policy")
    if command_seconds > wall_seconds:
        raise StrategyValidationError(f"workflow {workflow['workflow_id']} command timeout exceeds workflow timeout")
    if workflow.get("on_timeout") not in WORKFLOW_TIMEOUT_ACTIONS:
        raise StrategyValidationError(
            f"workflow {workflow['workflow_id']} on_timeout must be one of {sorted(WORKFLOW_TIMEOUT_ACTIONS)}"
        )
    completion = workflow.get("completion")
    if not isinstance(completion, dict) or not is_non_empty_string(completion.get("evidence")):
        raise StrategyValidationError(
            f"workflow {workflow['workflow_id']} completion.evidence must be a non-empty string"
        )
    review = workflow.get("review")
    is_review_kind = workflow["kind"].lower().replace("-", "_").endswith("review")
    if is_review_kind and review is None:
        raise StrategyValidationError(
            f"workflow {workflow['workflow_id']} review workflow requires an explicit review declaration"
        )
    if review is not None:
        if not is_review_kind:
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} review declaration requires a review workflow kind"
            )
        if not isinstance(review, dict) or set(review) != {"mode", "required_reviewers"}:
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} review requires exactly mode and required_reviewers"
            )
        if review.get("mode") not in {"self", "independent"}:
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} review.mode must be self or independent"
            )
        reviewers = review.get("required_reviewers")
        if not is_int_not_bool(reviewers) or reviewers <= 0:
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} review.required_reviewers must be positive"
            )
        if review["mode"] == "independent":
            if executor["mode"] != "native_subagents" or max_agents < reviewers:
                raise StrategyValidationError(
                    f"workflow {workflow['workflow_id']} independent review requires native_subagents "
                    "and max_agents >= required_reviewers"
                )
            if executor["write_access"]:
                raise StrategyValidationError(
                    f"workflow {workflow['workflow_id']} independent review must be read-only"
                )
        elif executor["mode"] != "primary_worker" or reviewers != 1:
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} self review requires primary_worker and one reviewer"
            )
    resume = workflow.get("resume")
    if resume is not None:
        if not isinstance(resume, dict):
            raise StrategyValidationError(f"workflow {workflow['workflow_id']} resume must be an object")
        if set(resume) != {"from_attempt", "from_workflow", "mode"}:
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} resume requires exactly from_attempt, from_workflow, and mode"
            )
        for field in ("from_attempt", "from_workflow"):
            value = resume.get(field)
            if not is_non_empty_string(value) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
                raise StrategyValidationError(
                    f"workflow {workflow['workflow_id']} resume.{field} must be a safe identifier"
                )
        if resume.get("mode") not in WORKFLOW_RESUME_MODES:
            raise StrategyValidationError(
                f"workflow {workflow['workflow_id']} resume.mode must be one of {sorted(WORKFLOW_RESUME_MODES)}"
            )
    return workflow


def validate_strategy(strategy: Any, policy: dict[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
    validate_execution_policy(policy)
    if not isinstance(strategy, dict):
        raise StrategyValidationError("strategy must be a JSON object")
    if strategy.get("schema_version") != 2:
        raise StrategyValidationError("strategy schema_version must be 2")
    for field in ("strategy_id", "task_id", "objective", "backend_id"):
        if not is_non_empty_string(strategy.get(field)):
            raise StrategyValidationError(f"strategy {field} must be a non-empty string")
    if strategy["backend_id"] not in WORKER_BACKENDS:
        raise StrategyValidationError(f"strategy backend_id must be one of {sorted(WORKER_BACKENDS)}")
    if task_id is not None and strategy["task_id"] != task_id:
        raise StrategyValidationError("strategy task_id does not match task")
    revision = _positive_int(strategy, "revision")
    if revision == 1 and strategy.get("supersedes") is not None:
        raise StrategyValidationError("first strategy revision must have supersedes=null")
    if revision > 1 and not is_non_empty_string(strategy.get("supersedes")):
        raise StrategyValidationError("later strategy revisions require supersedes")
    global_budget = strategy.get("global_budget")
    if not isinstance(global_budget, dict):
        raise StrategyValidationError("strategy global_budget must be an object")
    for field in (
        "wall_seconds",
        "max_workflows",
        "max_workflow_instances",
        "max_parallel_workflows",
        "max_subagents",
        "max_parallel_subagents",
    ):
        _positive_int(global_budget, field)
    mappings = {
        "wall_seconds": "attempt_wall_seconds",
        "max_workflows": "max_workflows",
        "max_workflow_instances": "max_workflow_instances",
        "max_parallel_workflows": "max_parallel_workflows",
        "max_subagents": "max_subagents",
        "max_parallel_subagents": "max_parallel_subagents",
    }
    for strategy_field, policy_field in mappings.items():
        if global_budget[strategy_field] > policy[policy_field]:
            raise StrategyValidationError(f"strategy {strategy_field} exceeds execution policy")
    resource_budget = strategy.get("resource_budget")
    if resource_budget is not None:
        if not isinstance(resource_budget, dict) or not resource_budget:
            raise StrategyValidationError("strategy resource_budget must be a non-empty object")
        unknown = sorted(set(resource_budget) - RESOURCE_BUDGET_FIELDS)
        if unknown:
            raise StrategyValidationError(f"strategy resource_budget has unknown fields: {unknown}")
        for field, value in resource_budget.items():
            if field == "max_cost_usd":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                    raise StrategyValidationError("resource_budget.max_cost_usd must be a positive number")
            elif not is_int_not_bool(value) or value <= 0:
                raise StrategyValidationError(f"resource_budget.{field} must be a positive integer")
    workflows = strategy.get("workflows")
    if not isinstance(workflows, list) or not workflows:
        raise StrategyValidationError("strategy workflows must be a non-empty list")
    if len(workflows) > global_budget["max_workflows"]:
        raise StrategyValidationError("strategy workflow count exceeds global budget")
    validated = [_validate_workflow(item, policy, strategy["backend_id"]) for item in workflows]
    if any(
        item["executor"]["max_agents"] > global_budget["max_subagents"]
        or item["executor"]["max_parallel"] > global_budget["max_parallel_subagents"]
        for item in validated
    ):
        raise StrategyValidationError("workflow executor exceeds strategy global subagent budget")
    if revision == 1 and any(item.get("resume") is not None for item in validated):
        raise StrategyValidationError("workflow resume requires a strategy revision greater than 1")
    if sum(item["budget"]["max_instances"] for item in validated) > global_budget["max_workflow_instances"]:
        raise StrategyValidationError("sum of workflow max_instances exceeds global budget")
    workflow_ids = [item["workflow_id"] for item in validated]
    if len(workflow_ids) != len(set(workflow_ids)):
        raise StrategyValidationError("workflow_id values must be unique")
    known = set(workflow_ids)
    for item in validated:
        unknown = sorted(set(item["depends_on"]) - known)
        if unknown:
            raise StrategyValidationError(f"workflow {item['workflow_id']} has unknown dependencies: {unknown}")
        if item["workflow_id"] in item["depends_on"]:
            raise StrategyValidationError(f"workflow {item['workflow_id']} cannot depend on itself")
    visiting: set[str] = set()
    visited: set[str] = set()
    dependencies = {item["workflow_id"]: item["depends_on"] for item in validated}

    def visit(workflow_id: str) -> None:
        if workflow_id in visiting:
            raise StrategyValidationError("workflow dependency graph must be acyclic")
        if workflow_id in visited:
            return
        visiting.add(workflow_id)
        for dependency in dependencies[workflow_id]:
            visit(dependency)
        visiting.remove(workflow_id)
        visited.add(workflow_id)

    for workflow_id in workflow_ids:
        visit(workflow_id)
    change_policy = strategy.get("runtime_change_policy")
    required_change_fields = {
        "allow_new_instances_of_approved_workflows",
        "require_revision_for_new_workflow_kind",
        "require_revision_for_budget_increase",
        "allow_unbounded_search",
    }
    if not isinstance(change_policy, dict) or not all(
        isinstance(change_policy.get(field), bool) for field in required_change_fields
    ):
        raise StrategyValidationError("runtime_change_policy must contain boolean protocol fields")
    if change_policy["allow_unbounded_search"] and not policy["allow_unbounded_search"]:
        raise StrategyValidationError("strategy cannot allow unbounded search when policy forbids it")
    completion = strategy.get("completion_gate")
    completion_fields = {"required_workflows_complete", "acceptance_commands_pass", "optional_workflows_may_timeout"}
    if not isinstance(completion, dict) or not all(isinstance(completion.get(field), bool) for field in completion_fields):
        raise StrategyValidationError("completion_gate must contain boolean protocol fields")
    return strategy


def canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def strategy_path(task_dir: Path, revision: int) -> Path:
    return task_dir / "strategy" / f"STRATEGY-v{revision:03d}.json"


def review_path(task_dir: Path, revision: int) -> Path:
    return task_dir / "strategy" / f"REVIEW-v{revision:03d}.json"


def write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
    except FileExistsError as exc:
        raise StrategyValidationError(f"immutable protocol artifact already exists: {path}") from exc


def submit_strategy(task_dir: Path, payload: dict[str, Any]) -> tuple[Path, str]:
    policy = load_execution_policy(task_dir)
    status = load_json(task_dir / "STATUS.json")
    task_id = status.get("task_id")
    strategy = validate_strategy(payload, policy, task_id=task_id)
    expected_backend = _current_attempt_backend(task_dir, status)
    if expected_backend and strategy["backend_id"] != expected_backend:
        raise StrategyValidationError(
            f"strategy backend {strategy['backend_id']!r} does not match planning backend {expected_backend!r}"
        )
    existing = sorted((task_dir / "strategy").glob("STRATEGY-v*.json"))
    expected_revision = len(existing) + 1
    if strategy["revision"] != expected_revision:
        raise StrategyValidationError(
            f"strategy revision must be the next immutable revision ({expected_revision})"
        )
    if existing:
        previous = load_json(existing[-1])
        if strategy.get("supersedes") != previous.get("strategy_id"):
            raise StrategyValidationError("strategy supersedes must name the previous strategy_id")
    path = strategy_path(task_dir, strategy["revision"])
    digest = canonical_digest(strategy)
    write_immutable_json(path, strategy)
    return path, digest


def review_strategy(
    task_dir: Path,
    revision: int,
    *,
    decision: str,
    reviewer: str,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    if decision not in {"approved", "changes_requested"}:
        raise StrategyValidationError("strategy review decision must be approved or changes_requested")
    if not is_non_empty_string(reviewer):
        raise StrategyValidationError("strategy reviewer must be a non-empty string")
    strategy = load_json(strategy_path(task_dir, revision))
    status = load_json(task_dir / "STATUS.json")
    validate_strategy(strategy, load_execution_policy(task_dir), task_id=status["task_id"])
    expected_backend = _current_attempt_backend(task_dir, status)
    if expected_backend and strategy["backend_id"] != expected_backend:
        raise StrategyValidationError(
            f"strategy backend {strategy['backend_id']!r} does not match planning backend {expected_backend!r}"
        )
    review = {
        "schema_version": 1,
        "strategy_id": strategy["strategy_id"],
        "strategy_sha256": canonical_digest(strategy),
        "decision": decision,
        "reviewer": reviewer,
        "reviewed_at": utc_now(),
        "notes": list(notes or []),
    }
    write_immutable_json(review_path(task_dir, revision), review)
    if decision == "approved":
        write_json(
            task_dir / "strategy" / "CURRENT.json",
            {
                "revision": revision,
                "strategy": strategy_path(task_dir, revision).name,
                "review": review_path(task_dir, revision).name,
                "strategy_id": strategy["strategy_id"],
                "strategy_sha256": review["strategy_sha256"],
            },
        )
    return review


def _current_attempt_backend(task_dir: Path, status: dict[str, Any]) -> str | None:
    attempt_id = status.get("current_attempt_id")
    if not is_non_empty_string(attempt_id):
        return None
    attempt_path = task_dir / "attempts" / attempt_id / "ATTEMPT.json"
    if not attempt_path.exists():
        return None
    attempt = load_json(attempt_path)
    backend_id = attempt.get("backend_id")
    return backend_id if is_non_empty_string(backend_id) else None


def load_approved_strategy(task_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    current_path = task_dir / "strategy" / "CURRENT.json"
    if not current_path.exists():
        raise StrategyValidationError("task has no approved execution strategy")
    current = load_json(current_path)
    strategy = load_json(task_dir / "strategy" / current["strategy"])
    review = load_json(task_dir / "strategy" / current["review"])
    digest = canonical_digest(strategy)
    if review.get("decision") != "approved" or review.get("strategy_sha256") != digest:
        raise StrategyValidationError("approved strategy review digest mismatch")
    if current.get("strategy_sha256") != digest:
        raise StrategyValidationError("CURRENT strategy digest mismatch")
    validate_strategy(strategy, load_execution_policy(task_dir), task_id=load_json(task_dir / "STATUS.json")["task_id"])
    return strategy, review


def load_bound_approved_strategy(
    task_dir: Path,
    *,
    strategy_id: Any,
    strategy_sha256: Any,
    revision: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the exact coordinator-approved strategy frozen for one attempt.

    Unlike ``load_approved_strategy``, this deliberately ignores a mutable
    ``CURRENT.json`` pointer.  The caller supplies the launch-time identity and
    digest, so a later pointer change or an in-place strategy rewrite cannot
    widen an active attempt's authority.
    """

    if not is_non_empty_string(strategy_id):
        raise StrategyValidationError("bound strategy_id must be a non-empty string")
    if not isinstance(strategy_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", strategy_sha256
    ):
        raise StrategyValidationError("bound strategy_sha256 must be a SHA-256 digest")
    if not is_int_not_bool(revision) or revision < 1:
        raise StrategyValidationError("bound strategy revision must be a positive integer")

    strategy = load_json(strategy_path(task_dir, revision))
    review = load_json(review_path(task_dir, revision))
    digest = canonical_digest(strategy)
    if (
        strategy.get("strategy_id") != strategy_id
        or strategy.get("revision") != revision
        or digest != strategy_sha256
    ):
        raise StrategyValidationError("strategy bytes do not match the attempt-frozen binding")
    if (
        review.get("decision") != "approved"
        or review.get("strategy_id") != strategy_id
        or review.get("strategy_sha256") != strategy_sha256
    ):
        raise StrategyValidationError("strategy review does not match the attempt-frozen approval")
    status = load_json(task_dir / "STATUS.json")
    validate_strategy(
        strategy,
        load_execution_policy(task_dir),
        task_id=status["task_id"],
    )
    return strategy, review
