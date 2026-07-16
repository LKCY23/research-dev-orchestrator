#!/usr/bin/env python3
"""Compile and evaluate attempt-local, backend-neutral repository read policy."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from protocol import utc_now


LARGE_MARKDOWN_BYTES = 16 * 1024
SECTION_MAX_BYTES = 16 * 1024
CONTEXT_ACCESS_LOG = "CONTEXT_ACCESS.ndjson"


def _resolve_under(root: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


def _relative_strings(root: Path, values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        resolved = _resolve_under(root, value)
        if not _contains(root, resolved):
            continue
        result.append(resolved.relative_to(root).as_posix() or ".")
    return sorted(set(result))


def compile_read_policy(
    *, repo_root: Path, task_dir: Path, status: dict[str, Any], execution_policy: dict[str, Any]
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    raw_worktree = str(status.get("worktree") or ".")
    worktree = _resolve_under(repo_root, raw_worktree)
    write_paths = _relative_strings(worktree, list(execution_policy.get("allowed_paths", [])))
    read_paths = _relative_strings(worktree, list(execution_policy.get("read_paths", ["."])))
    forbidden_paths = _relative_strings(worktree, list(execution_policy.get("forbidden_paths", [])))

    denied_roots: set[str] = set()
    # RDO and Claude both use sibling worktree directories. Enumerating concrete
    # siblings avoids relying on backend-specific glob precedence.
    for parent in {worktree.parent, repo_root / ".agent-worktrees", repo_root / ".claude" / "worktrees"}:
        if not parent.is_dir():
            continue
        for child in parent.iterdir():
            if child.is_dir() and child.resolve() != worktree:
                denied_roots.add(str(child.resolve()))

    return {
        "schema_version": 1,
        "repo_root": str(repo_root),
        "worktree": str(worktree),
        "write_paths": write_paths,
        "read_paths": read_paths,
        "forbidden_paths": forbidden_paths,
        # CONTEXT.md is intentionally non-normative. Visibility exceptions are
        # declared only in the machine-readable execution policy.
        "context_sources": _relative_strings(
            worktree, list(execution_policy.get("context_sources", []))
        ),
        "denied_roots": sorted(denied_roots),
        "large_markdown_bytes": LARGE_MARKDOWN_BYTES,
        "section_max_bytes": SECTION_MAX_BYTES,
        "rules": {
            "deny_other_worktrees": True,
            "deny_forbidden_paths": True,
            "bounded_large_markdown_outside_write_scope": True,
        },
    }


def resolve_source(policy: dict[str, Any], value: str) -> Path:
    worktree = Path(policy["worktree"]).resolve()
    source = _resolve_under(worktree, value)
    if not _contains(worktree, source):
        raise ValueError("source is outside the assigned worktree")
    for denied in policy.get("denied_roots", []):
        if _contains(Path(denied).resolve(), source):
            raise ValueError("source belongs to another worktree")
    for forbidden in policy.get("forbidden_paths", []):
        if _contains(_resolve_under(worktree, forbidden), source):
            raise ValueError("source is forbidden by the task policy")
    visible = any(
        _contains(_resolve_under(worktree, allowed), source)
        for allowed in policy.get("read_paths", ["."])
    ) or any(
        source == _resolve_under(worktree, indexed)
        for indexed in policy.get("context_sources", [])
    )
    if not visible:
        raise ValueError("path is outside task read_paths and context_sources")
    return source


def normalize_tool_input(backend: str, tool_name: str, tool_input: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Translate one backend tool call to the common Read/Grep/Glob shape."""
    def first_present(*names: str) -> Any:
        return next((tool_input[name] for name in names if tool_input.get(name) is not None), None)

    lowered = tool_name.casefold()
    operation = "Read" if lowered == "read" else "Grep" if lowered == "grep" else "Glob" if lowered == "glob" else tool_name
    if backend == "kimi-code":
        return operation, {
            "file_path": first_present("path", "file_path"),
            "offset": first_present("line_offset", "offset")
            if operation == "Read" else tool_input.get("offset"),
            "limit": first_present("n_lines", "limit")
            if operation == "Read" else first_present("head_limit", "limit"),
        }
    if backend == "opencode":
        return operation, {
            "file_path": first_present("filePath", "path", "file_path"),
            "offset": tool_input.get("offset"),
            "limit": tool_input.get("limit"),
        }
    return operation, dict(tool_input)


def _access_path_metadata(
    policy: dict[str, Any], raw_path: Any
) -> tuple[str | None, int | None]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, None
    worktree = Path(policy["worktree"]).resolve()
    source = _resolve_under(worktree, raw_path)
    display = (
        source.relative_to(worktree).as_posix() or "."
        if _contains(worktree, source)
        else "outside_worktree"
    )
    source_size: int | None = None
    if _contains(worktree, source) and source.is_file():
        try:
            source_size = source.stat().st_size
        except OSError:
            # Telemetry must not change the policy decision when a file races
            # with the hook between classification and metadata collection.
            source_size = None
    return display, source_size


def record_context_access(
    *,
    runtime: Path,
    policy: dict[str, Any],
    backend: str,
    operation: str,
    tool_input: dict[str, Any],
    decision: str,
    reason: str,
    coverage: str,
    scope: str | None = None,
    bounded: bool | None = None,
) -> None:
    """Append one content-free, normalized read-policy decision.

    O_APPEND plus a single os.write keeps concurrent hook processes from
    racing on a shared file offset. Records describe requests and decisions,
    not proof that the backend subsequently completed the read.
    """
    raw_path = tool_input.get("file_path") or tool_input.get("path")
    display_path, source_size = _access_path_metadata(policy, raw_path)
    offset = tool_input.get("offset")
    limit = tool_input.get("limit")
    if bounded is None:
        bounded = operation in {"Grep", "Glob"} or offset is not None or limit is not None
    record: dict[str, Any] = {
        "schema_version": 1,
        "at": utc_now(),
        "backend": backend,
        "event": "context_access",
        "operation": operation,
        "bounded": bool(bounded),
        "offset": offset,
        "limit": limit,
        "decision": decision,
        "reason": reason,
        "source_size_bytes": source_size,
        "coverage": coverage,
    }
    if display_path is not None:
        record["path"] = display_path
    else:
        record["scope"] = scope or ("." if operation in {"Grep", "Glob"} else "unspecified")
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor = os.open(runtime / CONTEXT_ACCESS_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise OSError(f"short context access log write: {written}/{len(encoded)} bytes")
    finally:
        os.close(descriptor)


def evaluate_read(policy: dict[str, Any], tool_input: dict[str, Any], tool_name: str = "Read") -> str | None:
    """Return a denial reason for one normalized Read/Grep/Glob call, otherwise None."""
    raw_path = tool_input.get("file_path") or tool_input.get("path")
    if (not isinstance(raw_path, str) or not raw_path.strip()) and tool_name in {"Grep", "Glob"}:
        # Preserve backend-native repository discovery. Native Grep/Glob
        # already bound their result sets; treating an omitted path as a full
        # file read creates a denial/retry loop whenever read_paths is narrow.
        # Explicit paths are still checked below, and full Read calls remain
        # subject to read_paths and large-document bounds.
        return None
    if not isinstance(raw_path, str) or not raw_path.strip():
        return "Read requires a concrete file path"
    try:
        source = resolve_source(policy, raw_path)
    except ValueError as exc:
        return str(exc)
    worktree = Path(policy["worktree"]).resolve()
    if not source.exists() or not source.is_file():
        return None  # Let the backend report ordinary missing-file/directory errors.
    in_write_scope = any(
        _contains(_resolve_under(worktree, value), source)
        for value in policy.get("write_paths", [])
    )
    is_markdown = source.suffix.lower() in {".md", ".mdx"}
    threshold = int(policy.get("large_markdown_bytes", LARGE_MARKDOWN_BYTES))
    bounded = tool_input.get("offset") is not None or tool_input.get("limit") is not None
    if tool_name == "Read" and is_markdown and source.stat().st_size > threshold and not in_write_scope and not bounded:
        return (
            f"large Markdown reads outside write scope must use offset/limit; "
            f"search first or use context_broker.py get (size={source.stat().st_size} bytes)"
        )
    return None
