#!/usr/bin/env python3
"""Detect a human-only startup gate from an attachable worker pane."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from protocol import utc_now


WAIT_PATTERNS = (
    re.compile(r"do you trust", re.IGNORECASE),
    re.compile(r"trust (?:this|the) (?:folder|workspace|directory|project)", re.IGNORECASE),
    re.compile(r"authentication required", re.IGNORECASE),
    re.compile(r"(?:log|sign) in to continue", re.IGNORECASE),
    re.compile(r"press enter to continue", re.IGNORECASE),
    re.compile(r"confirm.*dangerously", re.IGNORECASE),
)


def waiting_reason(pane_text: str) -> str | None:
    for pattern in WAIT_PATTERNS:
        match = pattern.search(pane_text)
        if match:
            return match.group(0)
    return None


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe a tmux human worker for startup gates.")
    parser.add_argument("--startup-path", required=True)
    parser.add_argument("--pane-path", required=True)
    args = parser.parse_args()
    pane_text = Path(args.pane_path).read_text(encoding="utf-8", errors="replace")
    reason = waiting_reason(pane_text)
    if reason is None:
        return 1
    startup_path = Path(args.startup_path)
    try:
        payload = json.loads(startup_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        payload = {"mode": "human"}
    if not isinstance(payload, dict) or payload.get("state") == "tui_startup_failed":
        return 1
    payload.update(
        state="worker_waiting_for_user",
        waiting_since=utc_now(),
        waiting_reason=reason,
        startup_evidence={"event": "pane_confirmation_gate"},
    )
    atomic_json(startup_path, payload)
    print(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
