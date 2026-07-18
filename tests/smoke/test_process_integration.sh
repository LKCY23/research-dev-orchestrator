#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH="${ROOT}/scripts:${ROOT}/tests/unit${PYTHONPATH:+:${PYTHONPATH}}"
export RDO_RUN_PROCESS_INTEGRATION=1

python3 -m unittest discover -s "${ROOT}/tests/unit" -p 'test_supervisor.py' -v
python3 -m unittest discover -s "${ROOT}/tests/unit" -p 'test_machine_attempt_supervisor.py' -v
python3 -m unittest discover -s "${ROOT}/tests/unit" -p 'test_check_broker.py' -v
python3 -m unittest discover -s "${ROOT}/tests/unit" -p 'test_supervise_attempt.py' -v
python3 -m unittest -v \
  test_task_merge.TaskMergeTests.test_failed_post_merge_verification_is_recorded_as_merged \
  test_task_merge.TaskMergeTests.test_timed_out_post_merge_verification_kills_descendants
python3 -m unittest -v \
  test_protocol_cli_v2.ProtocolCliV2Tests.test_full_dispatch_rejects_worker_replacement_of_frozen_approved_strategy
