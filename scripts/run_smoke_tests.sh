#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="${ROOT}/tests/smoke"

if [[ ! -d "${TEST_DIR}" ]]; then
  echo "smoke test directory not found: ${TEST_DIR}" >&2
  exit 2
fi

for test_script in "${TEST_DIR}"/test_*.sh; do
  echo "==> ${test_script##*/}"
  bash "${test_script}"
done

echo "All smoke tests passed."
