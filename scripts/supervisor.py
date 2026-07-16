#!/usr/bin/env python3
"""Deterministic process-group supervision shared by attempt and command runners."""

from __future__ import annotations

import hashlib
import os
import json
import math
import re
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, IO, Mapping, Sequence


SUPERVISION_TOKEN_ENV = "RDO_SUPERVISION_TOKEN"
SUPERVISION_TOKEN_LINEAGE_ENV = "RDO_SUPERVISION_TOKEN_LINEAGE"
# Process enumeration is part of the cleanup proof, so a transiently slow
# macOS `ps` must not turn an otherwise clean handoff into exit 126. Keep the
# scan bounded, but leave enough headroom for a loaded host.
PROCESS_SCAN_TIMEOUT_SECONDS = 1.0
MAX_FINALIZATION_MARKER_BYTES = 64 * 1024
MAX_DEADLINE_BYTES = 64 * 1024
MAX_FINALIZATION_SNAPSHOT_BYTES = 64 * 1024 * 1024
_FINALIZATION_CACHE: dict[tuple[Any, ...], float | None] = {}


@dataclass(frozen=True)
class SupervisedResult:
    exit_code: int
    child_exit_code: int
    timed_out: bool
    timeout_phase: str | None
    completion_requested: bool
    finalization_started: bool
    finalization_timed_out: bool
    elapsed_seconds: float
    observed_pids: tuple[int, ...]
    observed_pgids: tuple[int, ...]
    surviving_pids: tuple[int, ...]
    cleanup_verified: bool
    cleanup_failure_reason: str | None
    active_deadline_epoch: float
    deadline_phase: str
    execution_deadline_epoch: float
    attempt_started_epoch: float
    deadline_sha256: str


def _iso_from_epoch(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _atomic_create_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Create one JSON file atomically, leaving an existing file untouched."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(payload), indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably replace one mutable JSON state file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(payload), indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _finite_number(payload: Mapping[str, Any], key: str, *, positive: bool) -> float:
    value = payload.get(key)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or (positive and float(value) <= 0)
    ):
        qualifier = "finite and positive" if positive else "finite"
        raise ValueError(f"attempt deadline {key} must be {qualifier}")
    return float(value)


def _iso_epoch(value: Any, *, key: str) -> float:
    if not isinstance(value, str):
        raise ValueError(f"attempt deadline {key} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"attempt deadline {key} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"attempt deadline {key} must include a timezone")
    return parsed.timestamp()


def validate_attempt_deadline_payload(
    payload: Mapping[str, Any],
    *,
    attempt_timeout_seconds: float | None = None,
    finalization_grace_seconds: float | None = None,
    reminder_seconds: float | None = None,
) -> dict[str, Any]:
    """Validate the immutable arithmetic and timestamps in DEADLINE.json."""

    if payload.get("schema_version") != 1:
        raise ValueError("attempt deadline has an unsupported schema")
    started = _finite_number(payload, "started_at_epoch", positive=True)
    wall = _finite_number(payload, "attempt_wall_seconds", positive=True)
    deadline = _finite_number(
        payload,
        "execution_deadline_at_epoch",
        positive=True,
    )
    grace = _finite_number(
        payload,
        "finalization_grace_seconds",
        positive=True,
    )
    reminder = _finite_number(payload, "reminder_seconds", positive=True)
    if not math.isclose(deadline, started + wall, rel_tol=0, abs_tol=1e-6):
        raise ValueError(
            "attempt deadline execution_deadline_at_epoch does not equal "
            "started_at_epoch + attempt_wall_seconds"
        )
    if not math.isclose(
        _iso_epoch(payload.get("started_at"), key="started_at"),
        started,
        rel_tol=0,
        abs_tol=0.0015,
    ):
        raise ValueError("attempt deadline started_at does not match started_at_epoch")
    if not math.isclose(
        _iso_epoch(
            payload.get("execution_deadline_at"),
            key="execution_deadline_at",
        ),
        deadline,
        rel_tol=0,
        abs_tol=0.0015,
    ):
        raise ValueError(
            "attempt deadline execution_deadline_at does not match "
            "execution_deadline_at_epoch"
        )
    for label, actual, expected in (
        ("attempt_wall_seconds", wall, attempt_timeout_seconds),
        ("finalization_grace_seconds", grace, finalization_grace_seconds),
        ("reminder_seconds", reminder, reminder_seconds),
    ):
        if expected is not None and not math.isclose(
            actual,
            expected,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError(f"attempt deadline {label} does not match dispatch")
    return dict(payload)


def load_or_create_attempt_deadline(
    path: Path | None,
    *,
    attempt_timeout_seconds: float,
    finalization_grace_seconds: float,
    reminder_seconds: float,
) -> dict[str, Any]:
    """Return one attempt-wide absolute deadline shared by resume fallbacks."""

    for label, value in (
        ("attempt timeout", attempt_timeout_seconds),
        ("finalization grace", finalization_grace_seconds),
        ("deadline reminder", reminder_seconds),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{label} must be finite and positive")
    if path is None:
        started_at_epoch = time.time()
        return {
            "schema_version": 1,
            "started_at": _iso_from_epoch(started_at_epoch),
            "started_at_epoch": started_at_epoch,
            "execution_deadline_at": _iso_from_epoch(
                started_at_epoch + attempt_timeout_seconds
            ),
            "execution_deadline_at_epoch": started_at_epoch
            + attempt_timeout_seconds,
            "attempt_wall_seconds": attempt_timeout_seconds,
            "finalization_grace_seconds": finalization_grace_seconds,
            "reminder_seconds": reminder_seconds,
        }

    if path.is_symlink():
        raise ValueError("attempt deadline path must not be a symlink")
    if path.exists() and not path.is_file():
        raise ValueError("attempt deadline path must be a regular file")
    if not path.exists():
        started_at_epoch = time.time()
        _atomic_create_json(
            path,
            {
                "schema_version": 1,
                "started_at": _iso_from_epoch(started_at_epoch),
                "started_at_epoch": started_at_epoch,
                "execution_deadline_at": _iso_from_epoch(
                    started_at_epoch + attempt_timeout_seconds
                ),
                "execution_deadline_at_epoch": started_at_epoch
                + attempt_timeout_seconds,
                "attempt_wall_seconds": attempt_timeout_seconds,
                "finalization_grace_seconds": finalization_grace_seconds,
                "reminder_seconds": reminder_seconds,
            },
        )
    if path.is_symlink() or not path.is_file():
        raise ValueError("attempt deadline path must be a regular non-symlink file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"attempt deadline is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("attempt deadline must be a JSON object")
    return validate_attempt_deadline_payload(
        payload,
        attempt_timeout_seconds=attempt_timeout_seconds,
        finalization_grace_seconds=finalization_grace_seconds,
        reminder_seconds=reminder_seconds,
    )


def attempt_deadline_sha256(
    path: Path | None,
    payload: Mapping[str, Any],
) -> str:
    """Hash the exact immutable deadline bytes, or canonical in-memory payload."""

    if path is not None:
        result = _stable_regular_bytes(path, max_bytes=MAX_DEADLINE_BYTES)
        if result is None:
            raise ValueError("attempt deadline changed or became unsafe")
        raw, _metadata = result
    else:
        raw = (
            json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _stable_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[bytes, os.stat_result] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_ctime_ns",
            "st_mtime_ns",
        )
        if (
            len(raw) > max_bytes
            or any(
                getattr(before, field) != getattr(after, field)
                for field in identity_fields
            )
        ):
            return None
        return raw, after
    finally:
        os.close(descriptor)


def _stat_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        return None
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_ctime_ns),
        int(metadata.st_mtime_ns),
    )


def finalization_epoch_from_path(
    path: Path,
    *,
    attempt_id: str = "",
    expected_grace_seconds: float | None = None,
    require_bound_snapshot: bool = False,
) -> float | None:
    """Return the trusted finalization-entry epoch, or None for no valid marker."""

    marker_result = _stable_regular_bytes(
        path,
        max_bytes=MAX_FINALIZATION_MARKER_BYTES,
    )
    if marker_result is None:
        return None
    marker_raw, marker_metadata = marker_result
    marker_ctime = float(marker_metadata.st_ctime)
    try:
        payload = json.loads(marker_raw)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("stage") != "finalizing":
        return None
    if attempt_id and payload.get("attempt_id") != attempt_id:
        return None
    grace = payload.get("grace_seconds", payload.get("deadline_seconds"))
    if expected_grace_seconds is not None and (
        not isinstance(grace, (int, float))
        or isinstance(grace, bool)
        or not math.isclose(
            float(grace),
            expected_grace_seconds,
            rel_tol=0,
            abs_tol=1e-6,
        )
    ):
        return None
    if require_bound_snapshot:
        if payload.get("schema_version") != 2:
            return None
        snapshot_ref = payload.get("source_snapshot_ref")
        snapshot_sha256 = payload.get("source_snapshot_sha256")
        if (
            snapshot_ref != "runtime/finalization-worktree.json"
            or not isinstance(snapshot_sha256, str)
        ):
            return None
        attempt = path.parent.parent
        snapshot = attempt / snapshot_ref
        deadline_ref = payload.get("deadline_ref")
        deadline_sha256 = payload.get("deadline_sha256")
        deadline = attempt / "runtime" / "DEADLINE.json"
        if (
            deadline_ref != "runtime/DEADLINE.json"
            or not isinstance(deadline_sha256, str)
        ):
            return None
        marker_identity = (
            int(marker_metadata.st_dev),
            int(marker_metadata.st_ino),
            int(marker_metadata.st_size),
            int(marker_metadata.st_ctime_ns),
            int(marker_metadata.st_mtime_ns),
        )
        snapshot_identity = _stat_identity(snapshot)
        deadline_identity = _stat_identity(deadline)
        if snapshot_identity is None or deadline_identity is None:
            return None
        cache_key = (
            str(path),
            marker_identity,
            snapshot_identity,
            deadline_identity,
            attempt_id,
            expected_grace_seconds,
            require_bound_snapshot,
        )
        if cache_key in _FINALIZATION_CACHE:
            return _FINALIZATION_CACHE[cache_key]
        snapshot_result = _stable_regular_bytes(
            snapshot,
            max_bytes=MAX_FINALIZATION_SNAPSHOT_BYTES,
        )
        deadline_result = _stable_regular_bytes(
            deadline,
            max_bytes=MAX_DEADLINE_BYTES,
        )
        if snapshot_result is None or deadline_result is None:
            return None
        snapshot_raw, snapshot_metadata = snapshot_result
        deadline_raw, deadline_metadata = deadline_result
        if (
            _stat_identity(snapshot) != snapshot_identity
            or _stat_identity(deadline) != deadline_identity
            or hashlib.sha256(snapshot_raw).hexdigest() != snapshot_sha256
            or hashlib.sha256(deadline_raw).hexdigest() != deadline_sha256
        ):
            return None
        snapshot_ctime = float(snapshot_metadata.st_ctime)
        try:
            deadline_payload = json.loads(deadline_raw)
            if not isinstance(deadline_payload, dict):
                return None
            deadline_payload = validate_attempt_deadline_payload(
                deadline_payload,
                finalization_grace_seconds=expected_grace_seconds,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError):
            return None
        started = payload.get("started_at_epoch")
        marker_deadline = payload.get("deadline_at_epoch")
        marker_started_iso = payload.get("started_at")
        try:
            marker_started_iso_epoch = _iso_epoch(
                marker_started_iso,
                key="FINALIZATION.started_at",
            )
        except ValueError:
            return None
        if (
            not isinstance(started, (int, float))
            or isinstance(started, bool)
            or not math.isfinite(float(started))
            or float(started)
            < float(deadline_payload["started_at_epoch"]) - 1e-6
            or marker_ctime
            < float(deadline_payload["started_at_epoch"]) - 1.001
            or snapshot_ctime
            < float(deadline_payload["started_at_epoch"]) - 1.001
            or snapshot_ctime > marker_ctime + 0.001
            or snapshot_ctime
            > float(deadline_payload["execution_deadline_at_epoch"]) + 0.001
            or marker_ctime
            > float(deadline_payload["execution_deadline_at_epoch"]) + 1e-6
            or marker_ctime > time.time() + 1.0
            or not math.isclose(
                float(started),
                marker_ctime,
                rel_tol=0,
                abs_tol=1.001,
            )
            or not math.isclose(
                marker_started_iso_epoch,
                float(started),
                rel_tol=0,
                abs_tol=1.001,
            )
            or not isinstance(marker_deadline, (int, float))
            or isinstance(marker_deadline, bool)
            or not math.isclose(
                float(marker_deadline),
                float(deadline_payload["execution_deadline_at_epoch"])
                + float(deadline_payload["finalization_grace_seconds"]),
                rel_tol=0,
                abs_tol=1e-6,
            )
        ):
            _FINALIZATION_CACHE[cache_key] = None
            return None
        _FINALIZATION_CACHE[cache_key] = marker_ctime
        if len(_FINALIZATION_CACHE) > 64:
            _FINALIZATION_CACHE.pop(next(iter(_FINALIZATION_CACHE)))
    if payload.get("schema_version") == 2:
        return marker_ctime
    value = payload.get("started_at_epoch")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        epoch = float(value)
        return epoch if math.isfinite(epoch) else None
    legacy = payload.get("started_at")
    if not isinstance(legacy, str):
        return None
    try:
        return datetime.fromisoformat(legacy.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class AttemptDeadline:
    attempt_started_epoch: float
    execution_deadline_epoch: float
    finalization_grace_seconds: float
    reminder_seconds: float
    execution_deadline_monotonic: float
    finalization_started_epoch: float | None = None
    finalization_deadline_epoch: float | None = None
    finalization_deadline_monotonic: float | None = None
    timeout_phase: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AttemptDeadline":
        now_epoch = time.time()
        now_monotonic = time.monotonic()
        execution_deadline_epoch = float(payload["execution_deadline_at_epoch"])
        return cls(
            attempt_started_epoch=float(payload["started_at_epoch"]),
            execution_deadline_epoch=execution_deadline_epoch,
            finalization_grace_seconds=float(
                payload["finalization_grace_seconds"]
            ),
            reminder_seconds=float(payload["reminder_seconds"]),
            execution_deadline_monotonic=now_monotonic
            + max(0.0, execution_deadline_epoch - now_epoch),
        )

    @property
    def phase(self) -> str:
        return "finalization" if self.finalization_started_epoch is not None else "execution"

    def active_deadline_monotonic(self) -> float:
        return (
            self.finalization_deadline_monotonic
            if self.finalization_started_epoch is not None
            and self.finalization_deadline_monotonic is not None
            else self.execution_deadline_monotonic
        )

    def active_deadline_epoch(self) -> float:
        return (
            self.finalization_deadline_epoch
            if self.finalization_started_epoch is not None
            and self.finalization_deadline_epoch is not None
            else self.execution_deadline_epoch
        )

    def expired(self, *, now_monotonic: float | None = None) -> bool:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return now >= self.active_deadline_monotonic()

    def observe(
        self,
        finalization_started_epoch: float | None,
        *,
        now_epoch: float | None = None,
        now_monotonic: float | None = None,
        enforce_timeout: bool = True,
    ) -> str | None:
        now_epoch = time.time() if now_epoch is None else now_epoch
        now_monotonic = (
            time.monotonic() if now_monotonic is None else now_monotonic
        )
        if (
            self.finalization_started_epoch is None
            and finalization_started_epoch is not None
            and finalization_started_epoch >= self.attempt_started_epoch - 1e-6
            and finalization_started_epoch <= self.execution_deadline_epoch
            and finalization_started_epoch <= now_epoch + 1.0
        ):
            self.finalization_started_epoch = finalization_started_epoch
            self.finalization_deadline_epoch = (
                self.execution_deadline_epoch + self.finalization_grace_seconds
            )
            self.finalization_deadline_monotonic = now_monotonic + max(
                0.0,
                self.finalization_deadline_epoch - now_epoch,
            )
        deadline = self.active_deadline_monotonic()
        if enforce_timeout and deadline is not None and now_monotonic >= deadline:
            self.timeout_phase = self.phase
        return self.timeout_phase

    def state(
        self,
        *,
        now_monotonic: float | None = None,
    ) -> dict[str, Any]:
        now_monotonic = (
            time.monotonic() if now_monotonic is None else now_monotonic
        )
        deadline_monotonic = (
            self.finalization_deadline_monotonic
            if self.phase == "finalization"
            else self.execution_deadline_monotonic
        )
        remaining = max(0.0, (deadline_monotonic or now_monotonic) - now_monotonic)
        reminder: dict[str, Any] | None = None
        if self.phase == "execution" and remaining <= self.reminder_seconds:
            reminder = {
                "code": "attempt_deadline_approaching",
                "remaining_seconds": round(remaining, 3),
                "required_action": (
                    "finish implementation and enter finalization, or publish blocked"
                ),
            }
        elif (
            self.phase == "finalization"
            and remaining
            <= self.finalization_grace_seconds + self.reminder_seconds
        ):
            reminder = {
                "code": "finalization_grace_active",
                "remaining_seconds": round(remaining, 3),
                "allowed_actions": [
                    "required rdo check records",
                    "git commit",
                    "handoff",
                    "rdo finalize",
                ],
                "forbidden_actions": [
                    "production file edits",
                    "workflow activity",
                    "rdo exec",
                    "implementation expansion",
                ],
            }
        return {
            "phase": self.phase,
            "execution_deadline_at_epoch": self.execution_deadline_epoch,
            "execution_deadline_at": _iso_from_epoch(
                self.execution_deadline_epoch
            ),
            "finalization_started_at_epoch": self.finalization_started_epoch,
            "finalization_deadline_at_epoch": self.finalization_deadline_epoch,
            "finalization_deadline_at": (
                _iso_from_epoch(self.finalization_deadline_epoch)
                if self.finalization_deadline_epoch is not None
                else None
            ),
            "remaining_seconds": round(remaining, 3),
            "reminder": reminder,
            "timeout_phase": self.timeout_phase,
        }


def _process_table() -> dict[int, tuple[int, int]]:
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,pgid="],
        text=True,
        timeout=PROCESS_SCAN_TIMEOUT_SECONDS,
    )
    table: dict[int, tuple[int, int]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid, pgid = (int(part) for part in parts)
        except ValueError:
            continue
        table[pid] = (ppid, pgid)
    return table


def descendants(root_pid: int, table: dict[int, tuple[int, int]] | None = None) -> set[int]:
    table = table or _process_table()
    found = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _pgid) in table.items():
            if ppid in found and pid not in found:
                found.add(pid)
                changed = True
    return found


def supervision_environment(
    base: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], str]:
    """Return a child environment carrying an inherited supervision token."""

    environment = dict(os.environ if base is None else base)
    token = uuid.uuid4().hex
    lineage = [
        item
        for item in environment.get(SUPERVISION_TOKEN_LINEAGE_ENV, "").split(":")
        if item
    ]
    inherited = environment.get(SUPERVISION_TOKEN_ENV, "")
    if inherited and inherited not in lineage:
        lineage.append(inherited)
    if token not in lineage:
        lineage.append(token)
    environment[SUPERVISION_TOKEN_ENV] = token
    environment[SUPERVISION_TOKEN_LINEAGE_ENV] = ":".join(lineage)
    return environment, token


def tagged_processes(
    token: str,
    table: dict[int, tuple[int, int]],
    *,
    cleanup_observation: dict[str, Any] | None = None,
) -> set[int]:
    """Find descendants that detached/reparented but retained our launch token."""

    needle = f"{SUPERVISION_TOKEN_ENV}={token}"
    lineage_prefix = f"{SUPERVISION_TOKEN_LINEAGE_ENV}="

    def environment_has_token(entries: Sequence[bytes]) -> bool:
        current = needle.encode("utf-8")
        prefix = lineage_prefix.encode("utf-8")
        token_bytes = token.encode("utf-8")
        for entry in entries:
            if entry == current:
                return True
            if entry.startswith(prefix) and token_bytes in entry[len(prefix) :].split(b":"):
                return True
        return False

    tagged: set[int] = set()
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid not in table:
                continue
            try:
                environment = (entry / "environ").read_bytes().split(b"\0")
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            if environment_has_token(environment):
                tagged.add(pid)
        return tagged

    try:
        output = subprocess.check_output(
            ["ps", "eww", "-axo", "pid=,ppid=,pgid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=PROCESS_SCAN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        if cleanup_observation is not None:
            cleanup_observation.update(
                verified=False,
                reason="supervision_token_scan_unavailable",
            )
        return tagged
    lineage_pattern = re.compile(
        rf"(?:^|\s){re.escape(SUPERVISION_TOKEN_LINEAGE_ENV)}=([0-9a-f:]+)(?:\s|$)"
    )
    for line in output.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4:
            continue
        command = parts[3]
        lineage_match = lineage_pattern.search(command)
        lineage_tokens = (
            lineage_match.group(1).split(":") if lineage_match is not None else []
        )
        if needle not in command and token not in lineage_tokens:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid in table:
            tagged.add(pid)
    return tagged


def current_termination_targets(
    root_pid: int,
    table: dict[int, tuple[int, int]],
    supervision_token: str | None = None,
    cleanup_observation: dict[str, Any] | None = None,
) -> tuple[set[int], set[int]]:
    """Return only process identities that belong to the current descendant tree."""

    pids = descendants(root_pid, table)
    if supervision_token:
        pids.update(
            tagged_processes(
                supervision_token,
                table,
                cleanup_observation=cleanup_observation,
            )
        )
    pgids = {table[pid][1] for pid in pids if pid in table}
    if root_pid in table:
        pgids.add(table[root_pid][1])
    return pids, pgids


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = subprocess.check_output(
            ["ps", "-o", "stat=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=PROCESS_SCAN_TIMEOUT_SECONDS,
        ).strip()
    except OSError:
        return True
    except subprocess.CalledProcessError:
        return False
    except subprocess.TimeoutExpired:
        return True
    return bool(state) and not state.startswith("Z")


def _signal_groups(pgids: set[int], sig: signal.Signals) -> None:
    own_pgid = os.getpgrp()
    for pgid in sorted(pgids):
        if pgid <= 1 or pgid == own_pgid:
            continue
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _signal_pids(pids: set[int], sig: signal.Signals) -> None:
    for pid in sorted(pids, reverse=True):
        if pid <= 1 or pid == os.getpid():
            continue
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def terminate_processes(
    pgids: set[int],
    pids: set[int],
    *,
    grace_seconds: float = 2.0,
    root_pid: int | None = None,
    supervision_token: str | None = None,
    observed_pids: set[int] | None = None,
    observed_pgids: set[int] | None = None,
    cleanup_observation: dict[str, Any] | None = None,
) -> tuple[int, ...]:
    """Terminate a process tree while rescanning for descendants created on signal."""

    emergency_pids = set(pids)
    emergency_pgids = set(pgids)
    try:
        _process_table()
    except (OSError, subprocess.SubprocessError):
        # Without process enumeration, graceful SIGINT is unsafe because a
        # handler can detach work that we cannot rediscover. Fail closed with
        # one TERM/KILL sequence against the known launch identities.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            _signal_groups(emergency_pgids, sig)
            _signal_pids(emergency_pids, sig)
            time.sleep(max(0.05, min(grace_seconds, 0.1)))
        remaining = tuple(sorted(pid for pid in emergency_pids if pid_alive(pid)))
        if cleanup_observation is not None:
            cleanup_observation.update(
                verified=False,
                reason="process_table_unavailable",
            )
        return remaining
    if cleanup_observation is not None:
        cleanup_observation.update(verified=True, reason=None)
    has_live_identity = root_pid is not None or bool(supervision_token)
    fallback_pids = set() if has_live_identity else set(pids)
    fallback_pgids = (
        {root_pid}
        if root_pid is not None
        else set()
        if has_live_identity
        else set(pgids)
    )

    def refresh(*, scan_token: bool = False) -> tuple[set[int], set[int]]:
        current_pids: set[int] = set()
        current_pgids: set[int] = set()
        if root_pid is not None or supervision_token:
            try:
                table = _process_table()
                if root_pid is not None:
                    current_pids, current_pgids = current_termination_targets(
                        root_pid,
                        table,
                        None,
                    )
                current_pids.update(
                    pid for pid in fallback_pids if pid in table and pid_alive(pid)
                )
                if scan_token and supervision_token:
                    tagged = tagged_processes(
                        supervision_token,
                        table,
                        cleanup_observation=cleanup_observation,
                    )
                    fallback_pids.update(tagged)
                    current_pids.update(tagged)
                candidate_pgids = current_pgids | fallback_pgids
                if candidate_pgids:
                    current_pids.update(
                        pid
                        for pid, (_ppid, pgid) in table.items()
                        if pgid in candidate_pgids
                    )
                current_pgids = {
                    table[pid][1] for pid in current_pids if pid in table
                }
            except (OSError, subprocess.SubprocessError):
                if cleanup_observation is not None:
                    cleanup_observation.update(
                        verified=False,
                        reason="process_table_unavailable_during_cleanup",
                    )
                current_pids = {
                    pid for pid in emergency_pids if pid_alive(pid)
                }
                current_pgids = set(emergency_pgids)
        else:
            current_pids = {pid for pid in fallback_pids if pid_alive(pid)}
            current_pgids = set(fallback_pgids)
        current_pids = {pid for pid in current_pids if pid_alive(pid)}
        if observed_pids is not None:
            observed_pids.update(current_pids)
        if observed_pgids is not None:
            observed_pgids.update(current_pgids)
        return current_pids, current_pgids

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        phase_seconds = max(
            0.15,
            0.0 if sig == signal.SIGKILL else grace_seconds,
        )
        phase_deadline = time.monotonic() + phase_seconds
        empty_since: float | None = None
        iteration = 0
        signaled_pids: set[int] = set()
        signaled_pgids: set[int] = set()
        while True:
            current_pids, current_pgids = refresh(scan_token=iteration > 0)
            iteration += 1
            if current_pids or current_pgids:
                empty_since = None
                new_pgids = current_pgids - signaled_pgids
                new_pids = current_pids - signaled_pids
                _signal_groups(new_pgids, sig)
                _signal_pids(new_pids, sig)
                signaled_pgids.update(new_pgids)
                signaled_pids.update(new_pids)
            elif empty_since is None:
                empty_since = time.monotonic()
            now = time.monotonic()
            if empty_since is not None and now - empty_since >= 0.1:
                return ()
            if now >= phase_deadline:
                break
            time.sleep(0.05)
    final_pids, _final_pgids = refresh(scan_token=True)
    return tuple(sorted(final_pids))


def reap_process(process: subprocess.Popen[Any], *, timeout_seconds: float = 2.0) -> int:
    """Bound parent reaping even when cleanup observations were incomplete."""

    try:
        return int(process.wait(timeout=timeout_seconds))
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return int(process.wait(timeout=timeout_seconds))


def run_supervised(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    cwd: Path | None = None,
    stdin: IO[bytes] | int | None = None,
    stdout: IO[bytes] | int | None = None,
    stderr: IO[bytes] | int | None = None,
    grace_seconds: float = 2.0,
    state_path: Path | None = None,
    completion_requested: Callable[[], bool | float | None] | None = None,
    completion_grace_seconds: float = 0.5,
    finalization_started: Callable[[], bool | float | None] | None = None,
    finalization_timeout_seconds: float = 90.0,
    deadline_path: Path | None = None,
    deadline_reminder_seconds: float = 60.0,
) -> SupervisedResult:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    started = time.monotonic()
    deadline_payload = load_or_create_attempt_deadline(
        deadline_path,
        attempt_timeout_seconds=timeout_seconds,
        finalization_grace_seconds=finalization_timeout_seconds,
        reminder_seconds=deadline_reminder_seconds,
    )
    deadline_sha256 = attempt_deadline_sha256(deadline_path, deadline_payload)
    deadline = AttemptDeadline.from_payload(deadline_payload)
    child_environment, supervision_token = supervision_environment()
    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd else None,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        env=child_environment,
    )
    observed_pids: set[int] = {process.pid}
    observed_pgids: set[int] = {process.pid}
    termination_pids: set[int] = {process.pid}
    termination_pgids: set[int] = {process.pid}
    timeout_phase: str | None = None
    completed_by_signal = False
    last_state_write = 0.0
    cleanup_observation: dict[str, Any] = {"verified": True, "reason": None}
    if state_path:
        atomic_write_json(
            state_path,
            {
                "state": "running",
                "worker_pid": process.pid,
                "worker_pgid": process.pid,
                "observed_pids": sorted(observed_pids),
                "observed_pgids": sorted(observed_pgids),
                "deadline_seconds": timeout_seconds,
                "supervision_token": supervision_token,
                "deadline_sha256": deadline_sha256,
                "deadline": deadline.state(),
            },
        )

    def observed_finalization_epoch() -> float | None:
        if finalization_started is None:
            return None
        value = finalization_started()
        if isinstance(value, bool):
            return time.time() if value else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            epoch = float(value)
            return epoch if math.isfinite(epoch) else None
        return None

    def completion_is_acceptable(*, initial: bool) -> bool:
        if completion_requested is None:
            return False
        value = completion_requested()
        deadline.observe(
            observed_finalization_epoch(),
            enforce_timeout=False,
        )
        if isinstance(value, bool):
            return value and (not initial or not deadline.expired())
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            epoch = float(value)
            return bool(
                math.isfinite(epoch)
                and epoch >= deadline.attempt_started_epoch - 0.001
                and epoch <= deadline.active_deadline_epoch() + 1e-6
            )
        return False

    while process.poll() is None:
        try:
            table = _process_table()
            current, current_pgids = current_termination_targets(
                process.pid,
                table,
                None,
            )
            observed_pids.update(current)
            observed_pgids.update(current_pgids)
            termination_pids = current
            termination_pgids = current_pgids
        except (OSError, subprocess.SubprocessError):
            pass
        finalization_epoch = observed_finalization_epoch()
        deadline.observe(finalization_epoch, enforce_timeout=False)
        completion_now = completion_is_acceptable(initial=True)
        timeout_phase = deadline.observe(
            finalization_epoch,
            enforce_timeout=not completion_now,
        )
        if state_path and time.monotonic() - last_state_write >= 0.5:
            atomic_write_json(
                state_path,
                {
                    "state": "running",
                    "worker_pid": process.pid,
                    "worker_pgid": process.pid,
                    "observed_pids": sorted(observed_pids),
                    "observed_pgids": sorted(observed_pgids),
                    "deadline_seconds": timeout_seconds,
                    "supervision_token": supervision_token,
                    "deadline_sha256": deadline_sha256,
                    "deadline": deadline.state(),
                },
            )
            last_state_write = time.monotonic()
        if completion_now:
            completed_by_signal = True
            completion_deadline = time.monotonic() + max(
                0,
                completion_grace_seconds,
            )
            while (
                process.poll() is None
                and time.monotonic() < completion_deadline
            ):
                if not completion_is_acceptable(initial=False):
                    completed_by_signal = False
                    break
                time.sleep(0.05)
            if completed_by_signal:
                break
            continue
        if timeout_phase is not None:
            break
        wait_seconds = max(
            0.0,
            min(
                0.05,
                deadline.active_deadline_monotonic() - time.monotonic(),
            ),
        )
        if wait_seconds <= 0:
            continue
        try:
            process.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            finalization_epoch = observed_finalization_epoch()
            deadline.observe(finalization_epoch, enforce_timeout=False)
            if deadline.expired():
                timeout_phase = deadline.observe(
                    finalization_epoch,
                    enforce_timeout=True,
                )
                break
    deadline.observe(
        observed_finalization_epoch(),
        enforce_timeout=process.poll() is None and not completed_by_signal,
    )
    timeout_phase = timeout_phase or deadline.timeout_phase
    timed_out = timeout_phase is not None
    finalization_timed_out = timeout_phase == "finalization"
    try:
        table = _process_table()
        current, current_pgids = current_termination_targets(
            process.pid,
            table,
            None,
        )
        observed_pids.update(current)
        observed_pgids.update(current_pgids)
        termination_pids = current
        termination_pgids = current_pgids
    except (OSError, subprocess.SubprocessError):
        pass
    if timed_out:
        survivors = terminate_processes(
            termination_pgids,
            termination_pids,
            grace_seconds=grace_seconds,
            root_pid=process.pid,
            supervision_token=supervision_token,
            observed_pids=observed_pids,
            observed_pgids=observed_pgids,
            cleanup_observation=cleanup_observation,
        )
        reap_process(process)
        exit_code = 124
    elif completed_by_signal:
        if process.poll() is None:
            survivors = terminate_processes(
                termination_pgids,
                termination_pids,
                grace_seconds=grace_seconds,
                root_pid=process.pid,
                supervision_token=supervision_token,
                observed_pids=observed_pids,
                observed_pgids=observed_pgids,
                cleanup_observation=cleanup_observation,
            )
        else:
            survivors = terminate_processes(
                termination_pgids,
                termination_pids - {process.pid},
                grace_seconds=grace_seconds,
                root_pid=process.pid,
                supervision_token=supervision_token,
                observed_pids=observed_pids,
                observed_pgids=observed_pgids,
                cleanup_observation=cleanup_observation,
            )
        child_exit_code = reap_process(process)
        exit_code = (
            0
            if not survivors and cleanup_observation["verified"]
            else 126
        )
    else:
        exit_code = reap_process(process)
        survivors = terminate_processes(
            termination_pgids,
            termination_pids - {process.pid},
            grace_seconds=grace_seconds,
            root_pid=process.pid,
            supervision_token=supervision_token,
            observed_pids=observed_pids,
            observed_pgids=observed_pgids,
            cleanup_observation=cleanup_observation,
        )
        if (survivors or not cleanup_observation["verified"]) and exit_code == 0:
            exit_code = 126
    if timed_out:
        child_exit_code = int(process.returncode)
    elif not completed_by_signal:
        child_exit_code = exit_code
    if state_path:
        atomic_write_json(
            state_path,
            {
                "state": (
                    "cleanup_failed"
                    if survivors or not cleanup_observation["verified"]
                    else "timed_out"
                    if timed_out
                    else "completed"
                ),
                "worker_pid": process.pid,
                "worker_pgid": process.pid,
                "observed_pids": sorted(observed_pids),
                "observed_pgids": sorted(observed_pgids),
                "surviving_pids": list(survivors),
                "cleanup_verified": cleanup_observation["verified"],
                "cleanup_failure_reason": cleanup_observation["reason"],
                "exit_code": exit_code,
                "child_exit_code": child_exit_code,
                "completion_requested": completed_by_signal,
                "timeout_phase": timeout_phase,
                "finalization_started": deadline.finalization_started_epoch
                is not None,
                "finalization_timed_out": finalization_timed_out,
                "supervision_token": supervision_token,
                "deadline_sha256": deadline_sha256,
                "deadline": deadline.state(),
            },
        )
    return SupervisedResult(
        exit_code=exit_code,
        child_exit_code=child_exit_code,
        timed_out=timed_out,
        timeout_phase=timeout_phase,
        completion_requested=completed_by_signal,
        finalization_started=deadline.finalization_started_epoch is not None,
        finalization_timed_out=finalization_timed_out,
        elapsed_seconds=round(time.monotonic() - started, 6),
        observed_pids=tuple(sorted(observed_pids)),
        observed_pgids=tuple(sorted(observed_pgids)),
        surviving_pids=survivors,
        cleanup_verified=bool(cleanup_observation["verified"]),
        cleanup_failure_reason=(
            str(cleanup_observation["reason"])
            if cleanup_observation["reason"] is not None
            else None
        ),
        active_deadline_epoch=deadline.active_deadline_epoch(),
        deadline_phase=deadline.phase,
        execution_deadline_epoch=deadline.execution_deadline_epoch,
        attempt_started_epoch=deadline.attempt_started_epoch,
        deadline_sha256=deadline_sha256,
    )
