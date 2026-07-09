#!/usr/bin/env python3
"""Agent backend registry loader for v0.3 dispatch."""

from __future__ import annotations

import shlex
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protocol import IO_MODES, PERMISSION_MODES, SKILL_ROOT, WORKER_BACKENDS

BACKENDS_DIR = SKILL_ROOT / "agent_backends"


@dataclass(frozen=True)
class BackendCommand:
    command: str
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
    return BackendCommand(
        command=" ".join(shlex.quote(part) for part in argv),
        prompt_transport=str(transport),
        submit_key=str(prompt_info.get("submit_key") or ""),
        post_paste_delay_ms=int(prompt_info.get("post_paste_delay_ms") or 0),
    )
