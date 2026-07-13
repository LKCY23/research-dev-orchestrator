#!/usr/bin/env python3
"""Fail-fast checks for one RDO worker backend before dispatch mutation."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent_backends import load_backend


AUTH_PROBES: dict[str, tuple[list[str], str]] = {
    "claude-code": (["auth", "status", "--json"], "claude-json"),
    "codex": (["login", "status"], "exit-code"),
}


def run_probe(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def auth_state(backend_id: str, executable: str) -> tuple[str, str]:
    probe = AUTH_PROBES.get(backend_id)
    if probe is None:
        return "unknown", "backend has no deterministic auth probe"
    args, mode = probe
    result = run_probe([executable, *args])
    if mode == "claude-json" and result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "unknown", "auth probe returned invalid JSON"
        logged_in = payload.get("loggedIn")
        if logged_in is True:
            return "authenticated", "claude auth status reports loggedIn=true"
        if logged_in is False:
            return "unauthenticated", "claude auth status reports loggedIn=false"
        return "unknown", "claude auth status omitted loggedIn"
    if mode == "exit-code":
        return (
            ("authenticated", "auth status exited successfully")
            if result.returncode == 0
            else ("unauthenticated", (result.stderr or result.stdout or "auth status failed").strip())
        )
    return "unknown", "unsupported auth probe semantics"


def preflight(backend_id: str, command_override: str = "") -> dict[str, Any]:
    if command_override:
        parts = shlex.split(command_override)
        executable_name = parts[0] if parts else ""
        source = "override"
    else:
        backend = load_backend(backend_id)
        executable_name = str(backend["commands"]["machine"]["command"])
        source = "registry"
    executable = shutil.which(executable_name) if executable_name else None
    if executable is None and executable_name and Path(executable_name).is_file():
        executable = str(Path(executable_name).resolve())
    errors: list[str] = []
    version = ""
    auth = "unknown"
    auth_detail = "custom command auth is not probed"
    if executable is None:
        errors.append(f"worker executable not found: {executable_name!r}")
    elif source == "registry":
        try:
            result = run_probe([executable, "--version"])
        except subprocess.TimeoutExpired:
            errors.append(f"worker version probe timed out: {executable}")
        else:
            if result.returncode != 0:
                errors.append(f"worker version probe failed with exit code {result.returncode}")
            version = (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr).strip() else ""
        if not errors:
            try:
                auth, auth_detail = auth_state(backend_id, executable)
            except subprocess.TimeoutExpired:
                auth, auth_detail = "unknown", "auth probe timed out"
            if auth == "unauthenticated":
                errors.append(f"worker is not authenticated: {auth_detail}")
    return {
        "backend_id": backend_id,
        "source": source,
        "executable": executable,
        "version": version,
        "auth": auth,
        "auth_detail": auth_detail,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight one RDO worker backend.")
    parser.add_argument("--backend", required=True)
    parser.add_argument("--command", default="")
    args = parser.parse_args()
    result = preflight(args.backend, args.command)
    print(json.dumps(result, indent=2))
    if result["errors"]:
        for error in result["errors"]:
            print(f"preflight error: {error}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
