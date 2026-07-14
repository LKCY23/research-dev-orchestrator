#!/usr/bin/env python3
"""Run one non-interactive worker with deterministic prompt and startup handling."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any

from protocol import utc_now
from supervisor import _process_table, descendants, terminate_processes
from usage import UsageSupervisor


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def startup_event(backend: str, line: bytes) -> str | None:
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")
    if backend == "claude-code":
        return "system/init" if event_type == "system" and payload.get("subtype") == "init" else None
    if backend == "codex":
        return "thread.started" if event_type in {"thread.started", "thread_started"} else None
    if backend in {"kimi-code", "opencode"} and isinstance(event_type, str) and event_type:
        return event_type
    return None


def session_id_from_event(backend: str, line: bytes) -> str:
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    candidates: list[Any] = [
        payload.get("session_id"), payload.get("sessionId"), payload.get("sessionID"),
        payload.get("thread_id"), payload.get("threadId"),
    ]
    for key in ("thread", "session", "properties", "info"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.extend([
                nested.get("id"), nested.get("session_id"), nested.get("sessionID"),
                nested.get("thread_id"),
            ])
    return next((str(value) for value in candidates if isinstance(value, str) and value), "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervise one machine-mode RDO attempt.")
    parser.add_argument("--backend", required=True)
    parser.add_argument("--argv-json", required=True)
    parser.add_argument("--environment-json", default="{}")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--prompt-path", required=True)
    parser.add_argument("--prompt-transport", choices=["arg", "stdin"], required=True)
    parser.add_argument("--startup-timeout-seconds", type=float, required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--startup-result", required=True)
    parser.add_argument("--supervisor-result", required=True)
    parser.add_argument("--supervisor-state", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--session-result", default="")
    parser.add_argument("--existing-session-id", default="")
    parser.add_argument("--strategy-id", default="")
    parser.add_argument("--strategy-sha256", default="")
    parser.add_argument("--custom-command", action="store_true")
    parser.add_argument("--backend-profile", default="")
    args = parser.parse_args()

    argv = json.loads(args.argv_json)
    environment = json.loads(args.environment_json)
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise SystemExit("--argv-json must be a non-empty string array")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise SystemExit("--environment-json must be a string map")
    if args.startup_timeout_seconds <= 0 or args.timeout_seconds <= 0:
        raise SystemExit("startup and attempt timeouts must be positive")

    startup_path = Path(args.startup_result)
    supervisor_path = Path(args.supervisor_result)
    state_path = Path(args.supervisor_state)
    transcript_path = Path(args.transcript)
    prompt_path = Path(args.prompt_path)
    prompt_sha256 = __import__("hashlib").sha256(prompt_path.read_bytes()).hexdigest()
    session_path = Path(args.session_result) if args.session_result else None
    resource_budget: dict[str, Any] = {}
    if args.backend_profile:
        profile = json.loads(Path(args.backend_profile).read_text(encoding="utf-8"))
        resource_budget = profile.get("resource_budget", {})
    usage = UsageSupervisor(transcript_path.parent / "runtime", args.backend, resource_budget)
    observed_session_id = args.existing_session_id
    if observed_session_id and session_path is not None:
        atomic_json(session_path, {"backend_id": args.backend, "session_id": observed_session_id})
    env = os.environ.copy()
    env.update(environment)
    stdin_spec: int = subprocess.DEVNULL if args.prompt_transport == "arg" else subprocess.PIPE
    started_monotonic = time.monotonic()
    started_at = utc_now()
    process = subprocess.Popen(
        argv,
        cwd=args.cwd,
        env=env,
        stdin=stdin_spec,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    startup: dict[str, Any] = {
        "mode": "machine",
        "state": "process_started",
        "backend_id": args.backend,
        "prompt_transport": args.prompt_transport,
        "prompt_sha256": prompt_sha256,
        "process_started_at": started_at,
        "prompt_dispatched_at": None,
        "worker_started_at": None,
        "startup_timeout_seconds": args.startup_timeout_seconds,
        "startup_evidence": None,
        "failure": None,
    }
    atomic_json(startup_path, startup)
    if args.prompt_transport == "stdin":
        assert process.stdin is not None
        process.stdin.write(prompt_path.read_bytes())
        process.stdin.close()
    startup["state"] = "prompt_dispatched"
    startup["prompt_dispatched_at"] = utc_now()
    atomic_json(startup_path, startup)
    if args.custom_command:
        startup["state"] = "worker_started"
        startup["worker_started_at"] = utc_now()
        startup["startup_evidence"] = {"decoder": "custom-process", "event": "process_started"}
        atomic_json(startup_path, startup)

    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = b""
    observed_pids = {process.pid}
    observed_pgids = {process.pid}
    timed_out = False
    startup_failed = False
    budget_exceeded = False
    last_state_write = 0.0
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    with transcript_path.open("wb") as transcript:
        while process.poll() is None:
            for key, _ in selector.select(timeout=0.1):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                transcript.write(chunk)
                transcript.flush()
                os.write(1, chunk)
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        usage_payload = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        usage_payload = None
                    if usage.observe(usage_payload):
                        budget_exceeded = True
                        break
                    evidence = startup_event(args.backend, line)
                    candidate_session_id = session_id_from_event(args.backend, line)
                    if candidate_session_id and not observed_session_id:
                        observed_session_id = candidate_session_id
                        if session_path is not None:
                            atomic_json(session_path, {"backend_id": args.backend, "session_id": observed_session_id})
                    if evidence and startup["state"] != "worker_started":
                        startup["state"] = "worker_started"
                        startup["worker_started_at"] = utc_now()
                        startup["startup_evidence"] = {"decoder": args.backend, "event": evidence}
                        atomic_json(startup_path, startup)
                if budget_exceeded:
                    break
            try:
                table = _process_table()
                current = descendants(process.pid, table)
                observed_pids.update(current)
                observed_pgids.update(table[pid][1] for pid in current if pid in table)
            except (OSError, subprocess.SubprocessError):
                pass
            elapsed = time.monotonic() - started_monotonic
            if elapsed - last_state_write >= 0.5:
                atomic_json(state_path, {
                    "state": "running",
                    "worker_pid": process.pid,
                    "worker_pgid": process.pid,
                    "observed_pids": sorted(observed_pids),
                    "observed_pgids": sorted(observed_pgids),
                    "deadline_seconds": args.timeout_seconds,
                    "startup_state": startup["state"],
                })
                last_state_write = elapsed
            if startup["state"] != "worker_started" and elapsed >= args.startup_timeout_seconds:
                startup_failed = True
                startup["state"] = "worker_startup_failed"
                startup["failure"] = {"code": "startup_timeout", "message": "no valid backend startup event"}
                atomic_json(startup_path, startup)
                break
            if usage.check_clock():
                budget_exceeded = True
                break
            if elapsed >= args.timeout_seconds:
                timed_out = True
                break
        if process.poll() is None and (timed_out or startup_failed or budget_exceeded):
            survivors = terminate_processes(observed_pgids, observed_pids)
            process.wait()
        else:
            exit_code_now = int(process.wait())
            survivors = terminate_processes(observed_pgids, observed_pids - {process.pid})
            if buffer:
                evidence = startup_event(args.backend, buffer)
                if evidence and startup["state"] != "worker_started":
                    startup["state"] = "worker_started"
                    startup["worker_started_at"] = utc_now()
                    startup["startup_evidence"] = {"decoder": args.backend, "event": evidence}
                    atomic_json(startup_path, startup)
            if startup["state"] != "worker_started":
                startup_failed = True
                startup["state"] = "worker_startup_failed"
                startup["failure"] = {
                    "code": "early_exit",
                    "message": f"worker exited before a valid startup event (exit {exit_code_now})",
                }
                atomic_json(startup_path, startup)

    exit_code = 124 if timed_out else 125 if startup_failed or budget_exceeded else int(process.returncode)
    state = (
        "timed_out" if timed_out else "startup_failed" if startup_failed
        else "budget_exceeded" if budget_exceeded else "completed"
    )
    atomic_json(state_path, {
        "state": state,
        "worker_pid": process.pid,
        "worker_pgid": process.pid,
        "observed_pids": sorted(observed_pids),
        "observed_pgids": sorted(observed_pgids),
        "surviving_pids": list(survivors),
        "exit_code": exit_code,
        "startup_state": startup["state"],
        "usage": usage.summary(),
    })
    atomic_json(supervisor_path, {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "startup_failed": startup_failed,
        "budget_exceeded": budget_exceeded,
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 6),
        "observed_pids": sorted(observed_pids),
        "observed_pgids": sorted(observed_pgids),
        "surviving_pids": list(survivors),
        "strategy_id": args.strategy_id or None,
        "strategy_sha256": args.strategy_sha256 or None,
        "backend_session_id": observed_session_id or None,
        "usage": usage.summary(),
    })
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
