"""Opt-in gate for tests that require the host process table and real signals."""

from __future__ import annotations

import os
import unittest


def require_process_integration() -> None:
    if os.environ.get("RDO_RUN_PROCESS_INTEGRATION") != "1":
        raise unittest.SkipTest(
            "requires real process-table access; covered by smoke/process-integration"
        )
