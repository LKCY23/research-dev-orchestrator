#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "${RDO_ROOT}"

python3 "${RDO_ROOT}/evaluation/light_bench/bench.py" validate

RDO_RUN_LIGHT_BENCH_INTEGRATION=1 \
PYTHONPATH="${RDO_ROOT}/scripts" \
python3 -m unittest -v \
  tests.unit.test_light_bench.LightBenchTests.test_direct_and_delegated_runs_exercise_public_rdo_lifecycle \
  tests.unit.test_light_bench.LightBenchTests.test_full_run_records_mechanical_strategy_approval_and_two_attempts
