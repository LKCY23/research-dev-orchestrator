#!/usr/bin/env python3
"""Deterministic external verifier for L01."""

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
    from miniqueue import InvalidJobError, RetryPolicy

    policy = RetryPolicy(base_delay=2.5, multiplier=3, max_delay=40)
    actual = [policy.delay_for_attempt(value) for value in range(1, 5)]
    if actual != [2.5, 7.5, 22.5, 40.0]:
        sys.stderr.write(f"unexpected retry sequence: {actual!r}\n")
        return 1
    try:
        policy.delay_for_attempt(0)
    except InvalidJobError:
        pass
    else:
        sys.stderr.write("attempt zero must be rejected\n")
        return 1
    print("L01 verifier passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
