#!/usr/bin/env python3
"""Deterministic process-group supervision shared by attempt and command runners."""

from __future__ import annotations

import os
import json
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Sequence


@dataclass(frozen=True)
class SupervisedResult:
    exit_code: int
    timed_out: bool
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
) -> SupervisedResult:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    started = time.monotonic()
    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd else None,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    observed_pids: set[int] = {process.pid}
    observed_pgids: set[int] = {process.pid}
    timed_out = False
    last_state_write = 0.0
    while process.poll() is None:
        try:
            table = _process_table()
            current = descendants(process.pid, table)
            observed_pids.update(current)
            observed_pgids.update(table[pid][1] for pid in current if pid in table)
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
        time.sleep(0.1)
    if timed_out:
        survivors = terminate_processes(observed_pgids, observed_pids, grace_seconds=grace_seconds)
        process.wait()
        exit_code = 124
    else:
        exit_code = int(process.wait())
        survivors = terminate_processes(observed_pgids, observed_pids - {process.pid}, grace_seconds=grace_seconds)
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
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return SupervisedResult(
        exit_code=exit_code,
        timed_out=timed_out,
        elapsed_seconds=round(time.monotonic() - started, 6),
        observed_pids=tuple(sorted(observed_pids)),
        observed_pgids=tuple(sorted(observed_pgids)),
        surviving_pids=survivors,
    )
