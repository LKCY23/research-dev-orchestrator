#!/usr/bin/env python3
"""Attempt-local broker for acceptance-command process supervision.

The worker still launches the frozen acceptance command inside its backend
sandbox.  The outer machine-attempt supervisor owns process observation and
cleanup, then returns a receipt to the worker-side ``rdo check`` caller.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, IO, Sequence

from supervisor import (
    SUPERVISION_TOKEN_ENV,
    SUPERVISION_TOKEN_LINEAGE_ENV,
    _process_table,
    atomic_write_json,
    current_termination_targets,
    descendants,
    terminate_processes,
)


BROKER_DIR_ENV = "RDO_CHECK_BROKER_DIR"
BROKER_ATTEMPT_ENV = "RDO_CHECK_BROKER_ATTEMPT_ID"
BROKER_INSTANCE_ENV = "RDO_CHECK_BROKER_INSTANCE_ID"
SCHEMA_VERSION = 1
LEASE_WAIT_SECONDS = 5.0
CLEANUP_WAIT_SECONDS = 15.0
START_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class BrokeredCommandResult:
    exit_code: int
    child_exit_code: int
    timed_out: bool
    elapsed_seconds: float
    surviving_pids: tuple[int, ...]
    cleanup_verified: bool
    cleanup_failure_reason: str | None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _token_environment(token: str) -> dict[str, str]:
    environment = dict(os.environ)
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
    return environment


def broker_directory_for_attempt(attempt: Path) -> Path | None:
    """Return the authenticated broker directory inherited by this attempt."""

    raw = os.environ.get(BROKER_DIR_ENV, "")
    if not raw:
        return None
    if os.environ.get(BROKER_ATTEMPT_ENV) != attempt.name:
        raise RuntimeError("acceptance supervision broker attempt identity mismatch")
    broker = Path(raw).resolve(strict=False)
    expected_parent = (attempt / "runtime" / "check-broker").resolve(strict=False)
    if broker.parent != expected_parent:
        raise RuntimeError("acceptance supervision broker path escapes the attempt runtime")
    metadata = _read_json(broker / "BROKER.json")
    if (
        metadata is None
        or metadata.get("schema_version") != SCHEMA_VERSION
        or metadata.get("attempt_id") != attempt.name
        or metadata.get("instance_id") != os.environ.get(BROKER_INSTANCE_ENV)
    ):
        raise RuntimeError("acceptance supervision broker metadata is invalid")
    return broker


def _failed_result(reason: str, *, elapsed_seconds: float = 0.0) -> BrokeredCommandResult:
    return BrokeredCommandResult(
        exit_code=126,
        child_exit_code=126,
        timed_out=False,
        elapsed_seconds=round(elapsed_seconds, 6),
        surviving_pids=(),
        cleanup_verified=False,
        cleanup_failure_reason=reason,
    )


def run_brokered(
    broker: Path,
    *,
    attempt_id: str,
    task_id: str,
    task_inputs_sha256: str,
    check_id: str,
    argv: Sequence[str],
    timeout_seconds: float,
    cwd: Path,
    stdin: IO[bytes] | int | None,
    stdout: IO[bytes] | int | None,
    stderr: IO[bytes] | int | None,
) -> BrokeredCommandResult:
    """Launch in the caller sandbox and wait for an outer cleanup receipt."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    started = time.monotonic()
    request_id = f"Q-{uuid.uuid4().hex}"
    request = broker / request_id
    request.mkdir(mode=0o700)
    instance_id = os.environ.get(BROKER_INSTANCE_ENV, "")
    atomic_write_json(
        request / "REQUEST.json",
        {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "attempt_id": attempt_id,
            "task_id": task_id,
            "task_inputs_sha256": task_inputs_sha256,
            "check_id": check_id,
            "timeout_seconds": timeout_seconds,
            "instance_id": instance_id,
            "requested_at_epoch": time.time(),
        },
    )

    lease_deadline = time.monotonic() + LEASE_WAIT_SECONDS
    lease: dict[str, Any] | None = None
    while time.monotonic() < lease_deadline:
        lease = _read_json(request / "LEASE.json")
        if lease is not None:
            break
        time.sleep(0.02)
    if (
        lease is None
        or lease.get("schema_version") != SCHEMA_VERSION
        or lease.get("request_id") != request_id
        or lease.get("instance_id") != instance_id
        or not isinstance(lease.get("supervision_token"), str)
        or not lease["supervision_token"]
    ):
        return _failed_result(
            "acceptance_supervision_lease_unavailable",
            elapsed_seconds=time.monotonic() - started,
        )

    token = str(lease["supervision_token"])
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=_token_environment(token),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    except OSError as exc:
        atomic_write_json(
            request / "FINISHED.json",
            {
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "launch_failed": True,
                "error": str(exc),
                "finished_at_epoch": time.time(),
            },
        )
        return BrokeredCommandResult(
            exit_code=127,
            child_exit_code=127,
            timed_out=False,
            elapsed_seconds=round(time.monotonic() - started, 6),
            surviving_pids=(),
            cleanup_verified=True,
            cleanup_failure_reason=None,
        )

    launched = time.monotonic()
    atomic_write_json(
        request / "STARTED.json",
        {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "root_pid": process.pid,
            "root_pgid": process.pid,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "started_at_epoch": time.time(),
        },
    )
    timed_out = False
    while process.poll() is None:
        if time.monotonic() - launched >= timeout_seconds:
            timed_out = True
            break
        time.sleep(0.02)
    child_exit_code = int(process.returncode) if process.returncode is not None else 124
    elapsed = time.monotonic() - launched
    atomic_write_json(
        request / "FINISHED.json",
        {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "child_exit_code": (
                int(process.returncode) if process.returncode is not None else None
            ),
            "timed_out": timed_out,
            "elapsed_seconds": round(elapsed, 6),
            "finished_at_epoch": time.time(),
        },
    )

    cleanup_deadline = time.monotonic() + CLEANUP_WAIT_SECONDS
    cleanup: dict[str, Any] | None = None
    while time.monotonic() < cleanup_deadline:
        cleanup = _read_json(request / "CLEANUP.json")
        if cleanup is not None:
            break
        time.sleep(0.02)
    if cleanup is None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
            break
        return _failed_result(
            "acceptance_supervision_cleanup_receipt_unavailable",
            elapsed_seconds=elapsed,
        )

    try:
        surviving_pids = tuple(int(pid) for pid in cleanup.get("surviving_pids", []))
    except (TypeError, ValueError):
        return _failed_result("acceptance_supervision_cleanup_receipt_invalid")
    cleanup_verified = cleanup.get("cleanup_verified") is True and not surviving_pids
    cleanup_reason = cleanup.get("cleanup_failure_reason")
    if cleanup_reason is not None and not isinstance(cleanup_reason, str):
        cleanup_verified = False
        cleanup_reason = "acceptance_supervision_cleanup_receipt_invalid"
    if process.poll() is None:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            cleanup_verified = False
            cleanup_reason = "acceptance_command_root_survived_cleanup"
    if timed_out:
        exit_code = 124
    else:
        child_exit_code = int(process.returncode) if process.returncode is not None else child_exit_code
        exit_code = child_exit_code
        if exit_code == 0 and not cleanup_verified:
            exit_code = 126
    return BrokeredCommandResult(
        exit_code=exit_code,
        child_exit_code=child_exit_code,
        timed_out=timed_out,
        elapsed_seconds=round(elapsed, 6),
        surviving_pids=surviving_pids,
        cleanup_verified=cleanup_verified,
        cleanup_failure_reason=(
            str(cleanup_reason)
            if cleanup_reason is not None
            else None
            if cleanup_verified
            else "acceptance_supervision_cleanup_unverified"
        ),
    )


class CheckBrokerServer:
    """Outer-supervisor side of one attempt-local supervision broker."""

    def __init__(self, runtime: Path, attempt_id: str):
        self.attempt_id = attempt_id
        self.instance_id = uuid.uuid4().hex
        self.directory = (
            runtime / "check-broker" / self.instance_id
        ).resolve(strict=False)
        self.directory.mkdir(parents=True, mode=0o700)
        atomic_write_json(
            self.directory / "BROKER.json",
            {
                "schema_version": SCHEMA_VERSION,
                "attempt_id": attempt_id,
                "instance_id": self.instance_id,
                "created_at_epoch": time.time(),
            },
        )
        self._leases: dict[str, dict[str, Any]] = {}

    def environment(self) -> dict[str, str]:
        return {
            BROKER_DIR_ENV: str(self.directory),
            BROKER_ATTEMPT_ENV: self.attempt_id,
            BROKER_INSTANCE_ENV: self.instance_id,
        }

    def _cleanup(
        self,
        request_id: str,
        state: dict[str, Any],
        *,
        worker_pid: int,
        table: dict[int, tuple[int, int]] | None,
        forced_reason: str | None = None,
    ) -> None:
        request = self.directory / request_id
        if (request / "CLEANUP.json").exists():
            return
        token = str(state["token"])
        root_pid = state.get("validated_root_pid")
        finished = _read_json(request / "FINISHED.json")
        if (
            finished is not None
            and isinstance(finished.get("child_exit_code"), int)
            and finished.get("timed_out") is not True
        ):
            # The launch PID has already exited.  Use only the unforgeable
            # lease token to find detached descendants; do not risk a reused
            # PID becoming a cleanup authority.
            root_pid = None
        observed_pids: set[int] = set(state.get("observed_pids", set()))
        observed_pgids: set[int] = set(state.get("observed_pgids", set()))
        pids: set[int] = set()
        pgids: set[int] = set()
        cleanup_observation: dict[str, Any] = {"verified": True, "reason": None}
        try:
            current_table = table if table is not None else _process_table()
            if isinstance(root_pid, int):
                pids, pgids = current_termination_targets(root_pid, current_table, None)
                observed_pids.update(pids)
                observed_pgids.update(pgids)
        except (OSError, subprocess.SubprocessError):
            cleanup_observation.update(verified=False, reason="process_table_unavailable")
        survivors = terminate_processes(
            pgids,
            pids,
            root_pid=root_pid if isinstance(root_pid, int) else None,
            supervision_token=token,
            observed_pids=observed_pids,
            observed_pgids=observed_pgids,
            cleanup_observation=cleanup_observation,
        )
        reason = cleanup_observation.get("reason")
        verified = bool(cleanup_observation.get("verified")) and not survivors
        if forced_reason and not verified and reason is None:
            reason = forced_reason
        atomic_write_json(
            request / "CLEANUP.json",
            {
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "cleanup_verified": verified,
                "cleanup_failure_reason": reason,
                "surviving_pids": list(survivors),
                "observed_pids": sorted(observed_pids),
                "observed_pgids": sorted(observed_pgids),
                "cleaned_at_epoch": time.time(),
            },
        )
        state["cleaned"] = True

    def poll(
        self,
        worker_pid: int,
        table: dict[int, tuple[int, int]] | None = None,
    ) -> None:
        try:
            request_paths = list(self.directory.glob("Q-*/REQUEST.json"))
        except OSError:
            return
        now = time.time()
        for request_path in request_paths:
            request_directory = request_path.parent
            if (
                request_directory.is_symlink()
                or request_directory.resolve(strict=False).parent != self.directory
            ):
                continue
            request_id = request_path.parent.name
            payload = _read_json(request_path)
            if payload is None:
                continue
            state = self._leases.get(request_id)
            if state is None:
                timeout = payload.get("timeout_seconds")
                if (
                    payload.get("schema_version") != SCHEMA_VERSION
                    or payload.get("request_id") != request_id
                    or payload.get("attempt_id") != self.attempt_id
                    or payload.get("instance_id") != self.instance_id
                    or not isinstance(timeout, (int, float))
                    or isinstance(timeout, bool)
                    or not math.isfinite(float(timeout))
                    or float(timeout) <= 0
                ):
                    continue
                token = uuid.uuid4().hex
                state = {
                    "token": token,
                    "timeout_seconds": float(timeout),
                    "leased_at_epoch": now,
                    "observed_pids": set(),
                    "observed_pgids": set(),
                    "cleaned": False,
                }
                self._leases[request_id] = state
                atomic_write_json(
                    request_path.parent / "LEASE.json",
                    {
                        "schema_version": SCHEMA_VERSION,
                        "request_id": request_id,
                        "instance_id": self.instance_id,
                        "supervision_token": token,
                        "leased_at_epoch": now,
                    },
                )
            if state.get("cleaned"):
                continue

            started = _read_json(request_path.parent / "STARTED.json")
            if started is not None:
                root_pid = started.get("root_pid")
                expected_token_sha = hashlib.sha256(str(state["token"]).encode()).hexdigest()
                if (
                    isinstance(root_pid, int)
                    and root_pid > 1
                    and started.get("root_pgid") == root_pid
                    and started.get("token_sha256") == expected_token_sha
                ):
                    try:
                        current_table = table if table is not None else _process_table()
                        worker_tree = descendants(worker_pid, current_table)
                        if root_pid in worker_tree:
                            state["validated_root_pid"] = root_pid
                            current, current_pgids = current_termination_targets(
                                root_pid, current_table, None
                            )
                            state["observed_pids"].update(current)
                            state["observed_pgids"].update(current_pgids)
                    except (OSError, subprocess.SubprocessError):
                        pass
                    state.setdefault("started_at_epoch", now)

            finished = _read_json(request_path.parent / "FINISHED.json")
            started_epoch = state.get("started_at_epoch")
            server_timed_out = bool(
                isinstance(started_epoch, float)
                and now >= started_epoch + state["timeout_seconds"] + 1.0
            )
            start_missing = now >= state["leased_at_epoch"] + START_WAIT_SECONDS
            if finished is not None or server_timed_out or start_missing:
                self._cleanup(
                    request_id,
                    state,
                    worker_pid=worker_pid,
                    table=table,
                    forced_reason=(
                        "acceptance_command_server_timeout"
                        if server_timed_out
                        else "acceptance_command_start_missing"
                        if start_missing and finished is None
                        else None
                    ),
                )

    def close(self, worker_pid: int, *, reason: str) -> None:
        try:
            table = _process_table()
        except (OSError, subprocess.SubprocessError):
            table = None
        self.poll(worker_pid, table)
        for request_id, state in list(self._leases.items()):
            if not state.get("cleaned"):
                self._cleanup(
                    request_id,
                    state,
                    worker_pid=worker_pid,
                    table=table,
                    forced_reason=reason,
                )
