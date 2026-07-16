#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${ROOT}/tests/unit"
source "${ROOT}/scripts/test_runner_lib.sh"

usage() {
  echo "usage: ${0##*/} [--pattern <unittest-discovery-pattern>]" >&2
}

pattern='test_*.py'
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --pattern)
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

if [[ ! -d "${UNIT_DIR}" ]]; then
  echo "unit test directory not found: ${UNIT_DIR}" >&2
  exit 2
fi

matched_count="$(find "${UNIT_DIR}" -type f -name "${pattern}" -print | wc -l | tr -d ' ')"
if [[ "${matched_count}" -eq 0 ]]; then
  echo "unit selector matched no tests: ${pattern}" >&2
  exit 2
fi

rdo_test_init
log_path="${RDO_TEST_LOG_DIR}/unit.log"
unit_pythonpath="${ROOT}/scripts"
if [[ -n "${PYTHONPATH:-}" ]]; then
  unit_pythonpath="${unit_pythonpath}:${PYTHONPATH}"
fi

if rdo_test_run_logged \
  "unit/${pattern}" \
  "${log_path}" \
  env PYTHONPATH="${unit_pythonpath}" \
  python3 -m unittest discover -s "${UNIT_DIR}" -p "${pattern}"; then
  test_count="$(awk '/^Ran [0-9]+ tests? in / { count=$2 } END { print count }' "${log_path}")"
  if [[ "${test_count:-0}" -eq 0 ]]; then
    printf 'unit selector loaded no tests: %s\nfull log: %s\n' \
      "${pattern}" "${log_path}" >&2
    exit 2
  fi
  summary="$(awk '/^Ran [0-9]+ tests? in / { line=$0 } END { print line }' "${log_path}")"
  if [[ -z "${summary}" ]]; then
    summary="matched ${matched_count} files"
  fi
  printf 'PASS unit: %s (%ss); log: %s\n' \
    "${summary}" "${RDO_TEST_LAST_ELAPSED}" "${log_path}"
else
  status=$?
  exit "${status}"
fi
