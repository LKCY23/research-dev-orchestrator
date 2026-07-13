#!/usr/bin/env python3
"""Operational configuration loading for research-dev-orchestrator scripts."""

from __future__ import annotations

import os
import re
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None  # type: ignore[assignment]
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from protocol import IO_MODES, PERMISSION_MODES, RUNTIME_BACKENDS, WORKER_BACKENDS
from agent_backends import validate_project_governance


@dataclass(frozen=True)
class RdoConfig:
    worker_command: str = ""
    worker_backend: str = "claude-code"
    worker_agent_name: str = "claude-worker"
    worker_session_id: str = ""
    permission_mode: str = "auto"
    runtime_backend: str = "plain"
    io_mode: str = "machine"
    startup_timeout_seconds: int = 45
    tmux_session_prefix: str = "rdo"
    tmux_keep_session: bool = False
    tmux_wait_timeout_seconds: int = 0
    tmux_exit_code_grace_seconds: int = 60
    stale_lock_hours: float = 6.0
    stale_created_minutes: float = 10.0
    task_branch_prefix: str = "agent/"
    worktree_root: str = ".agent-worktrees"
    backend_policies: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfigLoadResult:
    config: RdoConfig
    warnings: list[str]
    errors: list[str]
    path: Path


TOML_SCHEMA = {
    "worker": {"backend", "command", "agent_name", "permission_mode"},
    "runtime": {"backend", "io_mode", "startup_timeout_seconds"},
    "tmux": {"session_prefix", "keep_session", "wait_timeout_seconds", "exit_code_grace_seconds"},
    "status": {"stale_lock_hours", "stale_created_minutes"},
    "task": {"branch_prefix", "worktree_root"},
    "backends": None,
}


ENV_MAP = {
    "RDO_WORKER_COMMAND": ("worker_command", "string_empty_ok"),
    "CLAUDE_CODE_CMD": ("worker_command", "string"),
    "RDO_WORKER_BACKEND": ("worker_backend_or_legacy_runtime", "worker_backend_or_legacy_runtime"),
    "CLAUDE_AGENT_NAME": ("worker_agent_name", "string"),
    "RDO_WORKER_AGENT_NAME": ("worker_agent_name", "string"),
    "CLAUDE_SESSION_ID": ("worker_session_id", "string_empty_ok"),
    "RDO_BACKEND_SESSION_ID": ("worker_session_id", "string_empty_ok"),
    "RDO_PERMISSION_MODE": ("permission_mode", "permission_mode"),
    "RDO_RUNTIME_BACKEND": ("runtime_backend", "runtime_backend"),
    "RDO_IO_MODE": ("io_mode", "io_mode"),
    "RDO_STARTUP_TIMEOUT_SECONDS": ("startup_timeout_seconds", "int_positive"),
    "RDO_TMUX_SESSION_PREFIX": ("tmux_session_prefix", "string"),
    "RDO_TMUX_KEEP_SESSION": ("tmux_keep_session", "bool"),
    "RDO_TMUX_WAIT_TIMEOUT_SECONDS": ("tmux_wait_timeout_seconds", "int_nonnegative"),
    "RDO_TMUX_EXIT_CODE_GRACE_SECONDS": ("tmux_exit_code_grace_seconds", "int_nonnegative"),
    "RDO_STALE_LOCK_HOURS": ("stale_lock_hours", "float_nonnegative"),
    "RDO_STALE_CREATED_MINUTES": ("stale_created_minutes", "float_nonnegative"),
    "RDO_TASK_BRANCH_PREFIX": ("task_branch_prefix", "string"),
    "RDO_WORKTREE_ROOT": ("worktree_root", "string"),
}


TOML_MAP = {
    ("worker", "backend"): ("worker_backend", "worker_backend"),
    ("worker", "command"): ("worker_command", "string_empty_ok"),
    ("worker", "agent_name"): ("worker_agent_name", "string"),
    ("worker", "permission_mode"): ("permission_mode", "permission_mode"),
    ("runtime", "backend"): ("runtime_backend", "runtime_backend"),
    ("runtime", "io_mode"): ("io_mode", "io_mode"),
    ("runtime", "startup_timeout_seconds"): ("startup_timeout_seconds", "int_positive"),
    ("tmux", "session_prefix"): ("tmux_session_prefix", "string"),
    ("tmux", "keep_session"): ("tmux_keep_session", "bool"),
    ("tmux", "wait_timeout_seconds"): ("tmux_wait_timeout_seconds", "int_nonnegative"),
    ("tmux", "exit_code_grace_seconds"): ("tmux_exit_code_grace_seconds", "int_nonnegative"),
    ("status", "stale_lock_hours"): ("stale_lock_hours", "float_nonnegative"),
    ("status", "stale_created_minutes"): ("stale_created_minutes", "float_nonnegative"),
    ("task", "branch_prefix"): ("task_branch_prefix", "string"),
    ("task", "worktree_root"): ("worktree_root", "string"),
}


def config_path(repo_root: Path) -> Path:
    return repo_root / ".agent-collab" / "rdo.toml"


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def coerce_value(value: Any, kind: str) -> tuple[Any, str | None]:
    if kind == "string":
        if isinstance(value, str) and value.strip():
            return value, None
        return None, "must be a non-empty string"
    if kind == "string_empty_ok":
        if isinstance(value, str):
            return value, None
        return None, "must be a string"
    if kind == "runtime_backend":
        if isinstance(value, str) and value in RUNTIME_BACKENDS:
            return value, None
        return None, f"must be one of {sorted(RUNTIME_BACKENDS)}"
    if kind == "worker_backend":
        if isinstance(value, str) and value in WORKER_BACKENDS:
            return value, None
        return None, f"must be one of {sorted(WORKER_BACKENDS)}"
    if kind == "worker_backend_or_legacy_runtime":
        if isinstance(value, str) and value in WORKER_BACKENDS:
            return ("worker_backend", value), None
        if isinstance(value, str) and value in RUNTIME_BACKENDS:
            return ("runtime_backend", value), None
        return None, f"must be one of worker backends {sorted(WORKER_BACKENDS)} or legacy runtime backends {sorted(RUNTIME_BACKENDS)}"
    if kind == "io_mode":
        if isinstance(value, str) and value in IO_MODES:
            return value, None
        return None, f"must be one of {sorted(IO_MODES)}"
    if kind == "permission_mode":
        if isinstance(value, str) and value in PERMISSION_MODES:
            return value, None
        return None, f"must be one of {sorted(PERMISSION_MODES)}"
    if kind == "bool":
        parsed = parse_bool(value)
        if parsed is not None:
            return parsed, None
        return None, "must be a boolean"
    if kind == "int_nonnegative":
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value, None
        if isinstance(value, str) and re.match(r"^[0-9]+$", value):
            return int(value), None
        return None, "must be a non-negative integer"
    if kind == "int_positive":
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value, None
        if isinstance(value, str) and re.match(r"^[1-9][0-9]*$", value):
            return int(value), None
        return None, "must be a positive integer"
    if kind == "float_nonnegative":
        if isinstance(value, bool):
            return None, "must be a non-negative number"
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None, "must be a non-negative number"
        if parsed >= 0:
            return parsed, None
        return None, "must be a non-negative number"
    raise ValueError(f"unknown config kind: {kind}")


def apply_field(config: RdoConfig, field: str, value: Any) -> RdoConfig:
    return replace(config, **{field: value})


def load_toml(path: Path) -> tuple[dict[str, Any], list[str]]:
    if not path.exists():
        return {}, []
    if tomllib is None:
        return {}, ["Python tomllib is unavailable; .agent-collab/rdo.toml was not loaded"]
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:  # type: ignore[union-attr]
        return {}, [f"{path}: invalid TOML: {exc}"]
    if not isinstance(payload, dict):
        return {}, [f"{path}: root must be a TOML table"]
    return payload, []


def apply_toml(config: RdoConfig, payload: dict[str, Any]) -> tuple[RdoConfig, list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    updated = config
    for section, value in payload.items():
        if section not in TOML_SCHEMA:
            warnings.append(f"unknown section [{section}]")
            continue
        if section == "backends":
            if not isinstance(value, dict):
                errors.append("[backends] must be a table")
                continue
            policies: dict[str, dict[str, Any]] = {}
            for backend_id, policy in value.items():
                backend_errors = validate_project_governance(
                    backend_id, policy, prefix=f'[backends."{backend_id}"]'
                )
                errors.extend(backend_errors)
                if not backend_errors:
                    policies[backend_id] = dict(policy)
            updated = apply_field(updated, "backend_policies", policies)
            continue
        if not isinstance(value, dict):
            errors.append(f"[{section}] must be a table")
            continue
        for key, item in value.items():
            if key not in TOML_SCHEMA[section]:
                warnings.append(f"unknown key [{section}].{key}")
                continue
            field, kind = TOML_MAP[(section, key)]
            coerced, error = coerce_value(item, kind)
            if error:
                errors.append(f"[{section}].{key} {error}")
                continue
            updated = apply_field(updated, field, coerced)
    return updated, warnings, errors


def apply_env(config: RdoConfig, environ: Mapping[str, str]) -> tuple[RdoConfig, list[str]]:
    errors: list[str] = []
    updated = config
    for env_name, (field, kind) in ENV_MAP.items():
        if env_name not in environ:
            continue
        coerced, error = coerce_value(environ[env_name], kind)
        if error:
            errors.append(f"env: {env_name} {error}")
            continue
        if kind == "worker_backend_or_legacy_runtime":
            field, value = coerced
            updated = apply_field(updated, field, value)
            continue
        updated = apply_field(updated, field, coerced)
    return updated, errors


def load_config(repo_root: Path, environ: Mapping[str, str] | None = None, *, use_env: bool = True) -> ConfigLoadResult:
    path = config_path(repo_root)
    config = RdoConfig()
    warnings: list[str] = []
    errors: list[str] = []
    payload, load_errors = load_toml(path)
    errors.extend(load_errors)
    if payload:
        config, toml_warnings, toml_errors = apply_toml(config, payload)
        warnings.extend(toml_warnings)
        errors.extend(toml_errors)
    if use_env:
        config, env_errors = apply_env(config, os.environ if environ is None else environ)
        errors.extend(env_errors)
    return ConfigLoadResult(config=config, warnings=warnings, errors=errors, path=path)
