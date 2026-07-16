#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="${ROOT}/tests/smoke"
source "${ROOT}/scripts/test_runner_lib.sh"

usage() {
  echo "usage: ${0##*/} [--match <script-basename-or-pattern>]" >&2
}

pattern='test_*.sh'
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --match)
      if [[ "$#" -lt 2 ]]; then
        usage
        exit 2
      fi
      if [[ -z "$2" ]]; then
        usage
        exit 2
      fi
      pattern="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ ! -d "${TEST_DIR}" ]]; then
  echo "smoke test directory not found: ${TEST_DIR}" >&2
  exit 2
fi
if [[ "${pattern}" == */* ]]; then
  echo "smoke selector must be a basename pattern: ${pattern}" >&2
  exit 2
fi

shopt -s nullglob
saved_ifs="${IFS}"
IFS=
test_scripts=("${TEST_DIR}"/${pattern})
IFS="${saved_ifs}"
shopt -u nullglob
if [[ "${#test_scripts[@]}" -eq 0 ]]; then
  echo "smoke selector matched no tests: ${pattern}" >&2
  exit 2
fi

rdo_test_init
total_started="${SECONDS}"
passed=0
for test_script in "${test_scripts[@]}"; do
  if [[ ! -f "${test_script}" ]]; then
    continue
  fi
  test_name="${test_script##*/}"
  log_path="${RDO_TEST_LOG_DIR}/smoke-${test_name}.log"
  if rdo_test_run_logged \
    "smoke/${test_name}" \
    "${log_path}" \
    bash "${test_script}"; then
    passed=$((passed + 1))
    printf 'PASS smoke/%s (%ss)\n' "${test_name}" "${RDO_TEST_LAST_ELAPSED}"
  else
    status=$?
    exit "${status}"
  fi
done

if [[ "${passed}" -eq 0 ]]; then
  echo "smoke selector matched no test files: ${pattern}" >&2
  exit 2
fi
printf 'PASS smoke: %s scripts (%ss); logs: %s\n' \
  "${passed}" "$((SECONDS - total_started))" "${RDO_TEST_LOG_DIR}"
