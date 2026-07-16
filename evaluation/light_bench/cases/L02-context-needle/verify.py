#!/usr/bin/env python3
"""Deterministic external verifier for L02."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


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
    from miniqueue import InvalidJobError, ManualClock, Queue, QueueConfig

    clock = ManualClock(100)
    queue = Queue(clock=clock, config=QueueConfig(default_lease_seconds=17))
    queue.enqueue({"kind": "work"}, job_id="job")
    leased = queue.lease_next("worker")
    if leased.leased_until != 117:
        sys.stderr.write(
            f"omitted acquisition duration produced {leased.leased_until}, expected 117\n"
        )
        return 1
    clock.advance(2)
    renewed = queue.renew("job", "worker")
    if renewed.leased_until != 119:
        sys.stderr.write(
            f"omitted renewal duration produced {renewed.leased_until}, expected 119\n"
        )
        return 1
    explicit_queue = Queue(
        clock=ManualClock(5), config=QueueConfig(default_lease_seconds=17)
    )
    explicit_queue.enqueue({"kind": "work"}, job_id="explicit")
    if explicit_queue.lease_next("worker", ttl_seconds=4).leased_until != 9:
        sys.stderr.write("explicit duration no longer takes precedence\n")
        return 1
    try:
        explicit_queue.renew("explicit", "worker", ttl_seconds=True)
    except InvalidJobError:
        pass
    else:
        sys.stderr.write("boolean duration must remain invalid\n")
        return 1
    print("L02 verifier passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
