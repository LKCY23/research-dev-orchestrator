#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/scripts/test_runner_lib.sh"

if [[ "$#" -ne 0 ]]; then
  echo "usage: ${0##*/}" >&2
  exit 2
fi

rdo_test_init
started="${SECONDS}"

if "${ROOT}/scripts/run_unit_tests.sh"; then
  :
else
  status=$?
  exit "${status}"
fi
if "${ROOT}/scripts/run_smoke_tests.sh"; then
  :
else
  status=$?
  exit "${status}"
fi

printf 'PASS all-tests (%ss); logs: %s\n' \
  "$((SECONDS - started))" "${RDO_TEST_LOG_DIR}"
