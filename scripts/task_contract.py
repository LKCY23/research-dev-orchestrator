#!/usr/bin/env python3
"""Artifact Protocol v2 task-input contract primitives.

This module deliberately has no dependency on the dispatcher, FSM, or legacy
artifact readers.  It parses and validates the four canonical task inputs,
resolves declared dependencies through an injected resolver, and derives the
attempt-local ``TASK_INPUTS.json`` payload.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from task_budget import TaskBudgetError, normalize_task_budget


ARTIFACT_PROTOCOL_VERSION = 2
TASK_INPUTS_SCHEMA_VERSION = 2

TASK_REQUIRED_SECTIONS = (
    "Objective",
    "Deliverables",
    "Invariants",
    "Non-goals",
    "Dependencies",
)
CONTEXT_REQUIRED_SECTIONS = (
    "Frozen Decisions",
    "Required Interfaces",
    "Local Code Map",
    "Necessary Background",
)
ACCEPTANCE_REQUIRED_SECTIONS = (
    "Behavioral Checks",
    "Merge Preconditions",
    "Blocked Conditions",
    "Pre-Merge Checks",
    "Post-Merge Checks",
)

TASK_INPUT_FILENAMES = (
    "TASK.md",
    "CONTEXT.md",
    "ACCEPTANCE.md",
    "EXECUTION_POLICY.json",
)

_DEPENDENCIES_FENCE = "rdo-task-dependencies"
_ACCEPTANCE_FENCE = "rdo-acceptance-contract"
_TASK_ID_RE = re.compile(r"^T[0-9]{3}[A-Za-z0-9-]*$")
_COMMAND_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:/")
_TASK_CONTROL_FIELD_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?"
    r"(?:profile|allowed_paths|read_paths|forbidden_paths|branch|worktree)\s*:"
)
_SOURCE_INDEX_RE = re.compile(r"(?im)^#{1,6}\s+Source Index\s*$")
_PLACEHOLDER_PATTERNS = (
    re.compile(r"\bRDO_TEMPLATE_INCOMPLETE\b"),
    re.compile(r"\{\{[^}\n]+\}\}"),
    re.compile(
        r"(?im)^\s*(?:[-*]\s*)?(?:TODO|TBD|FIXME|PLACEHOLDER)"
        r"(?:\s*[:.\-].*)?$"
    ),
    re.compile(r"(?i)\breplace this (?:example|text|placeholder)\b"),
    re.compile(r"(?i)\bfill (?:this|me) in\b"),
    re.compile(r"(?i)\bpath/to/(?:file|design\.md)\b"),
)


class TaskContractError(ValueError):
    """Raised when an Artifact Protocol v2 task input is invalid."""


class ImmutableArtifactError(FileExistsError):
    """Raised when a caller attempts to replace an immutable artifact."""


DependencyResolver = Callable[[str], Mapping[str, Any] | None]


@dataclass(frozen=True)
class ReadinessResult:
    """Result of validating all canonical task-input artifacts."""

    ready: bool
    errors: tuple[str, ...]
    task: Mapping[str, Any] | None = None
    context: Mapping[str, Any] | None = None
    acceptance: Mapping[str, Any] | None = None
    policy: Mapping[str, Any] | None = None
    resolved_dependencies: tuple[Mapping[str, str], ...] = ()
    source_bytes: Mapping[str, bytes] = field(default_factory=dict, repr=False)

    def require_ready(self) -> "ReadinessResult":
        if not self.ready:
            raise TaskContractError("task is not dispatch-ready:\n- " + "\n- ".join(self.errors))
        return self


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def payload_sha256(payload: Any) -> str:
    """Return the deterministic digest of a JSON-compatible value."""

    return sha256_bytes(_canonical_json_bytes(payload))


def _reject_placeholders(text: str, artifact: str) -> None:
    for pattern in _PLACEHOLDER_PATTERNS:
        match = pattern.search(text)
        if match:
            marker = " ".join(match.group(0).split())
            raise TaskContractError(f"{artifact} contains template placeholder {marker!r}")


def _split_h2_sections(
    text: str,
    *,
    artifact: str,
    required: Sequence[str],
) -> dict[str, str]:
    """Split exact H2 sections while ignoring headings inside code fences."""

    sections: dict[str, list[str]] = {}
    current: str | None = None
    fence_char: str | None = None
    fence_length = 0

    for raw_line in text.splitlines(keepends=True):
        fence_match = _FENCE_RE.match(raw_line.rstrip("\r\n"))
        if fence_match:
            token = fence_match.group(1)
            if fence_char is None:
                fence_char = token[0]
                fence_length = len(token)
            elif token[0] == fence_char and len(token) >= fence_length:
                fence_char = None
                fence_length = 0
            if current is not None:
                sections[current].append(raw_line)
            continue

        if fence_char is None:
            heading = _H2_RE.match(raw_line.rstrip("\r\n"))
            if heading:
                title = heading.group(1).strip()
                if title in sections:
                    raise TaskContractError(f"{artifact} has duplicate section {title!r}")
                sections[title] = []
                current = title
                continue

        if current is not None:
            sections[current].append(raw_line)

    if fence_char is not None:
        raise TaskContractError(f"{artifact} contains an unterminated code fence")

    required_set = set(required)
    missing = [name for name in required if name not in sections]
    extra = [name for name in sections if name not in required_set]
    if missing:
        raise TaskContractError(f"{artifact} is missing required sections: {', '.join(missing)}")
    if extra:
        raise TaskContractError(f"{artifact} has unsupported sections: {', '.join(extra)}")

    result = {name: "".join(sections[name]).strip() for name in required}
    empty = [name for name, body in result.items() if not body]
    if empty:
        raise TaskContractError(f"{artifact} has empty required sections: {', '.join(empty)}")
    return result


def _extract_named_json_fence(text: str, *, artifact: str, name: str) -> tuple[Any, tuple[int, int]]:
    pattern = re.compile(
        rf"(?ms)^[ \t]*```json[ \t]+{re.escape(name)}[ \t]*\r?\n"
        rf"(.*?)^[ \t]*```[ \t]*$"
    )
    matches = list(pattern.finditer(text))
    if not matches:
        raise TaskContractError(f"{artifact} must contain one ```json {name} block")
    if len(matches) != 1:
        raise TaskContractError(f"{artifact} must not contain multiple ```json {name} blocks")
    try:
        payload = json.loads(matches[0].group(1))
    except json.JSONDecodeError as exc:
        raise TaskContractError(
            f"{artifact} {name} block is invalid JSON at line {exc.lineno}: {exc.msg}"
        ) from exc
    return payload, matches[0].span()


def _only_named_fence(section: str, *, artifact: str, name: str) -> Any:
    payload, span = _extract_named_json_fence(section, artifact=artifact, name=name)
    remainder = section[: span[0]] + section[span[1] :]
    if remainder.strip():
        raise TaskContractError(
            f"{artifact} machine section must contain only the ```json {name} block"
        )
    return payload


def _expect_exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise TaskContractError(f"{label} fields are invalid ({'; '.join(details)})")


def parse_task_markdown(text: str, *, task_id: str) -> dict[str, Any]:
    """Parse and validate canonical v2 ``TASK.md`` content."""

    if not _TASK_ID_RE.fullmatch(task_id):
        raise TaskContractError(f"invalid task_id {task_id!r}")
    _reject_placeholders(text, "TASK.md")
    if _TASK_CONTROL_FIELD_RE.search(text):
        raise TaskContractError(
            "TASK.md must not define profile, path policy, branch, or worktree controls"
        )
    sections = _split_h2_sections(
        text,
        artifact="TASK.md",
        required=TASK_REQUIRED_SECTIONS,
    )
    if sections["Objective"].casefold().rstrip(".") == "none":
        raise TaskContractError("TASK.md Objective must be substantive")
    if sections["Deliverables"].casefold().rstrip(".") == "none":
        raise TaskContractError("TASK.md Deliverables must be substantive")

    raw_dependencies = _only_named_fence(
        sections["Dependencies"],
        artifact="TASK.md Dependencies",
        name=_DEPENDENCIES_FENCE,
    )
    if not isinstance(raw_dependencies, dict):
        raise TaskContractError("TASK.md dependencies block must be a JSON object")
    _expect_exact_keys(
        raw_dependencies,
        {"schema_version", "dependencies"},
        label="TASK.md dependencies block",
    )
    if raw_dependencies["schema_version"] != 2:
        raise TaskContractError("TASK.md dependencies schema_version must be 2")
    entries = raw_dependencies["dependencies"]
    if not isinstance(entries, list):
        raise TaskContractError("TASK.md dependencies must be a JSON array")

    dependencies: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"TASK.md dependency #{index + 1}"
        if not isinstance(entry, dict):
            raise TaskContractError(f"{label} must be a JSON object")
        _expect_exact_keys(entry, {"task_id", "required_state"}, label=label)
        dependency_id = entry["task_id"]
        required_state = entry["required_state"]
        if not isinstance(dependency_id, str) or not _TASK_ID_RE.fullmatch(dependency_id):
            raise TaskContractError(f"{label} has invalid task_id")
        if dependency_id == task_id:
            raise TaskContractError(f"{label} must not reference the current task")
        if dependency_id in seen:
            raise TaskContractError(f"TASK.md repeats dependency {dependency_id!r}")
        if required_state != "merged":
            raise TaskContractError(f"{label} required_state must be 'merged'")
        seen.add(dependency_id)
        dependencies.append({"task_id": dependency_id, "required_state": required_state})

    return {"sections": sections, "dependencies": dependencies}


def parse_context_markdown(text: str) -> dict[str, Any]:
    """Parse and validate the non-normative v2 ``CONTEXT.md`` capsule."""

    _reject_placeholders(text, "CONTEXT.md")
    if _SOURCE_INDEX_RE.search(text):
        raise TaskContractError(
            "CONTEXT.md must not contain a Source Index; use EXECUTION_POLICY.context_sources"
        )
    sections = _split_h2_sections(
        text,
        artifact="CONTEXT.md",
        required=CONTEXT_REQUIRED_SECTIONS,
    )
    return {"sections": sections}


def _normalize_relative_path(value: Any, *, label: str, allow_dot: bool) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise TaskContractError(f"{label} must be a non-empty, trimmed string")
    if "\x00" in value:
        raise TaskContractError(f"{label} must not contain NUL")
    candidate = value.replace("\\", "/")
    if candidate.startswith(("/", "~/")) or _WINDOWS_ABSOLUTE_RE.match(candidate):
        raise TaskContractError(f"{label} must be relative: {value!r}")
    parts = candidate.split("/")
    if ".." in parts:
        raise TaskContractError(f"{label} must not traverse parents: {value!r}")
    normalized = posixpath.normpath(candidate)
    if normalized in ("", "."):
        if allow_dot:
            return "."
        raise TaskContractError(f"{label} must name a path below the worktree")
    if normalized.startswith("../"):
        raise TaskContractError(f"{label} must not traverse parents: {value!r}")
    return normalized


def _path_contains(parent: str, child: str) -> bool:
    return parent == "." or child == parent or child.startswith(parent + "/")


def _normalize_path_list(
    value: Any,
    *,
    label: str,
    allow_empty: bool,
    allow_dot: bool,
) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "a" if allow_empty else "a non-empty"
        raise TaskContractError(f"{label} must be {qualifier} JSON string array")
    result: list[str] = []
    for index, raw_path in enumerate(value):
        normalized = _normalize_relative_path(
            raw_path,
            label=f"{label}[{index}]",
            allow_dot=allow_dot,
        )
        if normalized in result:
            raise TaskContractError(f"{label} contains duplicate path {normalized!r}")
        result.append(normalized)
    return result


def _validate_command(value: Any, *, category: str, index: int) -> dict[str, Any]:
    label = f"ACCEPTANCE.md {category}[{index}]"
    if not isinstance(value, dict):
        raise TaskContractError(f"{label} must be a JSON object")
    _expect_exact_keys(value, {"id", "argv", "cwd", "timeout_seconds"}, label=label)

    command_id = value["id"]
    if not isinstance(command_id, str) or not _COMMAND_ID_RE.fullmatch(command_id):
        raise TaskContractError(
            f"{label}.id must match {_COMMAND_ID_RE.pattern!r}"
        )
    argv = value["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item and "\x00" not in item for item in argv)
    ):
        raise TaskContractError(f"{label}.argv must be a non-empty string array")
    cwd = _normalize_relative_path(value["cwd"], label=f"{label}.cwd", allow_dot=True)
    timeout = value["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise TaskContractError(f"{label}.timeout_seconds must be a positive integer")
    return {
        "id": command_id,
        "argv": list(argv),
        "cwd": cwd,
        "timeout_seconds": timeout,
    }


def parse_acceptance_markdown(text: str) -> dict[str, Any]:
    """Parse and validate canonical v2 ``ACCEPTANCE.md`` content."""

    _reject_placeholders(text, "ACCEPTANCE.md")
    sections = _split_h2_sections(
        text,
        artifact="ACCEPTANCE.md",
        required=ACCEPTANCE_REQUIRED_SECTIONS,
    )
    contract, _ = _extract_named_json_fence(
        text,
        artifact="ACCEPTANCE.md",
        name=_ACCEPTANCE_FENCE,
    )
    if not isinstance(contract, dict):
        raise TaskContractError("ACCEPTANCE.md machine block must be a JSON object")
    _expect_exact_keys(
        contract,
        {
            "schema_version",
            "required_commands",
            "required_outputs",
            "pre_merge_commands",
            "post_merge_commands",
        },
        label="ACCEPTANCE.md machine block",
    )
    if contract["schema_version"] != 2:
        raise TaskContractError("ACCEPTANCE.md machine schema_version must be 2")

    commands: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    for category in ("required_commands", "pre_merge_commands", "post_merge_commands"):
        raw_commands = contract[category]
        if not isinstance(raw_commands, list):
            raise TaskContractError(f"ACCEPTANCE.md {category} must be a JSON array")
        parsed_commands = [
            _validate_command(command, category=category, index=index)
            for index, command in enumerate(raw_commands)
        ]
        for command in parsed_commands:
            if command["id"] in seen_ids:
                raise TaskContractError(
                    f"ACCEPTANCE.md repeats command id {command['id']!r}"
                )
            seen_ids.add(command["id"])
        commands[category] = parsed_commands
    if not commands["required_commands"]:
        raise TaskContractError("ACCEPTANCE.md must define at least one required command")

    required_outputs = _normalize_path_list(
        contract["required_outputs"],
        label="ACCEPTANCE.md required_outputs",
        allow_empty=False,
        allow_dot=False,
    )
    parsed_contract: dict[str, Any] = {
        "schema_version": 2,
        **commands,
        "required_outputs": required_outputs,
    }
    return {"sections": sections, "contract": parsed_contract}


def _positive_integer(policy: Mapping[str, Any], field: str) -> int:
    value = policy.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TaskContractError(f"EXECUTION_POLICY.{field} must be a positive integer")
    return value


def validate_execution_policy_v2(policy: Any, *, profile: str) -> dict[str, Any]:
    """Validate the v2 policy fields relevant to task readiness."""

    if not isinstance(policy, dict):
        raise TaskContractError("EXECUTION_POLICY.json must contain a JSON object")
    if policy.get("schema_version") != 2:
        raise TaskContractError("EXECUTION_POLICY.schema_version must be 2")
    if profile not in {"direct", "delegated", "full"}:
        raise TaskContractError(f"unknown execution profile {profile!r}")
    strategy_required = policy.get("strategy_required")
    if not isinstance(strategy_required, bool):
        raise TaskContractError("EXECUTION_POLICY.strategy_required must be boolean")
    expected_strategy_required = profile == "full"
    if strategy_required is not expected_strategy_required:
        raise TaskContractError(
            "EXECUTION_POLICY.strategy_required must be "
            f"{str(expected_strategy_required).lower()} for profile {profile!r}"
        )

    integer_fields = (
        "attempt_wall_seconds",
        "max_workflows",
        "max_workflow_instances",
        "max_parallel_workflows",
        "max_subagents",
        "max_parallel_subagents",
        "default_command_seconds",
        "max_enumerated_cases",
    )
    normalized: dict[str, Any] = dict(policy)
    for name in integer_fields:
        normalized[name] = _positive_integer(policy, name)
    if normalized["max_parallel_workflows"] > normalized["max_workflows"]:
        raise TaskContractError(
            "EXECUTION_POLICY.max_parallel_workflows cannot exceed max_workflows"
        )
    if normalized["max_parallel_subagents"] > normalized["max_subagents"]:
        raise TaskContractError(
            "EXECUTION_POLICY.max_parallel_subagents cannot exceed max_subagents"
        )
    if not isinstance(policy.get("allow_unbounded_search"), bool):
        raise TaskContractError("EXECUTION_POLICY.allow_unbounded_search must be boolean")
    try:
        normalized["task_budget"] = normalize_task_budget(policy.get("task_budget"))
    except TaskBudgetError as exc:
        raise TaskContractError(f"EXECUTION_POLICY.{exc}") from exc

    allowed_paths = _normalize_path_list(
        policy.get("allowed_paths"),
        label="EXECUTION_POLICY.allowed_paths",
        allow_empty=False,
        allow_dot=True,
    )
    read_paths = _normalize_path_list(
        policy.get("read_paths"),
        label="EXECUTION_POLICY.read_paths",
        allow_empty=False,
        allow_dot=True,
    )
    forbidden_paths = _normalize_path_list(
        policy.get("forbidden_paths"),
        label="EXECUTION_POLICY.forbidden_paths",
        allow_empty=True,
        allow_dot=True,
    )
    context_sources = _normalize_path_list(
        policy.get("context_sources"),
        label="EXECUTION_POLICY.context_sources",
        allow_empty=True,
        allow_dot=False,
    )

    for path in allowed_paths:
        if any(
            _path_contains(forbidden, path) or _path_contains(path, forbidden)
            for forbidden in forbidden_paths
        ):
            raise TaskContractError(
                f"EXECUTION_POLICY allowed path {path!r} overlaps forbidden_paths"
            )
        if not any(_path_contains(read_root, path) for read_root in read_paths):
            raise TaskContractError(
                f"EXECUTION_POLICY allowed path {path!r} is outside read_paths"
            )
    for path in read_paths:
        if any(_path_contains(forbidden, path) for forbidden in forbidden_paths):
            raise TaskContractError(
                f"EXECUTION_POLICY read path {path!r} is inside forbidden_paths"
            )
    for source in context_sources:
        if any(_path_contains(forbidden, source) for forbidden in forbidden_paths):
            raise TaskContractError(
                f"EXECUTION_POLICY context source {source!r} is forbidden"
            )
        if not any(_path_contains(read_root, source) for read_root in read_paths):
            raise TaskContractError(
                f"EXECUTION_POLICY context source {source!r} is outside read_paths"
            )

    normalized.update(
        {
            "schema_version": 2,
            "strategy_required": strategy_required,
            "allowed_paths": allowed_paths,
            "read_paths": read_paths,
            "forbidden_paths": forbidden_paths,
            "context_sources": context_sources,
        }
    )
    return normalized


def parse_execution_policy(text: str | bytes, *, profile: str) -> dict[str, Any]:
    """Decode and validate ``EXECUTION_POLICY.json``."""

    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        _reject_placeholders(text, "EXECUTION_POLICY.json")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskContractError(f"EXECUTION_POLICY.json is invalid JSON/UTF-8: {exc}") from exc
    return validate_execution_policy_v2(payload, profile=profile)


def resolve_dependencies(
    dependencies: Sequence[Mapping[str, str]],
    *,
    task_id: str,
    resolver: DependencyResolver | Mapping[str, Mapping[str, Any]] | None,
) -> list[dict[str, str]]:
    """Resolve declared dependencies into exact merged commits.

    The injected resolver owns repository/FSM lookup.  It returns a mapping with
    ``state`` and the exact full ``commit`` for a dependency, or ``None`` when
    the task does not exist.
    """

    if not dependencies:
        return []
    if resolver is None:
        raise TaskContractError("dependency resolution is required before dispatch")

    resolved: list[dict[str, str]] = []
    seen: set[str] = set()
    for dependency in dependencies:
        dependency_id = dependency.get("task_id")
        required_state = dependency.get("required_state")
        if dependency_id == task_id:
            raise TaskContractError("task must not depend on itself")
        if not isinstance(dependency_id, str) or dependency_id in seen:
            raise TaskContractError("dependencies must have unique valid task_id values")
        if required_state != "merged":
            raise TaskContractError(
                f"dependency {dependency_id!r} required_state must be 'merged'"
            )
        seen.add(dependency_id)
        try:
            resolution = (
                resolver(dependency_id)
                if callable(resolver)
                else resolver.get(dependency_id)
            )
        except Exception as exc:  # isolate external lookup from contract errors
            raise TaskContractError(
                f"failed to resolve dependency {dependency_id!r}: {exc}"
            ) from exc
        if resolution is None:
            raise TaskContractError(f"dependency {dependency_id!r} does not exist")
        if not isinstance(resolution, Mapping):
            raise TaskContractError(
                f"dependency resolver returned invalid data for {dependency_id!r}"
            )
        state = resolution.get("state")
        if state != required_state:
            raise TaskContractError(
                f"dependency {dependency_id!r} requires state {required_state!r}, got {state!r}"
            )
        commit = resolution.get("commit")
        if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
            raise TaskContractError(
                f"dependency {dependency_id!r} is missing an exact full commit"
            )
        resolved.append(
            {
                "task_id": dependency_id,
                "required_state": required_state,
                "commit": commit,
            }
        )
    return resolved


def _read_required_file(task_dir: Path, filename: str) -> bytes:
    root = task_dir.resolve()
    path = task_dir / filename
    resolved = path.resolve(strict=False)
    if path.is_symlink() or resolved.parent != root:
        raise TaskContractError(f"{filename} must be a task-root regular non-symlink file")
    if path.exists() and not path.is_file():
        raise TaskContractError(f"{filename} must be a regular file")
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise TaskContractError(f"{filename} is missing") from exc
    except OSError as exc:
        raise TaskContractError(f"{filename} is unreadable: {exc}") from exc
    if not data:
        raise TaskContractError(f"{filename} is empty")
    return data


def evaluate_task_readiness(
    task_dir: Path,
    *,
    task_id: str,
    profile: str,
    dependency_resolver: DependencyResolver | Mapping[str, Mapping[str, Any]] | None = None,
    context_root: Path | None = None,
) -> ReadinessResult:
    """Validate all v2 task inputs and dependency readiness without side effects."""

    task_dir = Path(task_dir)
    source_bytes: dict[str, bytes] = {}
    errors: list[str] = []
    for filename in TASK_INPUT_FILENAMES:
        try:
            source_bytes[filename] = _read_required_file(task_dir, filename)
        except TaskContractError as exc:
            errors.append(str(exc))

    parsed_task: dict[str, Any] | None = None
    parsed_context: dict[str, Any] | None = None
    parsed_acceptance: dict[str, Any] | None = None
    parsed_policy: dict[str, Any] | None = None
    resolved: list[dict[str, str]] = []

    if "TASK.md" in source_bytes:
        try:
            parsed_task = parse_task_markdown(
                source_bytes["TASK.md"].decode("utf-8"),
                task_id=task_id,
            )
        except (UnicodeDecodeError, TaskContractError) as exc:
            errors.append(f"TASK.md: {exc}")
    if "CONTEXT.md" in source_bytes:
        try:
            parsed_context = parse_context_markdown(
                source_bytes["CONTEXT.md"].decode("utf-8")
            )
        except (UnicodeDecodeError, TaskContractError) as exc:
            errors.append(f"CONTEXT.md: {exc}")
    if "ACCEPTANCE.md" in source_bytes:
        try:
            parsed_acceptance = parse_acceptance_markdown(
                source_bytes["ACCEPTANCE.md"].decode("utf-8")
            )
        except (UnicodeDecodeError, TaskContractError) as exc:
            errors.append(f"ACCEPTANCE.md: {exc}")
    if "EXECUTION_POLICY.json" in source_bytes:
        try:
            parsed_policy = parse_execution_policy(
                source_bytes["EXECUTION_POLICY.json"],
                profile=profile,
            )
        except TaskContractError as exc:
            errors.append(str(exc))

    if parsed_task is not None:
        try:
            resolved = resolve_dependencies(
                parsed_task["dependencies"],
                task_id=task_id,
                resolver=dependency_resolver,
            )
        except TaskContractError as exc:
            errors.append(str(exc))

    if parsed_acceptance is not None and parsed_policy is not None:
        contract = parsed_acceptance["contract"]
        allowed_paths = parsed_policy["allowed_paths"]
        read_paths = parsed_policy["read_paths"]
        forbidden_paths = parsed_policy["forbidden_paths"]
        for output in contract["required_outputs"]:
            if not any(_path_contains(root, output) for root in allowed_paths):
                errors.append(
                    f"ACCEPTANCE.md required output {output!r} is outside EXECUTION_POLICY.allowed_paths"
                )
            if any(_path_contains(root, output) for root in forbidden_paths):
                errors.append(
                    f"ACCEPTANCE.md required output {output!r} is forbidden by EXECUTION_POLICY"
                )
        for command in contract["required_commands"]:
            cwd = command["cwd"]
            if not any(
                _path_contains(root, cwd) or _path_contains(cwd, root)
                for root in read_paths
            ):
                errors.append(
                    f"ACCEPTANCE.md required command {command['id']!r} cwd {cwd!r} "
                    "is outside EXECUTION_POLICY.read_paths"
                )
            if any(_path_contains(root, cwd) for root in forbidden_paths):
                errors.append(
                    f"ACCEPTANCE.md required command {command['id']!r} cwd {cwd!r} "
                    "is forbidden by EXECUTION_POLICY"
                )

        if context_root is not None:
            context_root = Path(context_root).resolve()
            for source in parsed_policy["context_sources"]:
                raw_source = context_root / source
                resolved_source = raw_source.resolve(strict=False)
                try:
                    resolved_source.relative_to(context_root)
                except ValueError:
                    errors.append(f"EXECUTION_POLICY context source {source!r} escapes the repository")
                    continue
                if raw_source.is_symlink() or not resolved_source.is_file():
                    errors.append(
                        f"EXECUTION_POLICY context source {source!r} must exist as a regular non-symlink file"
                    )

    return ReadinessResult(
        ready=not errors,
        errors=tuple(errors),
        task=parsed_task,
        context=parsed_context,
        acceptance=parsed_acceptance,
        policy=parsed_policy,
        resolved_dependencies=tuple(resolved),
        source_bytes=source_bytes,
    )


def _validate_full_commit(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        raise TaskContractError(f"{label} must be an exact full Git commit")
    return value


def build_task_inputs_payload(
    *,
    task_id: str,
    attempt_id: str,
    source_bytes: Mapping[str, bytes],
    task_base_commit: str,
    resolved_dependencies: Sequence[Mapping[str, str]],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Derive the immutable, attempt-local ``TASK_INPUTS.json`` payload."""

    if not _TASK_ID_RE.fullmatch(task_id):
        raise TaskContractError(f"invalid task_id {task_id!r}")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise TaskContractError("attempt_id must be a non-empty string")
    task_base_commit = _validate_full_commit(task_base_commit, label="task_base_commit")
    missing = sorted(set(TASK_INPUT_FILENAMES) - set(source_bytes))
    extra = sorted(set(source_bytes) - set(TASK_INPUT_FILENAMES))
    if missing or extra:
        raise TaskContractError(
            "TASK_INPUTS source set must contain exactly the four canonical inputs"
            f" (missing={missing}, extra={extra})"
        )

    input_names = {
        "task": "TASK.md",
        "context": "CONTEXT.md",
        "acceptance": "ACCEPTANCE.md",
        "execution_policy": "EXECUTION_POLICY.json",
    }
    inputs: dict[str, dict[str, str]] = {}
    for name, filename in input_names.items():
        data = source_bytes[filename]
        if not isinstance(data, bytes):
            raise TaskContractError(f"source_bytes[{filename!r}] must be bytes")
        inputs[name] = {
            "ref": filename,
            "sha256": sha256_bytes(data),
        }

    dependency_commits: list[dict[str, str]] = []
    seen: set[str] = set()
    for dependency in resolved_dependencies:
        dependency_id = dependency.get("task_id")
        required_state = dependency.get("required_state")
        commit = dependency.get("commit")
        if not isinstance(dependency_id, str) or not _TASK_ID_RE.fullmatch(dependency_id):
            raise TaskContractError("resolved dependency has invalid task_id")
        if dependency_id == task_id or dependency_id in seen:
            raise TaskContractError("resolved dependencies contain a duplicate or self-reference")
        if required_state != "merged":
            raise TaskContractError("resolved dependency required_state must be 'merged'")
        dependency_commits.append(
            {
                "task_id": dependency_id,
                "required_state": required_state,
                "commit": _validate_full_commit(
                    str(commit),
                    label=f"dependency {dependency_id!r} commit",
                ),
            }
        )
        seen.add(dependency_id)
    dependency_commits.sort(key=lambda item: item["task_id"])

    contract_basis = {
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "task_id": task_id,
        "inputs": inputs,
        "task_base_commit": task_base_commit,
        "resolved_dependencies": dependency_commits,
    }
    payload: dict[str, Any] = {
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "schema_version": TASK_INPUTS_SCHEMA_VERSION,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "inputs": inputs,
        "task_base_commit": task_base_commit,
        "resolved_dependencies": dependency_commits,
        "contract_sha256": payload_sha256(contract_basis),
    }
    if generated_at is not None:
        if not isinstance(generated_at, str) or not generated_at.strip():
            raise TaskContractError("generated_at must be a non-empty string when provided")
        payload["generated_at"] = generated_at
    return payload


def build_task_inputs_from_readiness(
    readiness: ReadinessResult,
    *,
    task_id: str,
    attempt_id: str,
    task_base_commit: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Derive ``TASK_INPUTS.json`` only from a successful readiness result."""

    readiness.require_ready()
    return build_task_inputs_payload(
        task_id=task_id,
        attempt_id=attempt_id,
        source_bytes=readiness.source_bytes,
        task_base_commit=task_base_commit,
        resolved_dependencies=readiness.resolved_dependencies,
        generated_at=generated_at,
    )


def validate_task_inputs_payload(payload: Any) -> dict[str, Any]:
    """Validate a derived TASK_INPUTS payload and its stable digest."""

    if not isinstance(payload, dict):
        raise TaskContractError("TASK_INPUTS.json must contain a JSON object")
    if payload.get("artifact_protocol_version") != ARTIFACT_PROTOCOL_VERSION:
        raise TaskContractError("TASK_INPUTS artifact_protocol_version must be 2")
    if payload.get("schema_version") != TASK_INPUTS_SCHEMA_VERSION:
        raise TaskContractError("TASK_INPUTS schema_version must be 2")
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise TaskContractError("TASK_INPUTS task_id is invalid")
    if not isinstance(payload.get("attempt_id"), str) or not payload["attempt_id"].strip():
        raise TaskContractError("TASK_INPUTS attempt_id is invalid")
    input_names = {
        "task": "TASK.md",
        "context": "CONTEXT.md",
        "acceptance": "ACCEPTANCE.md",
        "execution_policy": "EXECUTION_POLICY.json",
    }
    inputs = payload.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != set(input_names):
        raise TaskContractError("TASK_INPUTS inputs do not bind the four canonical artifacts")
    for name, filename in input_names.items():
        binding = inputs[name]
        if not isinstance(binding, dict) or set(binding) != {"ref", "sha256"}:
            raise TaskContractError(f"TASK_INPUTS binding for {filename} is invalid")
        if binding["ref"] != filename:
            raise TaskContractError(f"TASK_INPUTS ref for {filename} is invalid")
        digest = binding["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise TaskContractError(f"TASK_INPUTS digest for {filename} is invalid")
    task_base_commit = _validate_full_commit(
        payload.get("task_base_commit"),
        label="TASK_INPUTS task_base_commit",
    )
    dependencies = payload.get("resolved_dependencies")
    if not isinstance(dependencies, list):
        raise TaskContractError("TASK_INPUTS resolved_dependencies must be an array")
    previous_id = ""
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) != {
            "task_id",
            "required_state",
            "commit",
        }:
            raise TaskContractError("TASK_INPUTS dependency binding is invalid")
        dependency_id = dependency["task_id"]
        if (
            not isinstance(dependency_id, str)
            or not _TASK_ID_RE.fullmatch(dependency_id)
            or dependency_id == task_id
            or dependency_id <= previous_id
        ):
            raise TaskContractError(
                "TASK_INPUTS dependencies must be unique and sorted by task_id"
            )
        if dependency["required_state"] != "merged":
            raise TaskContractError("TASK_INPUTS dependency state must be 'merged'")
        _validate_full_commit(
            dependency["commit"],
            label=f"TASK_INPUTS dependency {dependency_id!r} commit",
        )
        previous_id = dependency_id
    contract_basis = {
        "artifact_protocol_version": ARTIFACT_PROTOCOL_VERSION,
        "task_id": task_id,
        "inputs": inputs,
        "task_base_commit": task_base_commit,
        "resolved_dependencies": dependencies,
    }
    expected_digest = payload_sha256(contract_basis)
    if payload.get("contract_sha256") != expected_digest:
        raise TaskContractError("TASK_INPUTS contract_sha256 does not match its bindings")
    return payload


def compare_task_inputs(previous: Any, current: Any) -> dict[str, Any]:
    """Return a deterministic, human-readable contract drift comparison."""

    previous = validate_task_inputs_payload(previous)
    current = validate_task_inputs_payload(current)
    input_names = ("task", "context", "acceptance", "execution_policy")
    changed_inputs = [
        name
        for name in input_names
        if previous["inputs"][name]["sha256"] != current["inputs"][name]["sha256"]
    ]
    comparison = {
        "matches": previous["contract_sha256"] == current["contract_sha256"],
        "previous_contract_sha256": previous["contract_sha256"],
        "current_contract_sha256": current["contract_sha256"],
        "changed_inputs": changed_inputs,
        "task_id_changed": previous["task_id"] != current["task_id"],
        "task_base_commit_changed": (
            previous["task_base_commit"] != current["task_base_commit"]
        ),
        "resolved_dependencies_changed": (
            previous["resolved_dependencies"] != current["resolved_dependencies"]
        ),
    }
    return comparison


def assert_resume_inputs_unchanged(previous: Any, current: Any) -> None:
    """Reject an ordinary resume when the frozen task input contract drifted."""

    comparison = compare_task_inputs(previous, current)
    if comparison["matches"]:
        return
    reasons: list[str] = []
    if comparison["changed_inputs"]:
        reasons.append("changed inputs: " + ", ".join(comparison["changed_inputs"]))
    if comparison["task_id_changed"]:
        reasons.append("task_id changed")
    if comparison["task_base_commit_changed"]:
        reasons.append("task base commit changed")
    if comparison["resolved_dependencies_changed"]:
        reasons.append("dependency commits changed")
    raise TaskContractError(
        "task input contract drift blocks ordinary resume; create a revision task"
        + (" (" + "; ".join(reasons) + ")" if reasons else "")
    )


def write_json_immutable(path: Path, payload: Any) -> str:
    """Atomically publish a new JSON artifact and never replace an existing path.

    The temporary file is fully flushed before an atomic hard-link publication.
    The returned digest covers the exact pretty-printed bytes on disk.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ImmutableArtifactError(f"immutable artifact already exists: {path}") from exc
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return sha256_bytes(data)


def write_task_inputs_immutable(path: Path, payload: Any) -> str:
    """Validate and atomically publish ``TASK_INPUTS.json``."""

    validate_task_inputs_payload(payload)
    return write_json_immutable(path, payload)
