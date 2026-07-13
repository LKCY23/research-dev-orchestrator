#!/usr/bin/env python3
"""Attempt-local Claude hook for bounded native-agent lifecycle accounting."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Any

from protocol import load_json, utc_now


def atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_line(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def default_state() -> dict[str, Any]:
    return {
        "total_requests": 0,
        "total_starts": 0,
        "inflight": 0,
        "active": {},
        "peak_active": 0,
        "denied": 0,
    }


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument(
        "--event",
        required=True,
        choices=["pre-tool-use", "post-tool-use", "subagent-start", "subagent-stop"],
    )
    args = parser.parse_args()
    runtime = Path(args.runtime_dir).resolve()
    profile = load_json(runtime / "BACKEND_PROFILE.json")
    limits = profile.get("native_agent_limits", {})
    max_spawns = int(limits.get("max_spawns", 0))
    enforce_max_spawns = bool(limits.get("enforce_max_spawns", False))
    max_parallel = int(limits.get("max_parallel", 0))
    hook_input = json.load(sys.stdin)
    state_path = runtime / "AGENTS.json"
    lock_path = runtime / "governance-counter.lock"
    events_path = runtime / "BACKEND_EVENTS.ndjson"
    violations_path = runtime / "VIOLATIONS.ndjson"
    lock_path.touch(exist_ok=True)

    with lock_path.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_json(state_path) if state_path.exists() else default_state()
        event = {
            "at": utc_now(),
            "event": args.event,
            "profile_sha256": profile.get("profile_sha256"),
        }
        if args.event == "pre-tool-use":
            occupied = max(int(state["inflight"]), len(state["active"]))
            if enforce_max_spawns and int(state["total_requests"]) >= max_spawns:
                state["denied"] = int(state["denied"]) + 1
                event.update(result="denied", reason="native agent spawn budget exhausted")
                append_line(events_path, event)
                atomic_json(state_path, state)
                deny(f"RDO strategy permits at most {max_spawns} native agent launches")
                return 0
            if occupied >= max_parallel:
                state["denied"] = int(state["denied"]) + 1
                event.update(result="denied", reason="native agent parallel budget exhausted")
                append_line(events_path, event)
                atomic_json(state_path, state)
                deny(f"RDO strategy permits at most {max_parallel} concurrent native agents")
                return 0
            state["total_requests"] = int(state["total_requests"]) + 1
            state["inflight"] = int(state["inflight"]) + 1
            event["result"] = "reserved"
        elif args.event == "post-tool-use":
            state["inflight"] = max(0, int(state["inflight"]) - 1)
            event["result"] = "reservation_released"
        elif args.event == "subagent-start":
            agent_id = str(hook_input.get("agent_id") or f"unknown-{state['total_requests']}")
            state["total_starts"] = int(state["total_starts"]) + 1
            state["active"][agent_id] = {
                "agent_type": hook_input.get("agent_type"),
                "started_at": event["at"],
            }
            state["peak_active"] = max(int(state["peak_active"]), len(state["active"]))
            event.update(agent_id=agent_id, agent_type=hook_input.get("agent_type"), result="started")
            violations: list[str] = []
            if enforce_max_spawns and int(state["total_starts"]) > max_spawns:
                violations.append("native agent spawn budget exceeded")
            if len(state["active"]) > max_parallel:
                violations.append("native agent parallel budget exceeded")
            if violations:
                append_line(violations_path, {
                    "at": event["at"],
                    "event": "backend_governance_violation",
                    "hard": True,
                    "reasons": violations,
                    "profile_sha256": profile.get("profile_sha256"),
                })
        else:
            agent_id = str(hook_input.get("agent_id") or "")
            state["active"].pop(agent_id, None)
            event.update(agent_id=agent_id, result="stopped")
        append_line(events_path, event)
        atomic_json(state_path, state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        runtime = None
        try:
            runtime = Path(sys.argv[sys.argv.index("--runtime-dir") + 1]).resolve()
            append_line(runtime / "VIOLATIONS.ndjson", {
                "at": utc_now(),
                "event": "backend_governance_hook_failure",
                "hard": True,
                "reason": str(exc),
            })
        except Exception:
            pass
        print(f"RDO backend governance hook failed: {exc}", file=sys.stderr)
        raise SystemExit(2 if "pre-tool-use" in sys.argv else 0)
