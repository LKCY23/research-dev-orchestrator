#!/usr/bin/env python3
"""Deterministic external verifier for L03."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def fail(message: str) -> int:
    sys.stderr.write(message + "\n")
    return 1


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    visible = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )
    if visible.returncode:
        sys.stderr.write(visible.stdout)
        return 1
    sys.path.insert(0, str(root / "src"))
    from miniqueue import (
        InvalidJobError,
        JobState,
        JsonStore,
        ManualClock,
        Queue,
        Scheduler,
    )

    with tempfile.TemporaryDirectory() as directory:
        clock = ManualClock(42)
        path = Path(directory) / "queue.json"
        queue = Queue(JsonStore(path), clock=clock)
        queue.enqueue({"kind": "work"}, job_id="job")
        cancelled = queue.cancel("job", reason="  superseded  ")
        if (
            cancelled.state is not JobState.CANCELLED
            or not cancelled.terminal
            or cancelled.last_error != "superseded"
            or cancelled.completed_at != 42
            or cancelled.attempts != 0
        ):
            return fail(f"invalid cancellation result: {cancelled!r}")
        reloaded = Queue(JsonStore(path), clock=clock).get("job")
        if reloaded.state is not JobState.CANCELLED:
            return fail("cancelled state did not survive JSON reload")
        repeated = queue.cancel("job", reason="ignored on idempotent repeat")
        if repeated.to_dict() != cancelled.to_dict():
            return fail("repeated cancellation must return unchanged terminal job")
        stats = queue.stats()
        if stats.cancelled != 1 or stats.total != 1:
            return fail(f"cancelled stats are incorrect: {stats!r}")
        handled: list[str] = []
        scheduler = Scheduler(
            queue,
            {"work": lambda payload: handled.append(payload["kind"])},
            worker_id="worker",
        )
        if scheduler.run_once().status != "idle" or handled:
            return fail("cancelled work was dispatched")

    for terminal_kind in ("leased", "succeeded", "dead"):
        clock = ManualClock(10)
        queue = Queue(clock=clock)
        queue.enqueue(
            {"kind": "work"}, job_id="job", max_attempts=1
        )
        queue.lease_next("worker")
        if terminal_kind == "succeeded":
            queue.acknowledge("job", "worker")
        elif terminal_kind == "dead":
            queue.fail("job", "worker", "failed")
        before = queue.get("job").to_dict()
        try:
            queue.cancel("job")
        except InvalidJobError:
            pass
        else:
            return fail(f"{terminal_kind} job cancellation must be rejected")
        if queue.get("job").to_dict() != before:
            return fail(f"rejected {terminal_kind} cancellation mutated the job")

    blank_queue = Queue(clock=ManualClock())
    blank_queue.enqueue({"kind": "work"}, job_id="blank")
    try:
        blank_queue.cancel("blank", reason="   ")
    except InvalidJobError:
        pass
    else:
        return fail("blank cancellation reason must be rejected")
    print("L03 verifier passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
