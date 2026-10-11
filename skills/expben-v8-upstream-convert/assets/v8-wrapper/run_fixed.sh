#!/bin/sh
# Derived from sec-bmk dc6901572; fixed release-asan comparison runner.
set -u
JS="${1:-}"
shift 2>/dev/null || true
if [ -z "${JS}" ] || [ ! -r "${JS}" ]; then
  echo "usage: run_fixed.sh <exploit.js> [extra d8 flags]" >&2
  exit 2
fi
export ASAN_OPTIONS="${ASAN_OPTIONS:-symbolize=1:abort_on_error=1:detect_leaks=0:halt_on_error=1}"
cd /testbed/grader/fixed
exec ./d8 --no-fuzzing "$@" "${JS}"
