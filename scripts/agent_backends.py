#!/usr/bin/env python3
"""Agent backend registry loader for v0.3 dispatch."""

from __future__ import annotations

import shlex
import json
import sys
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protocol import IO_MODES, PERMISSION_MODES, SKILL_ROOT, WORKER_BACKENDS

BACKENDS_DIR = SKILL_ROOT / "agent_backends"

CAPABILITY_FIELDS: dict[str, set[str]] = {
    "claude-code": {
        "session_settings",
        "pre_tool_hooks",
        "subagent_lifecycle_hooks",
        "tool_stream_events",
        "native_subagents",
        "agent_teams",
        "process_level_native_agent_control",
    },
    "codex": {
        "tool_stream_events",
        "native_subagents",
        "session_config_overrides",
        "subagent_lifecycle_events",
        "native_parallel_agent_limit",
        "native_agent_depth_limit",
    },
    "opencode": {
        "tool_stream_events",
        "native_subagents",
        "session_server",
        "permission_events",
        "native_agent_depth_limit",
        "native_parallel_agent_limit",
    },
    "kimi-code": {
        "tool_stream_events",
        "native_subagents",
        "native_agent_depth_limit",
        "native_swarm_parallel_limit",
        "pre_tool_hooks",
        "subagent_lifecycle_hooks",
    },
}

GOVERNANCE_FIELDS: dict[str, dict[str, str]] = {
    "claude-code": {
        "disabled_plugins": "string_list",
        "enable_agent_teams": "bool",
        "max_tool_use_concurrency": "positive_int",
        "enforce_spawn_limit": "bool",
    },
    "codex": {
        "enable_multi_agent": "bool",
        "max_agent_threads": "positive_int",
        "max_agent_depth": "nonnegative_int",
        "enforce_spawn_limit": "bool",
    },
    "opencode": {
        "enable_native_subagents": "bool",
        "allowed_subagent_types": "string_list",
        "max_parallel_subagents": "positive_int",
        "max_agent_depth": "nonnegative_int",
        "pure_mode": "bool",
    },
    "kimi-code": {
        "enable_native_subagents": "bool",
        "enable_agent_swarm": "bool",
        "max_parallel_subagents": "positive_int",
        "max_agent_depth": "nonnegative_int",
    },
}


@dataclass(frozen=True)
class BackendCommand:
    command: str
    argv: list[str]
    environment: dict[str, str]
    prompt_transport: str
    submit_key: str = ""
    post_paste_delay_ms: int = 0


def load_backend(backend_id: str) -> dict[str, Any]:
    if backend_id not in WORKER_BACKENDS:
        raise ValueError(f"unknown worker backend {backend_id!r}")
    if tomllib is None:
        raise RuntimeError("tomllib is unavailable; Python 3.11+ is required")
    path = BACKENDS_DIR / f"{backend_id}.toml"
    if not path.exists():
        raise FileNotFoundError(f"backend definition not found: {path}")
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    if payload.get("id") != backend_id:
        raise ValueError(f"{path}: id does not match filename")
    return payload


def validate_backend(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    backend_id = payload.get("id")
    if backend_id not in WORKER_BACKENDS:
        errors.append(f"id must be one of {sorted(WORKER_BACKENDS)}")
    roles = payload.get("roles")
    if not isinstance(roles, list) or not all(isinstance(item, str) for item in roles):
        errors.append("roles must be a list of strings")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict):
        errors.append("capabilities must be a table")
    else:
        allowed_capabilities = CAPABILITY_FIELDS.get(str(backend_id), set())
        for key, value in capabilities.items():
            if key not in allowed_capabilities:
                errors.append(f"unsupported capability for {backend_id}: {key}")
            elif not isinstance(value, bool):
                errors.append(f"capabilities.{key} must be boolean")
    governance = payload.get("governance")
    if not isinstance(governance, dict):
        errors.append("governance must be a table")
    else:
        errors.extend(validate_project_governance(str(backend_id), governance, prefix="governance"))
    prompt = payload.get("prompt")
    if not isinstance(prompt, dict):
        errors.append("prompt must be a table")
    else:
        if prompt.get("machine_transport") != "arg":
            errors.append("prompt.machine_transport must be arg in v0.3")
        if prompt.get("human_transport") not in {"arg", "tmux_send_keys"}:
            errors.append("prompt.human_transport must be arg or tmux_send_keys")
    for mode in PERMISSION_MODES:
        entry = payload.get("permission_modes", {}).get(mode)
        if not isinstance(entry, dict):
            errors.append(f"permission_modes.{mode} must be defined")
        elif "supported" not in entry or "args" not in entry:
            errors.append(f"permission_modes.{mode} requires supported and args")
    commands = payload.get("commands")
    if not isinstance(commands, dict):
        errors.append("commands must be a table")
    else:
        for io_mode in IO_MODES:
            command = commands.get(io_mode)
            if not isinstance(command, dict):
                errors.append(f"commands.{io_mode} must be defined")
            elif not isinstance(command.get("command"), str) or not isinstance(command.get("args"), list):
                errors.append(f"commands.{io_mode} requires command string and args list")
    return errors


def validate_project_governance(backend_id: str, policy: Any, *, prefix: str = "backend policy") -> list[str]:
    if backend_id not in WORKER_BACKENDS:
        return [f"unknown backend {backend_id!r}"]
    if not isinstance(policy, dict):
        return [f"{prefix} must be a table"]
    schema = GOVERNANCE_FIELDS.get(backend_id, {})
    errors: list[str] = []
    for key, value in policy.items():
        kind = schema.get(key)
        if kind is None:
            errors.append(f"unsupported governance field for {backend_id}: {key}")
        elif kind == "bool" and not isinstance(value, bool):
            errors.append(f"{prefix}.{key} must be boolean")
        elif kind == "positive_int" and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            errors.append(f"{prefix}.{key} must be a positive integer")
        elif kind == "nonnegative_int" and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            errors.append(f"{prefix}.{key} must be a non-negative integer")
        elif kind == "string_list" and (
            not isinstance(value, list)
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            errors.append(f"{prefix}.{key} must be a list of non-empty strings")
    return errors


def _flatten_args(args: list[Any], replacements: dict[str, str | list[str]]) -> list[str]:
    flattened: list[str] = []
    for item in args:
        if not isinstance(item, str):
            raise ValueError("command args must be strings")
        if item == "{permission_args}":
            value = replacements.get("permission_args", [])
            if isinstance(value, list):
                flattened.extend(value)
            elif value:
                flattened.append(value)
            continue
        rendered = item
        for key, value in replacements.items():
            if isinstance(value, list):
                continue
            rendered = rendered.replace("{" + key + "}", value)
        if rendered:
            flattened.append(rendered)
    return flattened


def build_command(
    *,
    backend_id: str,
    io_mode: str,
    permission_mode: str,
    cwd: str,
    prompt: str,
    agent_name: str,
    backend_profile: str = "",
) -> BackendCommand:
    if io_mode not in IO_MODES:
        raise ValueError(f"io_mode must be one of {sorted(IO_MODES)}")
    if permission_mode not in PERMISSION_MODES:
        raise ValueError(f"permission_mode must be one of {sorted(PERMISSION_MODES)}")
    payload = load_backend(backend_id)
    errors = validate_backend(payload)
    if errors:
        raise ValueError("; ".join(errors))
    permission = payload["permission_modes"][permission_mode]
    if not permission.get("supported"):
        raise ValueError(f"backend {backend_id} does not support permission mode {permission_mode}")
    prompt_info = payload["prompt"]
    transport = prompt_info["machine_transport"] if io_mode == "machine" else prompt_info["human_transport"]
    max_arg_bytes = int(prompt_info.get("max_arg_bytes") or 100000)
    if transport == "arg" and len(prompt.encode("utf-8")) > max_arg_bytes:
        raise ValueError(f"prompt is too large for arg transport: {len(prompt.encode('utf-8'))} > {max_arg_bytes}")
    spec = payload["commands"][io_mode]
    argv = [spec["command"], *_flatten_args(spec["args"], {
        "permission_args": permission.get("args") or [],
        "cwd": cwd,
        "prompt": prompt,
        "agent_name": agent_name,
    })]
    environment: dict[str, str] = {}
    if backend_profile:
        profile_path = Path(backend_profile).resolve()
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        if profile.get("backend_id") != backend_id:
            raise ValueError("backend profile does not match selected backend")
        environment = profile.get("environment", {})
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
        ):
            raise ValueError("backend profile environment must be a string map")
        environment = {
            **environment,
            "RDO_BACKEND_PROFILE": str(profile_path),
            "RDO_BACKEND_PROFILE_SHA256": str(profile.get("profile_sha256") or ""),
        }
        if backend_id == "claude-code":
            settings_path = profile_path.parent / "claude-settings.json"
            if not settings_path.exists():
                raise ValueError("Claude backend profile requires materialized claude-settings.json")
            argv[1:1] = ["--settings", str(settings_path)]
        elif backend_id == "codex":
            codex_settings = profile.get("backend_settings", {})
            config_overrides = codex_settings.get("config_overrides", [])
            if not isinstance(config_overrides, list) or not all(
                isinstance(item, str) and item for item in config_overrides
            ):
                raise ValueError("Codex backend profile config_overrides must be a string list")
            argv[1:1] = ["--strict-config", *(
                part
                for item in config_overrides
                for part in ("-c", item)
            )]
            monitor_required = bool(codex_settings.get("stream_monitor_required", False))
            if monitor_required and io_mode != "machine":
                raise ValueError(
                    "Codex native-subagent governance requires machine IO; "
                    "tmux/human execution cannot enforce the total spawn budget"
                )
            if monitor_required:
                monitor = Path(__file__).resolve().parent / "codex_stream_monitor.py"
                argv = [
                    str(Path(sys.executable)),
                    str(monitor),
                    "--runtime-dir",
                    str(profile_path.parent),
                    "--",
                    *argv,
                ]
        elif backend_id == "kimi-code":
            settings_path = profile_path.parent / "kimi-governance.toml"
            if not settings_path.exists():
                raise ValueError("Kimi backend profile requires materialized kimi-governance.toml")
            wrapper = Path(__file__).resolve().parent / "kimi_attempt_wrapper.py"
            argv = [
                str(Path(sys.executable)),
                str(wrapper),
                "--runtime-dir",
                str(profile_path.parent),
                "--",
                *argv,
            ]
        elif backend_id == "opencode":
            settings = profile.get("backend_settings", {})
            if not settings.get("attempt_supervisor_required"):
                raise ValueError("OpenCode backend profile requires the attempt supervisor")
            supervisor = Path(__file__).resolve().parent / "opencode_attempt_supervisor.py"
            argv = [
                str(Path(sys.executable)),
                str(supervisor),
                "--runtime-dir",
                str(profile_path.parent),
                "--io-mode",
                io_mode,
                "--permission-mode",
                permission_mode,
                "--cwd",
                cwd,
                "--prompt",
                prompt,
            ]
    display_argv = argv
    if environment:
        display_argv = ["env", *(f"{key}={value}" for key, value in sorted(environment.items())), *argv]
    return BackendCommand(
        command=" ".join(shlex.quote(part) for part in display_argv),
        argv=argv,
        environment=environment,
        prompt_transport=str(transport),
        submit_key=str(prompt_info.get("submit_key") or ""),
        post_paste_delay_ms=int(prompt_info.get("post_paste_delay_ms") or 0),
    )
