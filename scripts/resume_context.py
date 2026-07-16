#!/usr/bin/env python3
"""Validate explicit cross-attempt workflow reuse and materialize resume context."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from protocol import append_event, load_json, utc_now, write_json
from strategy import canonical_digest


class ResumeContextError(ValueError):
    pass


def workflow_records(attempt: Path) -> list[dict[str, Any]]:
    path = attempt / "runtime" / "WORKFLOWS.ndjson"
    return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def workflow_satisfied(records: list[dict[str, Any]], workflow_id: str) -> bool:
    return any(
        record.get("workflow_id") == workflow_id
        and record.get("event") in {"workflow_completed", "workflow_carried_forward"}
        for record in records
    )


def fingerprint_sha(path: Path) -> str:
    payload = load_json(path)
    digest = payload.get("semantic_sha256") if isinstance(payload, dict) else None
    if not isinstance(digest, str) or not digest:
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if isinstance(entries, list):
            digest = canonical_digest(entries)
    if not isinstance(digest, str) or not digest:
        digest = payload.get("sha256") if isinstance(payload, dict) else None
    if not isinstance(digest, str) or not digest:
        raise ResumeContextError(f"invalid worktree fingerprint: {path}")
    return digest


def build_resume_context(
    *,
    task_dir: Path,
    attempt_dir: Path,
    strategy_path: Path,
    current_worktree_before: Path,
) -> dict[str, Any]:
    strategy = load_json(strategy_path)
    current_sha = fingerprint_sha(current_worktree_before)
    declarations = [item for item in strategy["workflows"] if item.get("resume") is not None]
    entries: list[dict[str, Any]] = []
    for target in declarations:
        resume = target["resume"]
        source_attempt_id = resume["from_attempt"]
        if source_attempt_id == attempt_dir.name:
            raise ResumeContextError("workflow cannot resume from the current attempt")
        source_attempt = task_dir / "attempts" / source_attempt_id
        metadata_path = source_attempt / "ATTEMPT.json"
        if not metadata_path.exists():
            raise ResumeContextError(f"resume source attempt does not exist: {source_attempt_id}")
        metadata = load_json(metadata_path)
        if metadata.get("state") not in {"completed", "invalid_handoff"}:
            raise ResumeContextError(f"resume source attempt is not terminal: {source_attempt_id}")
        source_workflow = resume["from_workflow"]
        records = workflow_records(source_attempt)
        if not workflow_satisfied(records, source_workflow):
            raise ResumeContextError(
                f"resume source workflow is not complete: {source_attempt_id}/{source_workflow}"
            )
        source_after = source_attempt / "runtime" / "worktree-after.json"
        if not source_after.exists():
            raise ResumeContextError(f"resume source worktree fingerprint is missing: {source_attempt_id}")
        source_sha = fingerprint_sha(source_after)
        if source_sha != current_sha:
            raise ResumeContextError(
                f"resume source worktree no longer matches current worktree: {source_attempt_id}"
            )
        checkpoint = {
            "source_attempt_id": source_attempt_id,
            "source_workflow_id": source_workflow,
            "source_strategy_id": metadata.get("strategy_id"),
            "source_strategy_sha256": metadata.get("strategy_sha256"),
            "source_worktree_sha256": source_sha,
            "target_workflow_id": target["workflow_id"],
            "mode": resume["mode"],
        }
        checkpoint["checkpoint_sha256"] = canonical_digest(checkpoint)
        entries.append(checkpoint)

    reused = {entry["target_workflow_id"] for entry in entries if entry["mode"] == "reuse"}
    required = {item["workflow_id"] for item in strategy["workflows"] if item["required"]}
    if reused and required.issubset(reused) and strategy["completion_gate"]["acceptance_commands_pass"]:
        raise ResumeContextError(
            "acceptance command records are attempt-local; at least one required workflow must use revalidate"
        )

    payload = {
        "schema_version": 1,
        "task_id": strategy["task_id"],
        "attempt_id": attempt_dir.name,
        "strategy_id": strategy["strategy_id"],
        "strategy_sha256": canonical_digest(strategy),
        "current_worktree_sha256": current_sha,
        "generated_at": utc_now(),
        "checkpoints": entries,
        "carried_forward_workflows": sorted(reused),
        "remaining_workflows": [
            item["workflow_id"] for item in strategy["workflows"] if item["workflow_id"] not in reused
        ],
    }
    runtime = attempt_dir / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    attempt_path = attempt_dir / "ATTEMPT.json"
    if not attempt_path.exists():
        raise ResumeContextError(f"current ATTEMPT.json is missing: {attempt_dir.name}")
    attempt_metadata = load_json(attempt_path)
    write_json(runtime / "RESUME_CONTEXT.json", payload)
    attempt_metadata["resume_context_sha256"] = canonical_digest(payload)
    attempt_metadata["carried_forward_workflows"] = sorted(reused)
    attempt_metadata["remaining_workflows"] = payload["remaining_workflows"]
    write_json(attempt_path, attempt_metadata)
    write_json(runtime / "DISPATCH_ATTEMPT.json", attempt_metadata)
    workflows_path = runtime / "WORKFLOWS.ndjson"
    existing = workflow_records(attempt_dir)
    already_carried = {
        record.get("workflow_id") for record in existing if record.get("event") == "workflow_carried_forward"
    }
    run_dir = task_dir.parent.parent
    for entry in entries:
        if entry["mode"] != "reuse" or entry["target_workflow_id"] in already_carried:
            continue
        record = {
            "at": utc_now(),
            "event": "workflow_carried_forward",
            "attempt_id": attempt_dir.name,
            "workflow_id": entry["target_workflow_id"],
            "instance_id": f"resume-{entry['source_attempt_id']}-{entry['source_workflow_id']}",
            "source_attempt_id": entry["source_attempt_id"],
            "source_workflow_id": entry["source_workflow_id"],
            "checkpoint_sha256": entry["checkpoint_sha256"],
        }
        with workflows_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        append_event(
            run_dir,
            {
                **record,
                "actor": "dispatch",
                "run_id": run_dir.name,
                "task_id": strategy["task_id"],
            },
        )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize validated cross-attempt resume context.")
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--attempt-dir", required=True)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--current-worktree-before", required=True)
    args = parser.parse_args()
    try:
        payload = build_resume_context(
            task_dir=Path(args.task_dir),
            attempt_dir=Path(args.attempt_dir),
            strategy_path=Path(args.strategy),
            current_worktree_before=Path(args.current_worktree_before),
        )
    except ResumeContextError as exc:
        print(f"resume context error: {exc}", file=__import__("sys").stderr)
        return 2
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
