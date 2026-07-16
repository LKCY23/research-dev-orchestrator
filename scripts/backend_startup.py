#!/usr/bin/env python3
"""Deterministic backend session and startup-failure inspection."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any


_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_SESSION_NOT_FOUND = (
    re.compile(r"\bno conversation found with session id\b", re.IGNORECASE),
    re.compile(r"\bsession(?: id)?\b.{0,80}\bnot found\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bfailed to resume (?:the )?session\b", re.IGNORECASE),
    re.compile(r"\bfailed to resume session from\b", re.IGNORECASE),
    re.compile(r"\bunknown (?:conversation|session|thread)\b", re.IGNORECASE),
)
_AUTHENTICATION = (
    re.compile(r"\bauthentication required\b", re.IGNORECASE),
    re.compile(r"\bnot logged in\b", re.IGNORECASE),
    re.compile(r"\b(?:log|sign) in to continue\b", re.IGNORECASE),
    re.compile(r"\bauthentication failed\b", re.IGNORECASE),
    re.compile(r"\bunauthori[sz]ed\b", re.IGNORECASE),
)
_PERMISSION_CONFIRMATION = (
    re.compile(r"\bbypass permissions mode\b", re.IGNORECASE),
    re.compile(r"\byes,\s*i accept\b", re.IGNORECASE),
    re.compile(r"\bconfirm.{0,80}dangerously\b", re.IGNORECASE | re.DOTALL),
)
_INVALID_CLI = (
    re.compile(r"\bunexpected argument\b", re.IGNORECASE),
    re.compile(r"\bunrecognized (?:option|argument)\b", re.IGNORECASE),
    re.compile(r"\bunknown (?:option|argument)\b", re.IGNORECASE),
    re.compile(r"\binvalid value\b.{0,80}--[a-z0-9-]+", re.IGNORECASE | re.DOTALL),
)
_WAITING = (
    re.compile(r"\bdo you trust\b", re.IGNORECASE),
    re.compile(
        r"\btrust (?:this|the) (?:folder|workspace|directory|project)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bpress enter to continue\b", re.IGNORECASE),
)


def _match(patterns: tuple[re.Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return " ".join(match.group(0).split())
    return None


def classify_startup_failure(
    backend_id: str,
    text: str,
    *,
    returncode: int | None = None,
) -> dict[str, Any] | None:
    """Classify bounded startup output without invoking a model."""

    bounded = text[-65536:]
    reason = _match(_SESSION_NOT_FOUND, bounded)
    if reason:
        return {
            "code": "session_not_found",
            "message": reason,
            "category": "resume",
            "recoverable_resume_failure": True,
            "backend_id": backend_id,
        }
    reason = _match(_AUTHENTICATION, bounded)
    if reason:
        return {
            "code": "authentication_required",
            "message": reason,
            "category": "authentication",
            "recoverable_resume_failure": False,
            "backend_id": backend_id,
        }
    reason = _match(_PERMISSION_CONFIRMATION, bounded)
    if reason:
        return {
            "code": "permission_confirmation_required",
            "message": reason,
            "category": "authorization",
            "recoverable_resume_failure": False,
            "backend_id": backend_id,
        }
    reason = _match(_INVALID_CLI, bounded)
    if reason:
        return {
            "code": "invalid_cli_arguments",
            "message": reason,
            "category": "invocation",
            "recoverable_resume_failure": False,
            "backend_id": backend_id,
        }
    if returncode is not None and returncode != 0:
        return None
    return None


def classify_human_startup(backend_id: str, text: str) -> dict[str, Any] | None:
    failure = classify_startup_failure(backend_id, text)
    if failure is not None:
        return {"kind": "failed", "failure": failure}
    reason = _match(_WAITING, text[-65536:])
    if reason:
        return {"kind": "waiting", "reason": reason}
    return None


def _codex_session_state(session_id: str) -> tuple[str, str]:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    inspected = False
    database = home / "state_5.sqlite"
    if database.is_file():
        inspected = True
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                row = connection.execute(
                    "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
            finally:
                connection.close()
        except (sqlite3.Error, OSError):
            pass
        else:
            if row is not None:
                return "present", "Codex thread registry contains the session"

    for root in (home / "sessions", home / "archived_sessions"):
        if not root.is_dir():
            continue
        inspected = True
        try:
            if next(root.rglob(f"*{session_id}.jsonl"), None) is not None:
                return "present", f"Codex rollout exists under {root}"
        except OSError:
            return "unknown", f"Codex session storage could not be enumerated: {root}"
    if inspected:
        return "missing", "Codex session is absent from readable local storage"
    return "unknown", "Codex session storage is unavailable"


def _claude_session_state(session_id: str) -> tuple[str, str]:
    home = Path(
        os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    ).expanduser()
    projects = home / "projects"
    if not projects.is_dir():
        return "unknown", "Claude project session storage is unavailable"
    try:
        if next(projects.rglob(f"{session_id}.jsonl"), None) is not None:
            return "present", "Claude project transcript exists"
    except OSError:
        return "unknown", "Claude project session storage could not be enumerated"
    return "missing", "Claude session is absent from readable local storage"


def session_state(backend_id: str, session_id: str) -> tuple[str, str]:
    """Return present, missing, or unknown for a native backend session."""

    if not session_id:
        return "missing", "resume session id is empty"
    if not _UUID.fullmatch(session_id):
        return "unknown", "session id is not a UUID; local lookup is not authoritative"
    if backend_id == "codex":
        return _codex_session_state(session_id)
    if backend_id == "claude-code":
        return _claude_session_state(session_id)
    return "unknown", "backend has no deterministic local session lookup"
