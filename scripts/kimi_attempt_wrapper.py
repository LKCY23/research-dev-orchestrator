#!/usr/bin/env python3
"""Launch Kimi with an isolated, attempt-local governance overlay."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def set_background_limit(config: str, limit: int) -> str:
    lines = config.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "[background]"), None)
    if start is None:
        return config.rstrip() + f"\n\n[background]\nmax_running_tasks = {limit}\n"
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")),
        len(lines),
    )
    for index in range(start + 1, end):
        if lines[index].split("=", 1)[0].strip() == "max_running_tasks":
            lines[index] = f"max_running_tasks = {limit}"
            break
    else:
        lines.insert(end, f"max_running_tasks = {limit}")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a Kimi command is required after --")

    runtime = Path(args.runtime_dir).resolve()
    fragment = (runtime / "kimi-governance.toml").read_text(encoding="utf-8")
    profile = __import__("json").loads((runtime / "BACKEND_PROFILE.json").read_text(encoding="utf-8"))
    limit = int(profile["backend_settings"]["max_parallel_subagents"])
    source_home = Path(os.environ.get("RDO_KIMI_SOURCE_HOME", "~/.kimi-code")).expanduser().resolve()

    with tempfile.TemporaryDirectory(prefix="rdo-kimi-") as temporary:
        home = Path(temporary)
        source_config = source_home / "config.toml"
        base = source_config.read_text(encoding="utf-8") if source_config.exists() else ""
        config = set_background_limit(base, limit).rstrip() + "\n\n" + fragment
        (home / "config.toml").write_text(config, encoding="utf-8")
        os.chmod(home / "config.toml", 0o600)
        for name in (
            "oauth",
            "credentials",
            "device_id",
            "tui.toml",
            "AGENTS.md",
            "mcp.json",
            "skills",
            "plugins",
        ):
            source = source_home / name
            if not source.exists():
                continue
            target = home / name
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
        source_bin = source_home / "bin"
        for tool in ("fd", "fdfind", "rg", "fd.exe", "rg.exe"):
            source = source_bin / tool
            if source.is_file():
                (home / "bin").mkdir(exist_ok=True)
                shutil.copy2(source, home / "bin" / tool)
        child_env = os.environ.copy()
        child_env["KIMI_CODE_HOME"] = str(home)
        return subprocess.run(command, env=child_env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
