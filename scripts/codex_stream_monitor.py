#!/usr/bin/env python3
"""Supervise Codex JSONL output and enforce attempt-local native-agent budgets."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from protocol import load_json, utc_now
from supervisor import terminate_processes


VIOLATION_EXIT_CODE = 125
ACTIVE_AGENT_STATES = {"pending_init", "running"}


def atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_line(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def default_state(profile_sha256: str | None) -> dict[str, Any]:
    return {
        "profile_sha256": profile_sha256,
        "total_requests": 0,
        "total_starts": 0,
        "active": {},
        "peak_active": 0,
        "denied": 0,
        "spawn_items": [],
    }


def record_violation(runtime: Path, profile: dict[str, Any], reason: str) -> None:
    append_line(runtime / "VIOLATIONS.ndjson", {
        "at": utc_now(),
        "event": "backend_governance_violation",
        "backend_id": "codex",
        "hard": True,
        "reason": reason,
        "profile_sha256": profile.get("profile_sha256"),
    })


def _update_agent_states(
    runtime: Path,
    profile: dict[str, Any],
    state: dict[str, Any],
    item: dict[str, Any],
) -> None:
    agent_states = item.get("agents_states", {})
    if not isinstance(agent_states, dict):
        return
    for agent_id, details in agent_states.items():
        if not isinstance(agent_id, str) or not isinstance(details, dict):
            continue
        status = str(details.get("status") or "")
        was_active = agent_id in state["active"]
        if status in ACTIVE_AGENT_STATES:
            state["active"][agent_id] = {
                "status": status,
                "started_at": state["active"].get(agent_id, {}).get("started_at") or utc_now(),
            }
            if not was_active:
                state["total_starts"] = int(state["total_starts"]) + 1
                append_line(runtime / "BACKEND_EVENTS.ndjson", {
                    "at": utc_now(),
                    "event": "backend_agent_started",
                    "backend_id": "codex",
                    "agent_id": agent_id,
                    "profile_sha256": profile.get("profile_sha256"),
                })
        elif was_active:
            state["active"].pop(agent_id, None)
            append_line(runtime / "BACKEND_EVENTS.ndjson", {
                "at": utc_now(),
                "event": "backend_agent_stopped",
                "backend_id": "codex",
                "agent_id": agent_id,
                "status": status or "unknown",
                "profile_sha256": profile.get("profile_sha256"),
            })
    state["peak_active"] = max(int(state["peak_active"]), len(state["active"]))


def process_event(
    runtime: Path,
    profile: dict[str, Any],
    state: dict[str, Any],
    event: dict[str, Any],
) -> str | None:
    if event.get("type") not in {"item.started", "item.updated", "item.completed"}:
        return None
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "collab_tool_call":
        return None
    _update_agent_states(runtime, profile, state, item)
    if item.get("tool") != "spawn_agent":
        return None
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        return "Codex spawn_agent event is missing a stable item id"
    spawn_items = set(state.get("spawn_items", []))
    if item_id in spawn_items:
        return None
    spawn_items.add(item_id)
    state["spawn_items"] = sorted(spawn_items)
    state["total_requests"] = int(state["total_requests"]) + 1
    append_line(runtime / "BACKEND_EVENTS.ndjson", {
        "at": utc_now(),
        "event": "backend_agent_requested",
        "backend_id": "codex",
        "item_id": item_id,
        "sender_thread_id": item.get("sender_thread_id"),
        "receiver_thread_ids": item.get("receiver_thread_ids", []),
        "profile_sha256": profile.get("profile_sha256"),
    })
    max_spawns = int(profile.get("native_agent_limits", {}).get("max_spawns", 0))
    enforce_max_spawns = bool(profile.get("native_agent_limits", {}).get("enforce_max_spawns", False))
    if enforce_max_spawns and int(state["total_requests"]) > max_spawns:
        state["denied"] = int(state["denied"]) + 1
        return f"Codex native agent spawn budget exceeded: {state['total_requests']} > {max_spawns}"
    return None


def run(runtime: Path, command: Sequence[str]) -> int:
    profile = load_json(runtime / "BACKEND_PROFILE.json")
    if profile.get("backend_id") != "codex":
        raise ValueError("Codex stream monitor requires a Codex backend profile")
    state = default_state(profile.get("profile_sha256"))
    atomic_json(runtime / "AGENTS.json", state)
    child = subprocess.Popen(
        list(command),
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    def forward_signal(signum: int, _frame: Any) -> None:
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    previous_handlers = {
        sig: signal.signal(sig, forward_signal)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    violation: str | None = None
    try:
        assert child.stdout is not None
        for line in child.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                violation = f"Codex JSONL stream is invalid: {exc}"
            else:
                if not isinstance(event, dict):
                    violation = "Codex JSONL event must be an object"
                else:
                    violation = process_event(runtime, profile, state, event)
            atomic_json(runtime / "AGENTS.json", state)
            if violation:
                record_violation(runtime, profile, violation)
                terminate_processes({child.pid}, {child.pid})
                break
        child.wait()
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
    atomic_json(runtime / "AGENTS.json", state)
    return VIOLATION_EXIT_CODE if violation else int(child.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Codex JSONL governance events")
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        parser.error("a Codex command is required after --")
    return run(Path(args.runtime_dir).resolve(), command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"RDO Codex stream monitor failed: {exc}", file=sys.stderr)
        raise SystemExit(VIOLATION_EXIT_CODE)
