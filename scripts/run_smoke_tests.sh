#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="${ROOT}/tests/smoke"
UNIT_DIR="${ROOT}/tests/unit"

if [[ ! -d "${TEST_DIR}" ]]; then
  echo "smoke test directory not found: ${TEST_DIR}" >&2
  exit 2
fi

if [[ -d "${UNIT_DIR}" ]]; then
  echo "==> Python unit tests"
  PYTHONPATH="${ROOT}/scripts" python3 -m unittest discover -s "${UNIT_DIR}" -p 'test_*.py'
fi

for test_script in "${TEST_DIR}"/test_*.sh; do
  echo "==> ${test_script##*/}"
  bash "${test_script}"
done

echo "All smoke tests passed."
