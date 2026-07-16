#!/usr/bin/env python3
"""Run one worker command under an attempt-local deterministic supervisor."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time

from completion import (
    inspect_candidate_source_head,
    inspect_candidate_source_state,
    inspect_publication_candidate,
    publication_path,
    validate_publication,
    v2_publication_dependency_latest_ctime,
)
from supervisor import (
    finalization_epoch_from_path,
    load_or_create_attempt_deadline,
    run_supervised,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervise one worker attempt.")
    parser.add_argument("--timeout-seconds", required=True, type=float)
    parser.add_argument("--grace-seconds", type=float, default=2.0)
    parser.add_argument("--result", required=True)
    parser.add_argument("--cwd", default="")
    parser.add_argument("--shell-command", required=True)
    parser.add_argument("--strategy-id", default="")
    parser.add_argument("--strategy-sha256", default="")
    parser.add_argument("--artifact-protocol-version", choices=(1, 2), type=int, default=1)
    parser.add_argument("--publication-path", default="")
    parser.add_argument("--completion-path", default="")
    parser.add_argument("--task-dir", default="")
    parser.add_argument("--attempt-id", default="")
    parser.add_argument(
        "--completion-grace-seconds",
        "--handoff-grace-seconds",
        dest="publication_grace_seconds",
        type=float,
        default=0.5,
    )
    parser.add_argument("--finalization-path", default="")
    parser.add_argument("--finalization-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--deadline-path", default="")
    parser.add_argument("--deadline-reminder-seconds", type=float, default=60.0)
    args = parser.parse_args()
    if not math.isfinite(args.publication_grace_seconds) or args.publication_grace_seconds < 0:
        parser.error("publication grace seconds must be finite and non-negative")

    legacy_path = args.completion_path
    if args.publication_path and legacy_path:
        if Path(args.publication_path).resolve(strict=False) != Path(legacy_path).resolve(strict=False):
            parser.error("--publication-path and --completion-path disagree")
    configured_path = args.publication_path or legacy_path
    monitor_publication = bool(
        configured_path or args.task_dir or args.attempt_id
    ) or args.artifact_protocol_version == 2
    signal_path: Path | None = None
    if monitor_publication:
        if not args.task_dir or not args.attempt_id:
            parser.error("publication monitoring requires --task-dir and --attempt-id")
        expected = publication_path(
            Path(args.task_dir),
            args.attempt_id,
            args.artifact_protocol_version,
        )
        signal_path = Path(configured_path) if configured_path else expected
        if signal_path.resolve(strict=False) != expected.resolve(strict=False):
            parser.error(
                "publication path must be the protocol-specific path for the supervised attempt"
            )

    publication_state: dict[str, object] = {
        "artifact_protocol_version": args.artifact_protocol_version,
        "path": str(signal_path) if signal_path is not None else None,
        "valid": False,
        "reasons": [],
        "payload": None,
        "sha256": None,
    }
    deadline_path = Path(args.deadline_path) if args.deadline_path else None
    deadline_payload = load_or_create_attempt_deadline(
        deadline_path,
        attempt_timeout_seconds=args.timeout_seconds,
        finalization_grace_seconds=args.finalization_timeout_seconds,
        reminder_seconds=args.deadline_reminder_seconds,
    )
    attempt_started_epoch = float(deadline_payload["started_at_epoch"])
    execution_deadline_epoch = float(
        deadline_payload["execution_deadline_at_epoch"]
    )
    maximum_deadline_epoch = float(
        deadline_payload["execution_deadline_at_epoch"]
    ) + float(deadline_payload["finalization_grace_seconds"])
    accepted_publication_sha256: str | None = None
    accepted_publication_epoch: float | None = None
    accepted_publication_marker_ctime: float | None = None
    accepted_publication_receipt: dict[str, object] | None = None
    late_publication_seen = False

    def publication_requested() -> bool | float:
        nonlocal accepted_publication_epoch
        nonlocal accepted_publication_marker_ctime
        nonlocal accepted_publication_receipt
        nonlocal accepted_publication_sha256
        nonlocal late_publication_seen
        if signal_path is None:
            return False
        if not signal_path.exists():
            publication_state.update(
                valid=False,
                reasons=["publication marker is missing"],
                payload=None,
            )
            return False
        result = inspect_publication_candidate(
            signal_path,
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
        publication_epoch = receipt.get("ctime")
        if not isinstance(publication_sha256, str) or not isinstance(
            publication_epoch,
            (int, float),
        ):
            return False
        marker_ctime = float(publication_epoch)
        active_deadline_epoch = (
            maximum_deadline_epoch
            if finalization_started() is not None
            else execution_deadline_epoch
        )
        if accepted_publication_receipt is None:
            if marker_ctime < attempt_started_epoch - 0.001:
                publication_state.update(
                    valid=False,
                    reasons=["publication marker is outside the attempt deadline interval"],
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
        assert accepted_publication_epoch is not None
        return accepted_publication_epoch

    def finalization_started() -> float | None:
        if not args.finalization_path:
            return None
        return finalization_epoch_from_path(
            Path(args.finalization_path),
            attempt_id=args.attempt_id,
            expected_grace_seconds=args.finalization_timeout_seconds,
            require_bound_snapshot=args.artifact_protocol_version == 2,
        )

    result = run_supervised(
        ["/bin/bash", "-c", args.shell_command],
        timeout_seconds=args.timeout_seconds,
        grace_seconds=args.grace_seconds,
        cwd=Path(args.cwd) if args.cwd else None,
        stdin=0,
        stdout=1,
        stderr=2,
        state_path=Path(args.result).parent / "runtime" / "supervisor.json",
        completion_requested=publication_requested if monitor_publication else None,
        completion_grace_seconds=args.publication_grace_seconds,
        finalization_started=finalization_started if args.finalization_path else None,
        finalization_timeout_seconds=args.finalization_timeout_seconds,
        deadline_path=deadline_path,
        deadline_reminder_seconds=args.deadline_reminder_seconds,
    )
    final_publication = publication_requested()
    candidate_valid = bool(final_publication)
    final_source_receipt: dict[str, object] | None = None
    if candidate_valid and signal_path is not None:
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
            publication_state.update(
                valid=False,
                reasons=(
                    list(final_source.reasons)
                    if final_source is not None
                    else ["publication source payload is missing"]
                ),
            )
            publication_valid = False
        else:
            final_source_receipt = dict(final_source.receipt or {})
        full_publication = validate_publication(
            signal_path,
            artifact_protocol_version=args.artifact_protocol_version,
            task_dir=Path(args.task_dir),
            attempt_id=args.attempt_id,
        )
        if final_source is not None and final_source.valid:
            publication_state.update(
                valid=full_publication.valid,
                reasons=list(full_publication.reasons),
                payload=full_publication.payload,
            )
            publication_valid = full_publication.valid
        if (
            publication_valid
            and args.artifact_protocol_version == 2
            and accepted_publication_marker_ctime is not None
        ):
            try:
                latest_dependency = v2_publication_dependency_latest_ctime(
                    Path(args.task_dir),
                    args.attempt_id,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                publication_state.update(
                    valid=False,
                    reasons=[f"publication temporal closure is invalid: {exc}"],
                )
                publication_valid = False
            else:
                if (
                    latest_dependency > accepted_publication_marker_ctime + 0.001
                    or latest_dependency > result.active_deadline_epoch + 0.001
                ):
                    publication_state.update(
                        valid=False,
                        reasons=[
                            "publication dependencies were materialized after "
                            "the accepted marker/deadline"
                        ],
                    )
                    publication_valid = False
    else:
        publication_valid = False
    publication_within_deadline = bool(
        isinstance(final_publication, (int, float))
        and not isinstance(final_publication, bool)
        and math.isfinite(float(final_publication))
        and float(final_publication) >= result.attempt_started_epoch - 0.001
        and float(final_publication) <= result.active_deadline_epoch + 1e-6
    )
    publication_accepted = bool(
        publication_valid
        and not result.timed_out
        and (result.completion_requested or publication_within_deadline)
    )
    publication_invalidated = bool(
        result.completion_requested and not publication_valid
    )
    late_publication = late_publication_seen or bool(
        publication_valid
        and isinstance(final_publication, (int, float))
        and not isinstance(final_publication, bool)
        and float(final_publication) > result.active_deadline_epoch + 1e-6
    )
    publication_unaccepted = bool(
        publication_valid and not publication_accepted
    )
    timed_out = bool(result.timed_out or late_publication)
    timeout_phase = (
        result.timeout_phase
        or (result.deadline_phase if late_publication else None)
    )
    exit_code = (
        124
        if late_publication
        else 125
        if publication_invalidated or publication_unaccepted
        else result.exit_code
    )
    if late_publication:
        state_path = Path(args.result).parent / "runtime" / "supervisor.json"
        try:
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            state_payload = {}
        if isinstance(state_payload, dict):
            state_payload.update(
                state="timed_out",
                exit_code=124,
                timeout_phase=timeout_phase,
                finalization_timed_out=timeout_phase == "finalization",
                publication_requested=False,
                publication_unaccepted=True,
                late_publication=True,
            )
            temporary_state = state_path.with_suffix(state_path.suffix + ".tmp")
            temporary_state.write_text(
                json.dumps(state_payload, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_state, state_path)
    payload = {
        "exit_code": exit_code,
        "child_exit_code": result.child_exit_code,
        "timed_out": timed_out,
        "timeout_phase": timeout_phase,
        "completion_requested": publication_accepted,
        "publication_requested": publication_accepted,
        "publication_invalidated": publication_invalidated,
        "publication_unaccepted": publication_unaccepted,
        "late_publication": late_publication,
        "accepted_publication_sha256": accepted_publication_sha256,
        "accepted_publication_receipt": accepted_publication_receipt,
        "final_source": final_source_receipt,
        "finalization_started": result.finalization_started,
        "finalization_timed_out": timeout_phase == "finalization",
        "artifact_protocol_version": args.artifact_protocol_version,
        "publication": publication_state if monitor_publication else None,
        "completion": (
            publication_state
            if monitor_publication and args.artifact_protocol_version == 1
            else None
        ),
        "handoff_ready": (
            publication_state
            if monitor_publication and args.artifact_protocol_version == 2
            else None
        ),
        "elapsed_seconds": result.elapsed_seconds,
        "observed_pids": list(result.observed_pids),
        "observed_pgids": list(result.observed_pgids),
        "surviving_pids": list(result.surviving_pids),
        "cleanup_verified": result.cleanup_verified,
        "cleanup_failure_reason": result.cleanup_failure_reason,
        "active_deadline_at_epoch": result.active_deadline_epoch,
        "deadline_phase": result.deadline_phase,
        "execution_deadline_at_epoch": result.execution_deadline_epoch,
        "attempt_started_at_epoch": result.attempt_started_epoch,
        "deadline_sha256": result.deadline_sha256,
        "strategy_id": args.strategy_id or None,
        "strategy_sha256": args.strategy_sha256 or None,
    }
    path = Path(args.result)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
