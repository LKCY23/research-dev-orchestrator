#!/usr/bin/env python3
"""Detect a human-only startup gate from an attachable worker pane."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from backend_startup import classify_human_startup
from protocol import utc_now


def waiting_reason(pane_text: str) -> str | None:
    assessment = classify_human_startup("", pane_text)
    if assessment and assessment["kind"] == "waiting":
        return str(assessment["reason"])
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
    parser.add_argument("--backend", default="")
    args = parser.parse_args()
    pane_text = Path(args.pane_path).read_text(encoding="utf-8", errors="replace")
    assessment = classify_human_startup(args.backend, pane_text)
    if assessment is None:
        return 1
    startup_path = Path(args.startup_path)
    try:
        payload = json.loads(startup_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        payload = {"mode": "human"}
    if not isinstance(payload, dict) or payload.get("state") == "tui_startup_failed":
        return 1
    if assessment["kind"] == "failed":
        failure = dict(assessment["failure"])
        payload.update(
            state="tui_startup_failed",
            failed_at=utc_now(),
            failure=failure,
            startup_evidence={"event": "pane_startup_failure"},
        )
        atomic_json(startup_path, payload)
        print(failure["code"])
        return 2
    reason = str(assessment["reason"])
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
