#!/usr/bin/env bash

rdo_test_init() {
  RDO_TEST_TAIL_LINES="${RDO_TEST_TAIL_LINES:-80}"
  case "${RDO_TEST_TAIL_LINES}" in
    ''|*[!0-9]*|0)
      echo "RDO_TEST_TAIL_LINES must be a positive integer" >&2
      return 2
      ;;
  esac

  if [[ -z "${RDO_TEST_LOG_DIR:-}" ]]; then
    RDO_TEST_LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rdo-tests.XXXXXX")"
  else
    mkdir -p "${RDO_TEST_LOG_DIR}"
  fi
  export RDO_TEST_LOG_DIR RDO_TEST_TAIL_LINES
}

rdo_test_run_logged() {
  local label="$1"
  local log_path="$2"
  shift 2

  local started="${SECONDS}"
  local status
  if "$@" >"${log_path}" 2>&1; then
    status=0
  else
    status=$?
  fi
  RDO_TEST_LAST_ELAPSED=$((SECONDS - started))

  if [[ "${status}" -ne 0 ]]; then
    printf 'FAIL %s (%ss, exit %s)\n' \
      "${label}" "${RDO_TEST_LAST_ELAPSED}" "${status}" >&2
    printf 'last %s log lines:\n' "${RDO_TEST_TAIL_LINES}" >&2
    tail -n "${RDO_TEST_TAIL_LINES}" "${log_path}" >&2
    printf 'full log: %s\n' "${log_path}" >&2
  fi
  return "${status}"
}
