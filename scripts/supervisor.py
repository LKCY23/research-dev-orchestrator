#!/usr/bin/env python3
"""Deterministic process-group supervision shared by attempt and command runners."""

from __future__ import annotations

import os
import json
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, IO, Mapping, Sequence


SUPERVISION_TOKEN_ENV = "RDO_SUPERVISION_TOKEN"


@dataclass(frozen=True)
class SupervisedResult:
    exit_code: int
    child_exit_code: int
    timed_out: bool
    completion_requested: bool
    finalization_timed_out: bool
    elapsed_seconds: float
    observed_pids: tuple[int, ...]
    observed_pgids: tuple[int, ...]
    surviving_pids: tuple[int, ...]


def _process_table() -> dict[int, tuple[int, int]]:
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,pgid="], text=True)
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
    environment[SUPERVISION_TOKEN_ENV] = token
    return environment, token


def tagged_processes(token: str, table: dict[int, tuple[int, int]]) -> set[int]:
    """Find descendants that detached/reparented but retained our launch token."""

    needle = f"{SUPERVISION_TOKEN_ENV}={token}"
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
            if needle.encode("utf-8") in environment:
                tagged.add(pid)
        return tagged

    try:
        output = subprocess.check_output(
            ["ps", "eww", "-axo", "pid=,ppid=,pgid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return tagged
    for line in output.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4 or needle not in parts[3]:
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
) -> tuple[set[int], set[int]]:
    """Return only process identities that belong to the current descendant tree."""

    pids = descendants(root_pid, table)
    if supervision_token:
        pids.update(tagged_processes(supervision_token, table))
    pgids = {table[pid][1] for pid in pids if pid in table}
    pgids.add(root_pid)
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
        ).strip()
    except subprocess.CalledProcessError:
        return False
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


def terminate_processes(pgids: set[int], pids: set[int], *, grace_seconds: float = 2.0) -> tuple[int, ...]:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        _signal_groups(pgids, sig)
        _signal_pids(pids, sig)
        deadline = time.monotonic() + (0 if sig == signal.SIGKILL else grace_seconds)
        while time.monotonic() < deadline:
            if not any(pid_alive(pid) for pid in pids):
                return ()
            time.sleep(0.05)
    return tuple(sorted(pid for pid in pids if pid_alive(pid)))


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
    completion_requested: Callable[[], bool] | None = None,
    completion_grace_seconds: float = 0.5,
    finalization_started: Callable[[], bool] | None = None,
    finalization_timeout_seconds: float = 90.0,
) -> SupervisedResult:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    started = time.monotonic()
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
    timed_out = False
    completed_by_signal = False
    finalization_started_at: float | None = None
    finalization_timed_out = False
    last_state_write = 0.0
    while process.poll() is None:
        try:
            table = _process_table()
            current, current_pgids = current_termination_targets(
                process.pid,
                table,
                supervision_token,
            )
            observed_pids.update(current)
            observed_pgids.update(current_pgids)
            termination_pids = current
            termination_pgids = current_pgids
        except (OSError, subprocess.SubprocessError):
            pass
        if state_path and time.monotonic() - last_state_write >= 0.5:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "state": "running",
                        "worker_pid": process.pid,
                        "worker_pgid": process.pid,
                        "observed_pids": sorted(observed_pids),
                        "observed_pgids": sorted(observed_pgids),
                        "deadline_seconds": timeout_seconds,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            last_state_write = time.monotonic()
        if time.monotonic() - started >= timeout_seconds:
            timed_out = True
            break
        if completion_requested is not None and completion_requested():
            completed_by_signal = True
            deadline = time.monotonic() + max(0, completion_grace_seconds)
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            break
        if finalization_started is not None and finalization_started():
            if finalization_started_at is None:
                finalization_started_at = time.monotonic()
            elif time.monotonic() - finalization_started_at >= finalization_timeout_seconds:
                timed_out = True
                finalization_timed_out = True
                break
        time.sleep(0.1)
    try:
        table = _process_table()
        current, current_pgids = current_termination_targets(
            process.pid,
            table,
            supervision_token,
        )
        observed_pids.update(current)
        observed_pgids.update(current_pgids)
        termination_pids = current
        termination_pgids = current_pgids
    except (OSError, subprocess.SubprocessError):
        pass
    if timed_out:
        survivors = terminate_processes(termination_pgids, termination_pids, grace_seconds=grace_seconds)
        process.wait()
        exit_code = 124
    elif completed_by_signal:
        if process.poll() is None:
            survivors = terminate_processes(termination_pgids, termination_pids, grace_seconds=grace_seconds)
        else:
            survivors = terminate_processes(
                termination_pgids,
                termination_pids - {process.pid},
                grace_seconds=grace_seconds,
            )
        child_exit_code = int(process.wait())
        exit_code = 0
    else:
        exit_code = int(process.wait())
        survivors = terminate_processes(
            termination_pgids,
            termination_pids - {process.pid},
            grace_seconds=grace_seconds,
        )
    if timed_out:
        child_exit_code = int(process.returncode)
    elif not completed_by_signal:
        child_exit_code = exit_code
    if state_path:
        state_path.write_text(
            json.dumps(
                {
                    "state": "timed_out" if timed_out else "completed",
                    "worker_pid": process.pid,
                    "worker_pgid": process.pid,
                    "observed_pids": sorted(observed_pids),
                    "observed_pgids": sorted(observed_pgids),
                    "surviving_pids": list(survivors),
                    "exit_code": exit_code,
                    "child_exit_code": child_exit_code,
                    "completion_requested": completed_by_signal,
                    "finalization_timed_out": finalization_timed_out,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return SupervisedResult(
        exit_code=exit_code,
        child_exit_code=child_exit_code,
        timed_out=timed_out,
        completion_requested=completed_by_signal,
        finalization_timed_out=finalization_timed_out,
        elapsed_seconds=round(time.monotonic() - started, 6),
        observed_pids=tuple(sorted(observed_pids)),
        observed_pgids=tuple(sorted(observed_pgids)),
        surviving_pids=survivors,
    )
