#!/usr/bin/env python3
"""Run one non-interactive worker with deterministic prompt and startup handling."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any

from backend_startup import classify_startup_failure
from completion import (
    inspect_candidate_source_head,
    inspect_candidate_source_state,
    inspect_publication_candidate,
    publication_path as expected_publication_path,
    validate_publication,
    v2_publication_dependency_latest_ctime,
)
from protocol import utc_now
from supervisor import (
    AttemptDeadline,
    _process_table,
    attempt_deadline_sha256,
    current_termination_targets,
    finalization_epoch_from_path,
    load_or_create_attempt_deadline,
    reap_process,
    supervision_environment,
    terminate_processes,
)
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


def worker_progress_event(backend: str, line: bytes) -> str | None:
    """Return evidence that the backend progressed beyond session allocation."""

    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")
    if backend == "codex":
        if event_type in {"turn.completed", "turn_completed"}:
            return str(event_type)
        if event_type in {"item.started", "item.completed"}:
            item = payload.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            if isinstance(item_type, str) and item_type and item_type != "error":
                return f"{event_type}:{item_type}"
        return None
    if backend == "claude-code" and event_type == "assistant":
        return "assistant"
    return startup_event(backend, line)


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
    parser.add_argument("--artifact-protocol-version", choices=(1, 2), type=int, default=1)
    parser.add_argument("--publication-path", "--completion-path", dest="publication_path", default="")
    parser.add_argument("--task-dir", default="")
    parser.add_argument("--attempt-id", default="")
    parser.add_argument("--handoff-grace-seconds", type=float, default=0.5)
    parser.add_argument("--codex-terminal-event-grace-seconds", type=float, default=5.0)
    parser.add_argument("--finalization-path", default="")
    parser.add_argument("--finalization-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--deadline-path", default="")
    parser.add_argument("--deadline-reminder-seconds", type=float, default=60.0)
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
    if not math.isfinite(args.handoff_grace_seconds) or args.handoff_grace_seconds < 0:
        raise SystemExit("handoff grace seconds must be finite and non-negative")
    if (
        not math.isfinite(args.codex_terminal_event_grace_seconds)
        or args.codex_terminal_event_grace_seconds < 0
        or args.codex_terminal_event_grace_seconds > 30
    ):
        raise SystemExit("Codex terminal event grace seconds must be between 0 and 30")
    if (
        not math.isfinite(args.finalization_timeout_seconds)
        or args.finalization_timeout_seconds <= 0
    ):
        raise SystemExit("finalization timeout seconds must be finite and positive")
    if (
        not math.isfinite(args.deadline_reminder_seconds)
        or args.deadline_reminder_seconds <= 0
    ):
        raise SystemExit("deadline reminder seconds must be finite and positive")

    monitor_publication = bool(args.publication_path or args.task_dir or args.attempt_id)
    if args.artifact_protocol_version == 2:
        monitor_publication = True
    publication_path: Path | None = None
    if monitor_publication:
        if not args.task_dir or not args.attempt_id:
            raise SystemExit("publication monitoring requires --task-dir and --attempt-id")
        expected_path = expected_publication_path(
            Path(args.task_dir),
            args.attempt_id,
            args.artifact_protocol_version,
        )
        publication_path = Path(args.publication_path) if args.publication_path else expected_path
        if publication_path.resolve(strict=False) != expected_path.resolve(strict=False):
            raise SystemExit(
                "publication path must be the protocol-specific path for the supervised attempt"
            )
    publication_state: dict[str, Any] = {
        "artifact_protocol_version": args.artifact_protocol_version,
        "path": str(publication_path) if publication_path is not None else None,
        "valid": False,
        "reasons": [],
        "payload": None,
        "sha256": None,
    }
    accepted_publication_sha256: str | None = None
    accepted_publication_epoch: float | None = None
    accepted_publication_marker_ctime: float | None = None
    accepted_publication_receipt: dict[str, Any] | None = None
    late_publication_seen = False

    def publication_is_valid() -> bool:
        nonlocal accepted_publication_epoch
        nonlocal accepted_publication_marker_ctime
        nonlocal accepted_publication_receipt
        nonlocal accepted_publication_sha256
        nonlocal late_publication_seen
        if publication_path is None or not publication_path.exists():
            if publication_path is not None:
                publication_state.update(
                    valid=False,
                    reasons=["publication marker is missing"],
                    payload=None,
                )
            return False
        result = inspect_publication_candidate(
            publication_path,
            artifact_protocol_version=args.artifact_protocol_version,
            task_dir=Path(args.task_dir),
            attempt_id=args.attempt_id,
        )
        publication_state.update(
            valid=result.valid,
            reasons=list(result.reasons),
            payload=result.payload,
        )
        if not result.valid:
            return False
        receipt = result.receipt
        payload = result.payload
        if not isinstance(receipt, dict) or not isinstance(payload, dict):
            return False
        publication_sha256 = receipt.get("sha256")
        publication_epoch_value = receipt.get("ctime")
        if not isinstance(publication_sha256, str) or not isinstance(
            publication_epoch_value,
            (int, float),
        ):
            return False
        marker_ctime = float(publication_epoch_value)
        active_deadline_epoch = deadline.active_deadline_epoch()
        if accepted_publication_receipt is None:
            if marker_ctime < deadline.attempt_started_epoch - 0.001:
                publication_state.update(
                    valid=False,
                    reasons=["publication marker predates the supervised attempt"],
                )
                return False
            remaining = active_deadline_epoch - time.time()
            if marker_ctime > active_deadline_epoch + 0.001 or remaining <= 0:
                late_publication_seen = True
                publication_state.update(
                    valid=False,
                    reasons=["publication was first observed after the active deadline"],
                )
                return False
            source_state = inspect_candidate_source_state(
                Path(args.cwd),
                payload,
                timeout_seconds=max(0.01, min(1.0, remaining)),
            )
        else:
            source_state = inspect_candidate_source_head(
                Path(args.cwd),
                payload,
            )
        if not source_state.valid:
            publication_state.update(
                valid=False,
                reasons=list(source_state.reasons),
            )
            return False
        observed_at_epoch = time.time()
        current_receipt = {
            **receipt,
            "observed_at_epoch": observed_at_epoch,
            "source": source_state.receipt or {},
        }
        if accepted_publication_receipt is None:
            if observed_at_epoch > active_deadline_epoch + 1e-6:
                late_publication_seen = True
                publication_state.update(
                    valid=False,
                    reasons=["publication source proof completed after the active deadline"],
                )
                return False
            accepted_publication_receipt = current_receipt
            accepted_publication_sha256 = publication_sha256
            accepted_publication_marker_ctime = marker_ctime
            accepted_publication_epoch = observed_at_epoch
        elif any(
            current_receipt.get(field) != accepted_publication_receipt.get(field)
            for field in (
                "sha256",
                "mtime_ns",
                "device",
                "inode",
                "size",
            )
        ) or (
            (current_receipt.get("source") or {}).get("source_commit")
            != (accepted_publication_receipt.get("source") or {}).get("source_commit")
        ):
            publication_state.update(
                valid=False,
                reasons=["accepted publication identity changed during shutdown"],
                sha256=publication_sha256,
            )
            return False
        publication_state["sha256"] = publication_sha256
        publication_state["receipt"] = accepted_publication_receipt
        return True

    def publication_epoch() -> float | None:
        nonlocal accepted_publication_epoch
        if accepted_publication_epoch is not None:
            return accepted_publication_epoch
        return None

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
    usage_runtime = (
        transcript_path.parent
        if args.artifact_protocol_version == 2
        else transcript_path.parent / "runtime"
    )
    usage = UsageSupervisor(usage_runtime, args.backend, resource_budget)
    requested_session_id = args.existing_session_id
    observed_session_id = ""
    env = os.environ.copy()
    env.update(environment)
    env, supervision_token = supervision_environment(env)
    stdin_spec: int = subprocess.DEVNULL if args.prompt_transport == "arg" else subprocess.PIPE
    deadline_payload = load_or_create_attempt_deadline(
        Path(args.deadline_path) if args.deadline_path else None,
        attempt_timeout_seconds=args.timeout_seconds,
        finalization_grace_seconds=args.finalization_timeout_seconds,
        reminder_seconds=args.deadline_reminder_seconds,
    )
    deadline_sha256 = attempt_deadline_sha256(
        Path(args.deadline_path) if args.deadline_path else None,
        deadline_payload,
    )
    deadline = AttemptDeadline.from_payload(deadline_payload)
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
    atomic_json(state_path, {
        "state": "running",
        "worker_pid": process.pid,
        "worker_pgid": process.pid,
        "observed_pids": [process.pid],
        "observed_pgids": [process.pid],
        "deadline_seconds": args.timeout_seconds,
        "startup_state": "process_started",
        "publication_requested": False,
        "supervision_token": supervision_token,
        "deadline_sha256": deadline_sha256,
        "deadline": deadline.state(),
    })
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
        "worker_progress_at": None,
        "worker_progress_evidence": None,
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
        startup["worker_progress_at"] = startup["worker_started_at"]
        startup["worker_progress_evidence"] = {
            "decoder": "custom-process",
            "event": "process_started",
        }
        atomic_json(startup_path, startup)

    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = b""
    startup_output = bytearray()
    observed_pids = {process.pid}
    observed_pgids = {process.pid}
    termination_pids = {process.pid}
    termination_pgids = {process.pid}
    timed_out = False
    timeout_phase: str | None = None
    startup_failed = False
    budget_exceeded = False
    worker_progress_observed = args.custom_command
    publication_requested = False
    publication_was_accepted = False
    publication_deadline: float | None = None
    codex_terminal_event_deadline: float | None = None
    codex_terminal_event_wait_started: float | None = None
    codex_terminal_event_wait_elapsed: float | None = None
    last_state_write = 0.0
    cleanup_observation: dict[str, Any] = {"verified": True, "reason": None}
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    with transcript_path.open("wb") as transcript:
        while process.poll() is None:
            select_timeout = max(
                0.0,
                min(
                    0.1,
                    deadline.active_deadline_monotonic() - time.monotonic(),
                ),
            )
            for key, _ in selector.select(timeout=select_timeout):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                transcript.write(chunk)
                transcript.flush()
                os.write(1, chunk)
                startup_output.extend(chunk)
                if len(startup_output) > 65536:
                    del startup_output[:-65536]
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
                    progress = worker_progress_event(args.backend, line)
                    candidate_session_id = session_id_from_event(args.backend, line)
                    if candidate_session_id and not observed_session_id:
                        observed_session_id = candidate_session_id
                        if session_path is not None:
                            atomic_json(session_path, {"backend_id": args.backend, "session_id": observed_session_id})
                    if evidence and startup["state"] != "worker_started":
                        if not observed_session_id and requested_session_id:
                            observed_session_id = requested_session_id
                            if session_path is not None:
                                atomic_json(
                                    session_path,
                                    {
                                        "backend_id": args.backend,
                                        "session_id": observed_session_id,
                                    },
                                )
                        startup["state"] = "worker_started"
                        startup["worker_started_at"] = utc_now()
                        startup["startup_evidence"] = {"decoder": args.backend, "event": evidence}
                        atomic_json(startup_path, startup)
                    if progress and not worker_progress_observed:
                        worker_progress_observed = True
                        startup["worker_progress_at"] = utc_now()
                        startup["worker_progress_evidence"] = {
                            "decoder": args.backend,
                            "event": progress,
                        }
                        atomic_json(startup_path, startup)
                if budget_exceeded:
                    break
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
            elapsed = time.monotonic() - started_monotonic
            finalization_epoch = (
                finalization_epoch_from_path(
                    Path(args.finalization_path),
                    attempt_id=args.attempt_id,
                    expected_grace_seconds=args.finalization_timeout_seconds,
                    require_bound_snapshot=args.artifact_protocol_version == 2,
                )
                if args.finalization_path
                else None
            )
            deadline.observe(finalization_epoch, enforce_timeout=False)
            if elapsed - last_state_write >= 0.5:
                atomic_json(state_path, {
                    "state": "running",
                    "worker_pid": process.pid,
                    "worker_pgid": process.pid,
                    "observed_pids": sorted(observed_pids),
                    "observed_pgids": sorted(observed_pgids),
                    "deadline_seconds": args.timeout_seconds,
                    "startup_state": startup["state"],
                    "publication_requested": publication_requested,
                    "supervision_token": supervision_token,
                    "deadline_sha256": deadline_sha256,
                    "deadline": deadline.state(),
                })
                last_state_write = elapsed
            if startup["state"] != "worker_started" and elapsed >= args.startup_timeout_seconds:
                startup_failed = True
                startup["state"] = "worker_startup_failed"
                startup["failure"] = classify_startup_failure(
                    args.backend,
                    startup_output.decode("utf-8", errors="replace"),
                    include_model_unavailable=args.backend == "codex",
                ) or {
                    "code": "startup_timeout",
                    "message": "no valid backend startup event",
                    "category": "startup",
                    "recoverable_resume_failure": False,
                    "backend_id": args.backend,
                }
                atomic_json(startup_path, startup)
                break
            if usage.check_clock():
                budget_exceeded = True
                break
            publication_valid = publication_is_valid()
            if args.finalization_path:
                finalization_epoch = finalization_epoch_from_path(
                    Path(args.finalization_path),
                    attempt_id=args.attempt_id,
                    expected_grace_seconds=args.finalization_timeout_seconds,
                    require_bound_snapshot=args.artifact_protocol_version == 2,
                )
                deadline.observe(finalization_epoch, enforce_timeout=False)
            published_epoch = publication_epoch()
            publication_before_deadline = bool(
                published_epoch is not None
                and published_epoch >= deadline.attempt_started_epoch - 0.001
                and published_epoch <= deadline.active_deadline_epoch() + 1e-6
            )
            if (
                publication_valid
                and startup["state"] == "worker_started"
                and (
                    publication_before_deadline
                    or published_epoch is None and not deadline.expired()
                )
            ):
                if not publication_requested:
                    publication_requested = True
                    publication_was_accepted = True
                    publication_deadline = (
                        time.monotonic() + args.handoff_grace_seconds
                    )
                    codex_terminal_event_deadline = time.monotonic() + max(
                        args.handoff_grace_seconds,
                        args.codex_terminal_event_grace_seconds,
                    )
                    if args.backend == "codex":
                        codex_terminal_event_wait_started = time.monotonic()
                if publication_deadline is not None and time.monotonic() >= publication_deadline:
                    waiting_for_codex_terminal_event = bool(
                        args.backend == "codex"
                        and not usage.saw_source_event("turn.completed", "turn_completed")
                        and codex_terminal_event_deadline is not None
                        and time.monotonic() < codex_terminal_event_deadline
                    )
                    if not waiting_for_codex_terminal_event:
                        break
            elif publication_requested:
                # A mutation during the grace interval revokes the stop signal.
                publication_requested = False
                publication_deadline = None
                codex_terminal_event_deadline = None
            timeout_phase = deadline.observe(
                finalization_epoch,
                enforce_timeout=not publication_requested,
            )
            if timeout_phase is not None:
                timed_out = True
                break

        # A short-lived process may exit after writing its terminal JSONL event
        # but before the selector loop sees the final pipe bytes. Drain only
        # already-readable output; inherited pipes must not extend the bound.
        if process.poll() is not None:
            drain_deadline = time.monotonic() + 0.2
            while selector.get_map() and time.monotonic() < drain_deadline:
                events = selector.select(timeout=0.02)
                if not events:
                    continue
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    transcript.write(chunk)
                    transcript.flush()
                    os.write(1, chunk)
                    startup_output.extend(chunk)
                    if len(startup_output) > 65536:
                        del startup_output[:-65536]
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            usage_payload = json.loads(line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            usage_payload = None
                        if usage.observe(usage_payload):
                            budget_exceeded = True

        # Catch a marker published immediately before a natural worker exit and
        # revalidate after any grace-period output has been flushed.
        if buffer:
            try:
                usage_payload = json.loads(buffer)
            except (UnicodeDecodeError, json.JSONDecodeError):
                usage_payload = None
            if usage.observe(usage_payload):
                budget_exceeded = True
            evidence = startup_event(args.backend, buffer)
            progress = worker_progress_event(args.backend, buffer)
            if evidence and startup["state"] != "worker_started":
                startup["state"] = "worker_started"
                startup["worker_started_at"] = utc_now()
                startup["startup_evidence"] = {
                    "decoder": args.backend,
                    "event": evidence,
                }
                atomic_json(startup_path, startup)
            if progress and not worker_progress_observed:
                worker_progress_observed = True
                startup["worker_progress_at"] = utc_now()
                startup["worker_progress_evidence"] = {
                    "decoder": args.backend,
                    "event": progress,
                }
                atomic_json(startup_path, startup)
        if codex_terminal_event_wait_started is not None:
            codex_terminal_event_wait_elapsed = max(
                0.0,
                time.monotonic() - codex_terminal_event_wait_started,
            )
        if args.finalization_path:
            deadline.observe(
                finalization_epoch_from_path(
                    Path(args.finalization_path),
                    attempt_id=args.attempt_id,
                    expected_grace_seconds=args.finalization_timeout_seconds,
                    require_bound_snapshot=args.artifact_protocol_version == 2,
                ),
                enforce_timeout=process.poll() is None
                and not publication_requested
                and not startup_failed
                and not budget_exceeded,
            )
        timeout_phase = timeout_phase or deadline.timeout_phase
        timed_out = timed_out or timeout_phase is not None
        stopped_after_publication = publication_was_accepted
        final_publication_valid = publication_is_valid()
        if (
            args.backend == "codex"
            and publication_was_accepted
            and usage.require_budget_observations()
        ):
            budget_exceeded = True
        if args.finalization_path:
            deadline.observe(
                finalization_epoch_from_path(
                    Path(args.finalization_path),
                    attempt_id=args.attempt_id,
                    expected_grace_seconds=args.finalization_timeout_seconds,
                    require_bound_snapshot=args.artifact_protocol_version == 2,
                ),
                enforce_timeout=False,
            )
        final_published_epoch = publication_epoch()
        late_publication = late_publication_seen or bool(
            final_publication_valid
            and final_published_epoch is not None
            and final_published_epoch > deadline.active_deadline_epoch() + 1e-6
        )
        if (
            final_publication_valid
            and not publication_was_accepted
            and final_published_epoch is not None
            and final_published_epoch >= deadline.attempt_started_epoch - 0.001
            and final_published_epoch <= deadline.active_deadline_epoch() + 1e-6
        ):
            publication_was_accepted = True
        if late_publication:
            timeout_phase = deadline.phase
            deadline.timeout_phase = timeout_phase
            timed_out = True
        publication_requested = bool(
            final_publication_valid
            and startup["state"] == "worker_started"
            and publication_was_accepted
            and not timed_out
            and not startup_failed
            and not budget_exceeded
        )
        publication_invalidated = stopped_after_publication and not final_publication_valid
        publication_unaccepted = bool(
            final_publication_valid and not publication_requested
        )
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

        if process.poll() is None and (
            timed_out
            or startup_failed
            or budget_exceeded
            or publication_requested
            or publication_invalidated
        ):
            survivors = terminate_processes(
                termination_pgids,
                termination_pids,
                root_pid=process.pid,
                supervision_token=supervision_token,
                observed_pids=observed_pids,
                observed_pgids=observed_pgids,
                cleanup_observation=cleanup_observation,
            )
            reap_process(process)
        else:
            exit_code_now = reap_process(process)
            survivors = terminate_processes(
                termination_pgids,
                termination_pids - {process.pid},
                root_pid=process.pid,
                supervision_token=supervision_token,
                observed_pids=observed_pids,
                observed_pgids=observed_pgids,
                cleanup_observation=cleanup_observation,
            )
            if buffer:
                evidence = startup_event(args.backend, buffer)
                progress = worker_progress_event(args.backend, buffer)
                if evidence and startup["state"] != "worker_started":
                    startup["state"] = "worker_started"
                    startup["worker_started_at"] = utc_now()
                    startup["startup_evidence"] = {"decoder": args.backend, "event": evidence}
                    atomic_json(startup_path, startup)
                if progress and not worker_progress_observed:
                    worker_progress_observed = True
                    startup["worker_progress_at"] = utc_now()
                    startup["worker_progress_evidence"] = {
                        "decoder": args.backend,
                        "event": progress,
                    }
                    atomic_json(startup_path, startup)
            classified_failure = classify_startup_failure(
                args.backend,
                startup_output.decode("utf-8", errors="replace"),
                returncode=exit_code_now,
                include_model_unavailable=args.backend == "codex",
            )
            failed_before_codex_progress = bool(
                args.backend == "codex"
                and exit_code_now != 0
                and not worker_progress_observed
                and classified_failure is not None
            )
            if startup["state"] != "worker_started" or failed_before_codex_progress:
                startup_failed = True
                startup["state"] = "worker_startup_failed"
                startup["failure"] = classified_failure or {
                    "code": "early_exit",
                    "message": f"worker exited before a valid startup event (exit {exit_code_now})",
                    "category": "startup",
                    "recoverable_resume_failure": False,
                    "backend_id": args.backend,
                }
                if failed_before_codex_progress:
                    startup["failure_detected_after_start_event"] = True
                atomic_json(startup_path, startup)

    final_source_receipt: dict[str, Any] | None = None
    if publication_requested:
        candidate_stable = publication_is_valid()
        candidate_payload = publication_state.get("payload")
        final_source = (
            inspect_candidate_source_state(
                Path(args.cwd),
                candidate_payload,
            )
            if isinstance(candidate_payload, dict)
            else None
        )
        if final_source is None or not final_source.valid:
            candidate_stable = False
            publication_state.update(
                valid=False,
                reasons=(
                    list(final_source.reasons)
                    if final_source is not None
                    else ["publication source payload is missing"]
                ),
            )
        else:
            final_source_receipt = dict(final_source.receipt or {})
        full_publication = (
            validate_publication(
                publication_path,
                artifact_protocol_version=args.artifact_protocol_version,
                task_dir=Path(args.task_dir),
                attempt_id=args.attempt_id,
            )
            if candidate_stable and publication_path is not None
            else None
        )
        if full_publication is not None:
            publication_state.update(
                valid=full_publication.valid,
                reasons=list(full_publication.reasons),
                payload=full_publication.payload,
            )
            if (
                full_publication.valid
                and args.artifact_protocol_version == 2
                and accepted_publication_marker_ctime is not None
            ):
                try:
                    latest_dependency = v2_publication_dependency_latest_ctime(
                        Path(args.task_dir),
                        args.attempt_id,
                    )
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    full_publication = type(full_publication)(
                        False,
                        (f"publication temporal closure is invalid: {exc}",),
                        full_publication.payload,
                    )
                else:
                    if (
                        latest_dependency
                        > accepted_publication_marker_ctime + 0.001
                        or latest_dependency
                        > deadline.active_deadline_epoch() + 0.001
                    ):
                        full_publication = type(full_publication)(
                            False,
                            (
                                "publication dependencies were materialized "
                                "after the accepted marker/deadline",
                            ),
                            full_publication.payload,
                        )
                publication_state.update(
                    valid=full_publication.valid,
                    reasons=list(full_publication.reasons),
                    payload=full_publication.payload,
                )
        if full_publication is None or not full_publication.valid:
            publication_requested = False
            publication_invalidated = True
            publication_unaccepted = False

    exit_code = (
        126
        if survivors or not cleanup_observation["verified"]
        else 0
        if publication_requested
        else 124
        if timed_out
        else 125
        if startup_failed or budget_exceeded or publication_invalidated
        else int(process.returncode)
    )
    state = (
        "cleanup_failed"
        if survivors or not cleanup_observation["verified"]
        else "handoff_ready"
        if publication_requested
        else "publication_invalid"
        if publication_invalidated
        else "timed_out"
        if timed_out
        else "startup_failed"
        if startup_failed
        else "budget_exceeded"
        if budget_exceeded
        else "completed"
    )
    codex_terminal_event_drain = (
        {
            "limit_seconds": args.codex_terminal_event_grace_seconds,
            "elapsed_seconds": round(codex_terminal_event_wait_elapsed or 0.0, 6),
            "terminal_event_observed": usage.saw_source_event(
                "turn.completed", "turn_completed"
            ),
        }
        if codex_terminal_event_wait_started is not None
        else None
    )
    atomic_json(state_path, {
        "state": state,
        "worker_pid": process.pid,
        "worker_pgid": process.pid,
        "observed_pids": sorted(observed_pids),
        "observed_pgids": sorted(observed_pgids),
        "surviving_pids": list(survivors),
        "cleanup_verified": cleanup_observation["verified"],
        "cleanup_failure_reason": cleanup_observation["reason"],
        "exit_code": exit_code,
        "startup_state": startup["state"],
        "artifact_protocol_version": args.artifact_protocol_version,
        "publication_requested": publication_requested,
        "publication_invalidated": publication_invalidated,
        "publication_unaccepted": publication_unaccepted,
        "late_publication": late_publication,
        "accepted_publication_sha256": accepted_publication_sha256,
        "accepted_publication_receipt": accepted_publication_receipt,
        "final_source": final_source_receipt,
        "completion_requested": publication_requested,
        "timeout_phase": timeout_phase,
        "finalization_started": deadline.finalization_started_epoch is not None,
        "finalization_timed_out": timeout_phase == "finalization",
        "publication": publication_state if monitor_publication else None,
        "supervision_token": supervision_token,
        "deadline": deadline.state(),
        "deadline_sha256": deadline_sha256,
        "active_deadline_at_epoch": deadline.active_deadline_epoch(),
        "usage": usage.summary(),
        "codex_terminal_event_drain": codex_terminal_event_drain,
    })
    atomic_json(supervisor_path, {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timeout_phase": timeout_phase,
        "startup_failed": startup_failed,
        "budget_exceeded": budget_exceeded,
        "artifact_protocol_version": args.artifact_protocol_version,
        "publication_requested": publication_requested,
        "publication_invalidated": publication_invalidated,
        "publication_unaccepted": publication_unaccepted,
        "late_publication": late_publication,
        "accepted_publication_sha256": accepted_publication_sha256,
        "accepted_publication_receipt": accepted_publication_receipt,
        "final_source": final_source_receipt,
        "completion_requested": publication_requested,
        "finalization_started": deadline.finalization_started_epoch is not None,
        "finalization_timed_out": timeout_phase == "finalization",
        "publication": publication_state if monitor_publication else None,
        "handoff_ready": (
            publication_state
            if monitor_publication and args.artifact_protocol_version == 2
            else None
        ),
        "completion": (
            publication_state
            if monitor_publication and args.artifact_protocol_version == 1
            else None
        ),
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 6),
        "observed_pids": sorted(observed_pids),
        "observed_pgids": sorted(observed_pgids),
        "surviving_pids": list(survivors),
        "cleanup_verified": cleanup_observation["verified"],
        "cleanup_failure_reason": cleanup_observation["reason"],
        "supervision_token": supervision_token,
        "deadline": deadline.state(),
        "deadline_sha256": deadline_sha256,
        "attempt_started_at_epoch": deadline.attempt_started_epoch,
        "execution_deadline_at_epoch": deadline.execution_deadline_epoch,
        "active_deadline_at_epoch": deadline.active_deadline_epoch(),
        "strategy_id": args.strategy_id or None,
        "strategy_sha256": args.strategy_sha256 or None,
        "backend_session_id": observed_session_id or None,
        "usage": usage.summary(),
        "codex_terminal_event_drain": codex_terminal_event_drain,
    })
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
    attempt_deadline_sha256,
